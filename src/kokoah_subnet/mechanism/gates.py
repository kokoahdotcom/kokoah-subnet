"""Activity gates (specification section 9) and the counts they need."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from .fills import Fill
from .params import Params


def round_trips(fills: Sequence[Fill], start_positions: Mapping[str, float] | None = None) -> int:
    """Completed round trips: each time a coin's position returns to zero or flips sign. Fills are sorted by time;
    `start_positions` are the signed sizes held before the first fill (default flat)."""
    pos: dict[str, float] = dict(start_positions or {})
    count = 0
    for f in sorted(fills, key=lambda x: x.time_ms):
        before = pos.get(f.coin, 0.0)
        after = before + f.signed_size
        if before != 0 and (abs(after) < 1e-12 or (before > 0) != (after > 0)):
            count += 1
        pos[f.coin] = 0.0 if abs(after) < 1e-12 else after
    return count


def active_days(fills: Iterable[Fill]) -> int:
    return len({f.day for f in fills})


def passes_gates(round_trip_count: int, active_day_count: int, mean_equity_usd: float, params: Params) -> bool:
    return (
        round_trip_count >= params.gate_round_trips
        and active_day_count >= params.gate_active_days
        and mean_equity_usd >= params.gate_mean_equity_usd
    )
