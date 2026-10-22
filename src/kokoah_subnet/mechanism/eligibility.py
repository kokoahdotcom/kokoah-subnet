"""Eligibility (specification section 9a): who may earn on a day, and from which trade.

Two conditions, both checked on every scored day:

1. **Equity.** The day's time-weighted average account value of the bound address is at least `min_equity_usd`.
   It is computed from the exchange's own recorded history of the account (the portfolio window named by
   `equity_window`), never from a single snapshot: the recorded points inside the day are joined by straight lines,
   the day's two edges are interpolated from the points around them, and the area is divided by the day's length.
   When too few points are recorded the average is unknown, and an unknown average is never estimated.
2. **Qualification.** A new identity (a hotkey with a bound address, from its registration) earns nothing until it
   has made `qualify_trades` qualifying trades worth `qualify_notional_usd` together. A trade is one execution:
   the eligible fills of one coin and side at one instant (an order that crosses several resting orders fills
   several times at the same millisecond). The trades before the one that completes both counts earn no credit;
   that trade and every later one do. Qualification is kept until the identity changes.

On a day either condition fails, the miner is ineligible: its trades earn no credit, its smoothed credit decays
as on a day without trades, it is left out of the day's shares and it is paid nothing. Nothing is owed later.
Volume, account value and skill keep being recorded.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from .fills import Fill
from .params import Params
from .state import MinerState


@dataclass(frozen=True)
class Trade:
    time_ms: int
    coin: str
    side: str
    notional: float
    fills: tuple[Fill, ...]


def trades(fills: Iterable[Fill]) -> list[Trade]:
    """Group fills into executions: same coin, side and millisecond. Ordered by time, then coin, then side, so every
    validator walks them in the same order."""
    groups: dict[tuple[int, str, str], list[Fill]] = {}
    for f in sorted(fills, key=lambda x: (x.time_ms, x.coin, x.side, x.px, x.sz)):
        groups.setdefault((f.time_ms, f.coin, f.side), []).append(f)
    return [
        Trade(t, c, s, sum(f.notional for f in group), tuple(group))
        for (t, c, s), group in sorted(groups.items(), key=lambda kv: kv[0])
    ]


@dataclass(frozen=True)
class Qualification:
    qualified: bool  # after today
    trades: int  # qualifying trades counted so far (stops counting once qualified)
    notional: float  # their eligible notional
    credited: tuple[Fill, ...]  # today's fills that may earn credit (before the equity check)
    qualified_at_ms: int | None = None  # the trade that completed qualification, when it happened today


def qualify(
    fills: Sequence[Fill], qualified: bool, trades_so_far: int, notional_so_far: float, params: Params
) -> Qualification:
    """Walk today's eligible fills in time order. Already qualified: every fill may earn. Not yet: count trades
    and notional until both thresholds are met; that trade and the ones after it may earn."""
    if qualified:
        return Qualification(True, trades_so_far, notional_so_far, tuple(fills))
    count, notional = trades_so_far, notional_so_far
    ordered = [t for t in trades(fills) if t.notional > 0]
    for i, t in enumerate(ordered):
        count += 1
        notional += t.notional
        if count >= params.qualify_trades and notional >= params.qualify_notional_usd:
            credited = tuple(f for later in ordered[i:] for f in later.fills)
            return Qualification(True, count, notional, credited, t.time_ms)
    return Qualification(False, count, notional, ())


@dataclass(frozen=True)
class EquityAverage:
    value: float | None  # USD; None when the recorded history cannot say
    points: int  # recorded points inside the day
    note: str = ""


def daily_average_equity(
    points: Iterable[tuple[int, float]],
    start_ms: int,
    end_ms: int,
    origin_ms: int | None,
    min_points: int,
) -> EquityAverage:
    """Time-weighted average account value over [start_ms, end_ms) from recorded (time, value) points.

    `origin_ms` is when the account's recorded history begins. An account whose history begins inside the day had
    nothing before its first point (that part of the day counts as zero). An account whose history begins earlier
    but has no recorded point before the day (a window that no longer reaches back) gives an unknown average."""
    by_time: dict[int, float] = {}
    for t, v in points:
        by_time[int(t)] = float(v)
    pts = sorted(by_time.items())
    span = end_ms - start_ms
    if not pts or span <= 0:
        return EquityAverage(0.0, 0, "no recorded account history")
    before = [p for p in pts if p[0] < start_ms]
    inside = [p for p in pts if start_ms <= p[0] < end_ms]
    after = [p for p in pts if p[0] >= end_ms]
    if not before and not inside:
        return EquityAverage(0.0, 0, "account history begins after the day")
    begins_in_day = not before
    if begins_in_day:
        if origin_ms is None or origin_ms < start_ms:
            return EquityAverage(None, len(inside), "recorded history does not reach the start of the day")
    elif len(inside) < min_points:
        return EquityAverage(None, len(inside), "too few recorded points in the day")

    def lerp(a: tuple[int, float], b: tuple[int, float], t: int) -> float:
        if b[0] == a[0]:
            return b[1]
        return a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0])

    path: list[tuple[int, float]] = []
    if not begins_in_day:
        nxt = inside[0] if inside else after[0] if after else None
        path.append((start_ms, lerp(before[-1], nxt, start_ms) if nxt else before[-1][1]))
    path.extend(inside)
    last = path[-1]
    path.append((end_ms, lerp(last, after[0], end_ms) if after else last[1]))
    area = sum((b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(path, path[1:], strict=False))
    note = "account history begins during the day" if begins_in_day else ""
    return EquityAverage(area / span, len(inside), note)


def identity_change(state: MinerState, address: str, registered_block: int | None) -> str | None:
    """Why the state's credit and qualification must not carry over to today's identity, or None. A different
    bound address (a migration) or a new registration of the hotkey makes a fresh identity."""
    if state.address is not None and state.address != address:
        return "address changed"
    if registered_block and state.registered_block and registered_block != state.registered_block:
        return "registered again"
    return None


def fresh_identity(state: MinerState) -> MinerState:
    """Drop what a fresh identity must not inherit: the smoothed fee credit and fees, and the qualification. What a
    fresh identity must not escape stays: the pay history (holding target), the skill record and any penalty."""
    return replace(state, phi_hat=0.0, fee_hat=0.0, qualified=False, qual_trades=0, qual_notional=0.0)
