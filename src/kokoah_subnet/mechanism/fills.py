"""Fills: the unit of everything the mechanism counts.

Two public sources feed it. Hyperliquid's per-builder daily file (`builder_fills/{builder}/{YYYYMMDD}.csv.lz4`) is
the canonical list of fills that carry the builder code; the address's own public fill history (`userFillsByTime`)
supplies the fill's total fee field. `match_total_fees` joins the two.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

BUILDER_CSV_HEADER = (
    "time,user,coin,side,px,sz,crossed,special_trade_type,tif,is_trigger,counterparty,closed_pnl,twap_id,builder_fee"
)


@dataclass(frozen=True)
class Fill:
    """One fill. Addresses are lowercase; money is USD; `side` is "B" (buy) or "A" (sell)."""

    time_ms: int
    user: str
    coin: str
    side: str
    px: float
    sz: float
    crossed: bool  # taker
    builder_fee: float  # USD paid to the builder on this fill
    counterparty: str = ""  # lowercase address, "" when unknown
    closed_pnl: float = 0.0
    total_fee: float | None = None  # the fill's total fee field: exchange fee (rebate negative) plus builder fee
    mark_px: float | None = None  # mark price at the fill, when known (for the 0.5% clip)
    twap_id: int | None = None
    tid: int | None = None  # the exchange's trade id, shared by both sides of a trade (history fills only)
    cloid: str | None = None  # the client order id (history fills only)
    source: str = "file"  # "file" (builder file only), "history" (the address's own history only), "both"

    @property
    def notional(self) -> float:
        return self.px * self.sz

    @property
    def signed_size(self) -> float:
        return self.sz if self.side == "B" else -self.sz

    @property
    def day(self) -> str:
        """UTC day of the fill, YYYY-MM-DD."""
        return datetime.fromtimestamp(self.time_ms / 1000, tz=UTC).strftime("%Y-%m-%d")

    def match_key(self) -> tuple[str, str, int, float, float, str]:
        """The builder file gives whole seconds and the history milliseconds, so fills are matched on the second."""
        return (self.user, self.coin, self.time_ms // 1000, self.px, self.sz, self.side)


def is_perp(coin: str) -> bool:
    """Perpetual markets are plain tickers ("BTC", "kPEPE"); spot pairs are "PURR/USDC" or index names like "@107".
    The subnet scores perpetuals only (v5.8): the app's builder code also rides on spot orders, where the exchange
    charges it on sells, and those fills are recorded but never counted."""
    return "/" not in coin and not coin.startswith("@")


def _parse_time_ms(text: str) -> int:
    t = text.strip()
    if t.isdigit():
        return int(t)
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _parse_bool(text: str) -> bool:
    return text.strip().lower() in ("true", "1", "t", "yes")


def normalize_side(text: str) -> str:
    """The exchange writes "B"/"A" in the API and "Bid"/"Ask" in the builder files; the mechanism uses "B"/"A"."""
    t = text.strip().lower()
    if t in ("b", "bid", "buy", "long"):
        return "B"
    if t in ("a", "ask", "sell", "s", "short"):
        return "A"
    raise ValueError(f"unknown side {text!r}")


def parse_builder_csv(text: str) -> list[Fill]:
    """Parse a per-builder daily file. The header must match the published format exactly."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError("empty builder fills file") from exc
    if ",".join(h.strip() for h in header) != BUILDER_CSV_HEADER:
        raise ValueError("unexpected builder fills header")
    fills: list[Fill] = []
    for row in reader:
        if len(row) < 14 or not row[1].strip():
            continue
        user = row[1].strip().lower()
        counterparty = row[10].strip().lower()
        twap = row[12].strip()
        fills.append(
            Fill(
                time_ms=_parse_time_ms(row[0]),
                user=user,
                coin=row[2].strip(),
                side=normalize_side(row[3]),
                px=float(row[4]),
                sz=float(row[5]),
                crossed=_parse_bool(row[6]),
                builder_fee=float(row[13]),
                counterparty=counterparty,
                closed_pnl=float(row[11] or 0.0),
                twap_id=int(twap) if twap and int(twap) != 0 else None,
            )
        )
    return fills


