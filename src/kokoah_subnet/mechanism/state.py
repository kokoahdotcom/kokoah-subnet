"""Per-miner smoothed state carried from day to day (specification sections 4, 6, 7, 8, 14)."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .credit import DayFees, ema
from .params import Params


@dataclass(frozen=True)
class MinerState:
    """Everything that belongs to the binding and survives a migration."""

    phi_hat: float = 0.0  # smoothed fee credit, USD
    fee_hat: float = 0.0  # smoothed total fees on the builder leg, USD
    g: float = 0.0  # smoothed skill
    paid: tuple[float, ...] = ()  # alpha paid per day, newest last, at most `holding_days` entries
    zero_until_day: int | None = None  # day index until which g' = h' = 0 (mirror report)

    def paid_window(self) -> float:
        return sum(self.paid)

    def is_zeroed(self, day_index: int) -> bool:
        return self.zero_until_day is not None and day_index < self.zero_until_day


def advance(
    state: MinerState,
    fees: DayFees,
    g_raw: float,
    paid_alpha: float,
    params: Params,
    credit_factor: float = 1.0,
    zero_from_day: int | None = None,
) -> MinerState:
    """Roll the state one day forward: smooth credit, total fees and skill, append today's pay, apply a mirror
    report (`zero_from_day` is today's index when the address is reported)."""
    paid = (*state.paid, paid_alpha)[-params.holding_days :]
    zero_until = state.zero_until_day
    if zero_from_day is not None:
        zero_until = max(zero_until or 0, zero_from_day + params.mirror_zero_days)
    return replace(
        state,
        phi_hat=ema(state.phi_hat, fees.credit * credit_factor, params),
        fee_hat=ema(state.fee_hat, fees.total_fee, params),
        g=ema(state.g, g_raw, params),
        paid=paid,
        zero_until_day=zero_until,
    )
