"""Daily fee credit and the smoothing (specification sections 4 and 6)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .fills import Fill
from .params import Params


@dataclass(frozen=True)
class DayFees:
    """One address's builder-code activity on one day, in USD."""

    credit: float  # Φ: taker builder fees fully, maker builder fees at the maker credit
    builder_fee: float  # Σ b_f
    total_fee: float  # Σ fee_f: exchange fee plus builder fee (rebates lower it)
    notional: float
    taker_notional: float
    fills: int
    closed_pnl: float = 0.0  # Σ closedPnl of the fills: published in the ranking, never used for pay

    @staticmethod
    def zero() -> DayFees:
        return DayFees(0.0, 0.0, 0.0, 0.0, 0.0, 0)


def day_fees(fills: Iterable[Fill], params: Params, fallback_rates_bps: tuple[float, float] | None = None) -> DayFees:
    """Sum one address's fills for one day.

    `fallback_rates_bps` = (taker, maker) exchange rates for fills whose total fee is unknown; without it an
    unknown total fee counts the builder fee only, which can only lower the second cap.
    """
    credit = builder = total = notional = taker_notional = pnl = 0.0
    n = 0
    for f in fills:
        n += 1
        notional += f.notional
        pnl += f.closed_pnl
        builder += f.builder_fee
        credit += f.builder_fee * (1.0 if f.crossed else params.maker_credit)
        if f.crossed:
            taker_notional += f.notional
        if f.total_fee is not None:
            total += f.total_fee
        elif fallback_rates_bps is not None:
            rate = fallback_rates_bps[0] if f.crossed else fallback_rates_bps[1]
            total += f.builder_fee + f.notional * rate / 1e4
        else:
            total += f.builder_fee
    return DayFees(credit, builder, total, notional, taker_notional, n, pnl)


def ema(previous: float, today: float, params: Params) -> float:
    """`(1 − α) × previous + α × today`: 30% today, 21% tomorrow, 15% the day after; the weights sum to one."""
    a = params.smoothing
    return (1 - a) * previous + a * today