def parse_user_fills(user: str, records: Iterable[Mapping[str, Any]]) -> list[Fill]:
    """Parse fills from the public `userFills` / `userFillsByTime` endpoints (fields documented 2026-10-19)."""
    out: list[Fill] = []
    for r in records:
        builder = float(r.get("builderFee") or 0.0)
        tid = r.get("tid")
        cloid = r.get("cloid")
        out.append(
            Fill(
                time_ms=int(r["time"]),
                user=user.lower(),
                coin=str(r["coin"]),
                side=normalize_side(str(r["side"])),
                px=float(r["px"]),
                sz=float(r["sz"]),
                crossed=bool(r.get("crossed", False)),
                builder_fee=builder,
                closed_pnl=float(r.get("closedPnl") or 0.0),
                total_fee=float(r["fee"]),
                twap_id=int(r["twapId"]) if r.get("twapId") else None,
                tid=int(tid) if tid is not None else None,
                cloid=str(cloid).lower() if cloid else None,
                source="history",
            )
        )
    return out


def match_total_fees(builder_fills: Sequence[Fill], user_fills: Sequence[Fill]) -> list[Fill]:
    """Copy the total fee of each matching public fill onto the builder fill (same user, coin, second, price, size,
    side; identical rows are matched one to one). Builder fills without a match keep `total_fee = None`; the credit
    code then counts the builder fee only, which can only lower the cap."""
    pool: dict[tuple[str, str, int, float, float, str], list[float]] = {}
    for f in user_fills:
        if f.total_fee is not None:
            pool.setdefault(f.match_key(), []).append(f.total_fee)
    out = []
    for f in builder_fills:
        fees = pool.get(f.match_key())
        out.append(replace(f, total_fee=fees.pop(0)) if fees else f)
    return out


KOKOAH_CLOID_MARKER = "0x6b6f"  # Kokoah's order ids start with ASCII "ko" (kokoah-shared, actions/cloid.ts)


def is_builder_rate(f: Fill, rate: float) -> bool:
    """The fill's builder fee is the configured rate of its notional (the exchange rounds fees to 6 decimals)."""
    expected = f.notional * rate
    return f.builder_fee > 0 and abs(f.builder_fee - expected) <= 2e-6 + 1e-4 * expected


def history_counts(f: Fill, only_builder_approved: bool, rate: float) -> bool:
    """Whether a fill seen only in the address's own history is a fill through the builder. The history carries the
    builder fee but not the builder's address. The exchange charges a builder fee only for a builder the user has
    approved, so when the scored builder is the only approved one, every builder fee is its own (at no more than
    the configured rate). Otherwise the fill must also carry Kokoah's order marker and exactly the configured rate."""
    if f.builder_fee <= 0:
        return False
    if only_builder_approved:
        return f.builder_fee <= f.notional * rate * (1 + 1e-4) + 2e-6
    return bool(f.cloid and f.cloid.startswith(KOKOAH_CLOID_MARKER)) and is_builder_rate(f, rate)


def merge_sources(
    file_fills: Sequence[Fill], history: Sequence[Fill], only_builder_approved: bool, rate: float
) -> list[Fill]:
    """One address's builder fills for a day from both sources (specification section 3).

    The exchange's per-builder daily file is incomplete (it has held about the first twelve hours of each UTC day),
    so the address's own fill history is the primary source. A fill in both is taken from the history (exact time,
    total fee, trade id) with the file's counterparty and builder fee; a fill only in the file is kept as it is
    (beyond the history's 10,000-fill reach); a fill only in the history counts when `history_counts` says so."""
    pool: dict[tuple[str, str, int, float, float, str], list[Fill]] = {}
    for f in file_fills:
        pool.setdefault(f.match_key(), []).append(f)
    out: list[Fill] = []
    for h in sorted(history, key=lambda x: (x.time_ms, x.tid or 0)):
        matches = pool.get(h.match_key())
        if matches:
            fl = matches.pop(0)
            out.append(replace(h, counterparty=fl.counterparty, builder_fee=fl.builder_fee, source="both"))
        elif history_counts(h, only_builder_approved, rate):
            out.append(h)
    out.extend(f for fs in pool.values() for f in fs)
    return sorted(out, key=lambda x: (x.time_ms, x.coin, x.side, x.px, x.sz))


def assign_counterparties(fills: Sequence[Fill], tid_owners: Mapping[int, set[str]]) -> list[Fill]:
    """Name the counterparty of history fills from the trade id: both sides of a trade carry the same id, so a
    fill whose id also appears among another known address's fills (a bound address or a linked account) traded
    with that address."""
    out = []
    for f in fills:
        if not f.counterparty and f.tid is not None:
            others = sorted(a for a in tid_owners.get(f.tid, set()) if a != f.user)
            if others:
                f = replace(f, counterparty=others[0])
        out.append(f)
    return out


def by_user(fills: Iterable[Fill]) -> dict[str, list[Fill]]:
    out: dict[str, list[Fill]] = {}
    for f in fills:
        out.setdefault(f.user, []).append(f)
    return out
