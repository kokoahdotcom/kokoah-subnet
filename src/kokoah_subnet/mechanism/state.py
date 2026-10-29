"""Per-miner smoothed state carried from day to day (specification sections 4, 6, 7, 8, 14)."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .credit import DayFees, ema
from .params import Params

PAY_HISTORY_DAYS = 60  # the pay history kept per hotkey


@dataclass(frozen=True)
class MinerState:
    """Everything carried from day to day for one hotkey. Credit and qualification belong to one identity and are
    dropped when it changes (`eligibility.fresh_identity`); pay history, skill and penalties are kept."""

    phi_hat: float = 0.0  # smoothed fee credit, USD
    fee_hat: float = 0.0  # smoothed total fees on the builder leg, USD
    g: float = 0.0  # smoothed skill
    paid: tuple[float, ...] = ()  # alpha paid per scored day, newest last (`paid_days` is authoritative)
    zero_until_day: int | None = None  # day index until which the miner is out of the skill pool (mirror report)
    paid_days: tuple[tuple[int, float], ...] = ()  # (day index, alpha paid), so the window follows calendar days
    last_day: int | None = None  # the last day index this state was advanced on
    # eligibility (section 9a): belongs to one identity (hotkey + bound address + registration), never carried over
    qualified: bool = False
    qual_trades: int = 0
    qual_notional: float = 0.0
    address: str | None = None  # the bound address the credit was earned with
    registered_block: int | None = None  # the hotkey's registration the credit was earned under

    def paid_window(self, day_index: int | None = None, days: int = 60) -> float:
        """Alpha paid in the last `days` calendar days before `day_index` (all stored days when no index is given)."""
        if not self.paid_days:
            return sum(self.paid)
        if day_index is None:
            return sum(a for _, a in self.paid_days)
        return sum(a for d, a in self.paid_days if day_index - days < d <= day_index)

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
    day_index: int | None = None,
) -> MinerState:
    """Roll the state one day forward: smooth credit, total fees and skill, append today's pay, apply a mirror
    report (`zero_from_day` is today's index when the address is reported)."""
    paid = (*state.paid, paid_alpha)[-PAY_HISTORY_DAYS:]
    zero_until = state.zero_until_day
    if zero_from_day is not None:
        zero_until = max(zero_until or 0, zero_from_day + params.mirror_zero_days)
    paid_days = state.paid_days
    if day_index is not None:
        paid_days = tuple(
            (d, a) for d, a in (*paid_days, (day_index, paid_alpha)) if day_index - PAY_HISTORY_DAYS < d <= day_index
        )
    return replace(
        state,
        phi_hat=ema(state.phi_hat, fees.credit * credit_factor, params),
        fee_hat=ema(state.fee_hat, fees.total_fee, params),
        g=ema(state.g, g_raw, params),
        paid=paid,
        zero_until_day=zero_until,
        paid_days=paid_days,
        last_day=day_index if day_index is not None else state.last_day,
    )
