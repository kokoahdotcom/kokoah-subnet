"""One scoring day, end to end, as a pure function (specification sections 3 to 9, appendix B of the whitepaper).

`score_day` takes everything a validator fetched for the day and returns the allocation, the weights and the new
per-miner states. It never touches a network, so a replay of recorded days is the same code path as production.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .credit import DayFees, day_fees
from .exclusions import (
    clip_to_mark,
    drop_cluster_matches,
    mirror_action,
    mirror_position_share,
    mirror_timing_notional,
)
from .fills import Fill, by_user
from .gates import active_days, passes_gates, round_trips
from .holding import holding_factor
from .params import Params
from .payout import Allocation, MinerDay, allocate, multiplier, weights
from .skill import skill_factor, skill_raw
from .state import MinerState, advance


@dataclass(frozen=True)
class MinerInputs:
    """What the validator fetched about one bound address for the day."""

    hotkey: str
    address: str  # lowercase
    returns: Sequence[float] = ()  # daily returns, newest last, flows removed, uncapped
    mean_equity_usd: float = 0.0  # over the gate window
    staked_min_alpha: float = 0.0  # the day's minimum own stake on the hotkey
    collateral_min_alpha: float = 0.0
    window_fills: Sequence[Fill] = ()  # the gate window's builder-code fills (for round trips and active days)
    start_positions: Mapping[str, float] = field(default_factory=dict)
    exchange_rates_bps: tuple[float, float] | None = None  # (taker, maker) fallback for unknown total fees


@dataclass(frozen=True)
class DayInputs:
    day_index: int
    price_usd: float  # the day's average alpha price in USD
    fills: Sequence[Fill]  # the day's builder-code fills, total fees matched where possible
    miners: Sequence[MinerInputs]
    states: Mapping[str, MinerState]  # by hotkey; missing hotkeys start fresh
    cluster_of: Mapping[str, str] = field(default_factory=dict)
    positions: Mapping[str, Mapping[str, Sequence[float]]] = field(default_factory=dict)  # mirror rule inputs
    pool_alpha: float | None = None


@dataclass(frozen=True)
class MinerRecord:
    """The published per-miner artifact of the day."""

    hotkey: str
    address: str
    fees: DayFees
    mirrored_share: float
    credit_factor: float
    reported: bool
    skill_raw: float
    skill: float
    holding: float
    gates_pass: bool
    multiplier: float
    share_alpha: float
    cap_alpha: float
    pay_alpha: float


@dataclass(frozen=True)
class DayResult:
    day_index: int
    allocation: Allocation
    records: list[MinerRecord]
    states: dict[str, MinerState]

    def weights(self, owner_hotkey: str) -> dict[str, float]:
        return weights(self.allocation, owner_hotkey)


def score_day(inputs: DayInputs, params: Params) -> DayResult:
    # exclusions on the day's fills
    fills = clip_to_mark(drop_cluster_matches(inputs.fills, inputs.cluster_of), params)
    per_address = by_user(fills)
    bound = {m.address for m in inputs.miners}
    bound_fills = {a: f for a, f in per_address.items() if a in bound}
    timing = mirror_timing_notional(bound_fills, params)
    position_share = mirror_position_share(inputs.positions, params)

    miner_days: list[MinerDay] = []
    partial: list[tuple[MinerInputs, MinerState, DayFees, float, float, bool, float, float, float, bool, float]] = []
    for m in inputs.miners:
        state = inputs.states.get(m.hotkey, MinerState())
        own = bound_fills.get(m.address, [])
        fees = day_fees(own, params, m.exchange_rates_bps)
        day_notional = fees.notional
        timing_share = timing.get(m.address, 0.0) / day_notional if day_notional > 0 else 0.0
        mirrored = max(timing_share, position_share.get(m.address, 0.0))
        action = mirror_action(mirrored, params)
        zeroed = action.zero_multiplier or state.is_zeroed(inputs.day_index)
        parts = skill_raw(m.returns, params)
        g_smoothed = (1 - params.smoothing) * state.g + params.smoothing * parts.raw
        gates = passes_gates(
            round_trips(m.window_fills, m.start_positions), active_days(m.window_fills), m.mean_equity_usd, params
        )
        skill = 0.0 if zeroed else skill_factor(g_smoothed, params)
        hold = 0.0 if zeroed else holding_factor(m.staked_min_alpha, state.paid_window(), m.collateral_min_alpha)
        mult = multiplier(skill, hold, gates, params)
        phi_hat = (1 - params.smoothing) * state.phi_hat + params.smoothing * fees.credit * action.credit_factor
        fee_hat = (1 - params.smoothing) * state.fee_hat + params.smoothing * fees.total_fee
        miner_days.append(MinerDay(m.hotkey, phi_hat, fee_hat, mult))
        partial.append(
            (
                m,
                state,
                fees,
                mirrored,
                action.credit_factor,
                action.zero_multiplier,
                parts.raw,
                skill,
                hold,
                gates,
                mult,
            )
        )

    allocation = allocate(miner_days, inputs.price_usd, params, inputs.pool_alpha)

    records: list[MinerRecord] = []
    states: dict[str, MinerState] = dict(inputs.states)
    for m, state, fees, mirrored, credit_factor, reported, raw, skill, hold, gates, mult in partial:
        pay = allocation.pays[m.hotkey]
        records.append(
            MinerRecord(
                m.hotkey,
                m.address,
                fees,
                mirrored,
                credit_factor,
                reported,
                raw,
                skill,
                hold,
                gates,
                mult,
                allocation.shares[m.hotkey],
                allocation.caps[m.hotkey],
                pay,
            )
        )
        states[m.hotkey] = advance(
            state,
            fees,
            raw,
            pay,
            params,
            credit_factor=credit_factor,
            zero_from_day=inputs.day_index if reported else None,
        )
    return DayResult(inputs.day_index, allocation, records, states)
