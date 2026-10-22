"""One scoring day, end to end, as a pure function (specification sections 3 to 9, appendix B of the whitepaper).

`score_day` takes everything a validator fetched for the day and returns the allocation, the weights and the new
per-miner states. It never touches a network, so a replay of recorded days is the same code path as production.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .credit import DayFees, day_fees
from .eligibility import fresh_identity, identity_change, qualify
from .exclusions import (
    bound_counterparty_notional,
    clip_to_mark,
    drop_bound_counterparties,
    drop_cluster_matches,
    mirror_action,
    mirror_position_share,
)
from .fills import Fill, by_user, is_perp
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
    collateral_min_alpha: float = 0.0  # recorded; not used by the holding factor since v5.8
    collateral_locked_alpha: float = 0.0  # registration collateral locked on the hotkey (does not count as holding)
    window_fills: Sequence[Fill] = ()  # the gate window's builder-code fills (for round trips and active days)
    start_positions: Mapping[str, float] = field(default_factory=dict)
    exchange_rates_bps: tuple[float, float] | None = None  # (taker, maker) fallback for unknown total fees
    round_trips_window: int | None = None  # precomputed gate counts (the live loop); else from window_fills
    active_days_window: int | None = None
    # eligibility (section 9a)
    avg_equity_usd: float | None = None  # the day's time-weighted average account value; None = unknown (ineligible)
    registered_block: int | None = None  # the hotkey's registration block (a new registration is a fresh identity)


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
    penalized_until: Mapping[str, int] = field(
        default_factory=dict
    )  # address -> day index: a report follows the address


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
    # the parts behind the figures above (published, for the record)
    confidence: float = 0.0
    quality: float = 0.0
    g: float = 0.0
    phi_hat: float = 0.0
    fee_hat: float = 0.0
    counterparty_share: float = 0.0
    position_share: float = 0.0
    zeroed: bool = False
    round_trips_window: int = 0
    active_days_window: int = 0
    returns_days: int = 0
    cap_usd: float = 0.0
    # eligibility (section 9a)
    eligible: bool = True
    ineligible_reason: str | None = None
    avg_equity_usd: float | None = None
    qualified: bool = False
    qual_trades: int = 0
    qual_notional: float = 0.0
    qualified_at_ms: int | None = None
    credited: DayFees = field(default_factory=DayFees.zero)  # the part of today's fills that earned credit
    identity_reset: str | None = None


@dataclass(frozen=True)
class DayResult:
    day_index: int
    allocation: Allocation
    records: list[MinerRecord]
    states: dict[str, MinerState]

    def weights(self, burn_hotkey: str) -> dict[str, float]:
        return weights(self.allocation, burn_hotkey)


def score_day(inputs: DayInputs, params: Params) -> DayResult:
    # exclusions on the day's fills
    perps = [f for f in inputs.fills if is_perp(f.coin)]  # perpetuals only; spot fills count for nothing
    fills = clip_to_mark(drop_cluster_matches(perps, inputs.cluster_of), params)
    bound = {m.address for m in inputs.miners}
    bound_fills = {a: f for a, f in by_user(fills).items() if a in bound}
    against_bound = bound_counterparty_notional(bound_fills)  # measured before the fills are dropped
    bound_fills = {a: drop_bound_counterparties(f, bound) for a, f in bound_fills.items()}
    position_share = mirror_position_share(inputs.positions, params)

    miner_days: list[MinerDay] = []
    partial: list[tuple[Any, ...]] = []
    for m in inputs.miners:
        state = inputs.states.get(m.hotkey, MinerState())
        reset = identity_change(state, m.address, m.registered_block)
        if reset is not None:
            state = fresh_identity(state)  # credit and qualification never pass to a fresh identity
        own = bound_fills.get(m.address, [])
        fees = day_fees(own, params, m.exchange_rates_bps)  # everything traded today, recorded
        qual = qualify(own, state.qualified, state.qual_trades, state.qual_notional, params)
        equity_ok = m.avg_equity_usd is not None and m.avg_equity_usd >= params.min_equity_usd
        eligible = qual.qualified and equity_ok
        reason = None
        if not qual.qualified:
            reason = "not yet qualified"
        elif m.avg_equity_usd is None:
            reason = "average equity unknown"
        elif not equity_ok:
            reason = "average equity below the minimum"
        # only trades made while eligible earn credit; an ineligible day adds nothing and the smoothing decays
        credited = day_fees(qual.credited, params, m.exchange_rates_bps) if eligible else DayFees.zero()
        gross = fees.notional + against_bound.get(m.address, 0.0)
        counterparty_share = against_bound.get(m.address, 0.0) / gross if gross > 0 else 0.0
        mirrored = max(counterparty_share, position_share.get(m.address, 0.0))
        action = mirror_action(mirrored, params)
        zeroed = (
            action.zero_multiplier
            or state.is_zeroed(inputs.day_index)
            or inputs.penalized_until.get(m.address, -1) > inputs.day_index
        )
        parts = skill_raw(m.returns, params)
        g_smoothed = (1 - params.smoothing) * state.g + params.smoothing * parts.raw
        rt = (
            m.round_trips_window if m.round_trips_window is not None else round_trips(m.window_fills, m.start_positions)
        )
        ad = m.active_days_window if m.active_days_window is not None else active_days(m.window_fills)
        gates = passes_gates(rt, ad, m.mean_equity_usd, params)

        skill = 0.0 if zeroed else skill_factor(g_smoothed, params)
        hold = (
            0.0
            if zeroed
            else holding_factor(
                m.staked_min_alpha,
                state.paid_window(inputs.day_index, params.holding_days),
                m.collateral_locked_alpha,
            )
        )
        mult = multiplier(skill, hold, gates, params)
        phi_hat = (1 - params.smoothing) * state.phi_hat + params.smoothing * credited.credit * action.credit_factor
        fee_hat = (1 - params.smoothing) * state.fee_hat + params.smoothing * credited.total_fee
        if eligible:
            miner_days.append(MinerDay(m.hotkey, phi_hat, fee_hat, mult))  # an ineligible miner takes no share
        partial.append(
            (
                m,
                state,
                fees,
                credited,
                qual,
                eligible,
                reason,
                reset,
                mirrored,
                action.credit_factor,
                action.zero_multiplier,
                parts.raw,
                skill,
                hold,
                gates,
                mult,
                parts,
                g_smoothed,
                phi_hat,
                fee_hat,
                counterparty_share,
                position_share.get(m.address, 0.0),
                zeroed,
                rt,
                ad,
            )
        )

    allocation = allocate(miner_days, inputs.price_usd, params, inputs.pool_alpha)

    records: list[MinerRecord] = []
    states: dict[str, MinerState] = dict(inputs.states)
    for (
        m,
        state,
        fees,
        credited,
        qual,
        eligible,
        reason,
        reset,
        mirrored,
        credit_factor,
        reported,
        raw,
        skill,
        hold,
        gates,
        mult,
        parts,
        g_s,
        phi_hat,
        fee_hat,
        cp_share,
        pos_share,
        zeroed,
        rt,
        ad,
    ) in partial:
        pay = allocation.pays.get(m.hotkey, 0.0)
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
                allocation.shares.get(m.hotkey, 0.0),
                allocation.caps.get(m.hotkey, 0.0),
                pay,
                confidence=parts.confidence,
                quality=parts.quality,
                g=g_s,
                phi_hat=phi_hat,
                fee_hat=fee_hat,
                counterparty_share=cp_share,
                position_share=pos_share,
                zeroed=zeroed,
                round_trips_window=rt,
                active_days_window=ad,
                returns_days=len(m.returns),
                cap_usd=allocation.caps.get(m.hotkey, 0.0) * inputs.price_usd,
                eligible=eligible,
                ineligible_reason=reason,
                avg_equity_usd=m.avg_equity_usd,
                qualified=qual.qualified,
                qual_trades=qual.trades,
                qual_notional=qual.notional,
                qualified_at_ms=qual.qualified_at_ms,
                credited=credited,
                identity_reset=reset,
            )
        )
        advanced = advance(
            state,
            credited,
            raw,
            pay,
            params,
            credit_factor=credit_factor,
            zero_from_day=inputs.day_index if reported else None,
            day_index=inputs.day_index,
        )
        states[m.hotkey] = replace(
            advanced,
            qualified=qual.qualified,
            qual_trades=qual.trades,
            qual_notional=qual.notional,
            address=m.address,
            registered_block=m.registered_block or state.registered_block,
        )

    return DayResult(inputs.day_index, allocation, records, states)
