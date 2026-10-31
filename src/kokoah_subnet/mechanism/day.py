"""One scoring day, end to end, as a pure function (the specification).

`score_day` takes everything a validator fetched for the day and returns the allocation, the weights and the new
per-miner states. It never touches a network, so a replay of recorded days is the same code path as production.

Per miner: the day's fills after the exclusions (§3); eligibility and qualification (§9a); the credited part of
the day smoothed into `Φ̂` (§4); the skill score smoothed into `g` (§7). Then the two pools (§5): the volume pool
shared by `Φ̂` among eligible miners, the skill pool shared by skill weight among miners that pass its entry rules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from .credit import DayFees, day_fees
from .eligibility import Qualification, fresh_identity, identity_change, qualify
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
from .params import Params
from .payout import Allocation, allocate, weights
from .skill import SkillParts, skill_factor, skill_raw
from .skillpool import (
    adjusted_skill,
    entry_psr,
    skill_weight,
    volume_factor,
)
from .state import MinerState, advance


@dataclass(frozen=True)
class MinerInputs:
    """What the validator fetched about one bound address for the day."""

    hotkey: str
    address: str  # lowercase
    returns: Sequence[float] = ()  # daily returns, newest last, flows removed, uncapped
    mean_equity_usd: float = 0.0  # over the gate window
    staked_min_alpha: float = 0.0  # recorded for information
    collateral_min_alpha: float = 0.0  # recorded
    collateral_locked_alpha: float = 0.0  # recorded
    window_fills: Sequence[Fill] = ()  # the gate window's builder-code fills (for round trips and active days)
    start_positions: Mapping[str, float] = field(default_factory=dict)
    exchange_rates_bps: tuple[float, float] | None = None  # (taker, maker) fallback for unknown total fees
    round_trips_window: int | None = None  # precomputed gate counts (the live loop); else from window_fills
    active_days_window: int | None = None
    # eligibility (section 9a)
    avg_equity_usd: float | None = None  # the day's time-weighted average account value; None = unknown (ineligible)
    registered_block: int | None = None  # the hotkey's registration block (a new registration is a fresh identity)
    # skill pool
    credited_notional_prior_usd: float = 0.0  # credited Kokoah notional over the window's earlier days
    agent_verified: bool = False  # a verified sealed-machine agent account (the skill bonus)


@dataclass(frozen=True)
class DayInputs:
    day_index: int
    price_usd: float  # the day's average alpha price in USD (recorded: pay in USD)
    fills: Sequence[Fill]  # the day's builder-code fills, total fees matched where possible
    miners: Sequence[MinerInputs]
    states: Mapping[str, MinerState]  # by hotkey; missing hotkeys start fresh
    cluster_of: Mapping[str, str] = field(default_factory=dict)
    positions: Mapping[str, Mapping[str, Sequence[float]]] = field(default_factory=dict)  # mirror rule inputs
    # address -> its owner's group (hotkeys on one coldkey, a master and its sub-accounts): the position mirror rule
    # compares only addresses in one group
    owner_group_of: Mapping[str, str] = field(default_factory=dict)
    pool_alpha: float | None = None
    penalized_until: Mapping[str, int] = field(default_factory=dict)  # address -> day index: a report follows it


@dataclass(frozen=True)
class MinerRecord:
    """The published per-miner artifact of the day."""

    hotkey: str
    address: str
    fees: DayFees  # everything traded today (recorded)
    credited: DayFees  # the part that earned credit (eligible trades only)
    mirrored_share: float
    credit_factor: float
    reported: bool
    eligible: bool
    ineligible_reason: str | None
    avg_equity_usd: float | None
    qualified: bool
    qual_trades: int
    qual_notional: float
    qualified_at_ms: int | None
    identity_reset: str | None
    phi_hat: float
    fee_hat: float
    # skill
    confidence: float
    quality: float
    skill_raw: float
    g: float
    skill: float  # g' in [0, 1]
    gates_pass: bool
    round_trips_window: int
    active_days_window: int
    returns_days: int
    zeroed: bool  # a mirror penalty is active: no skill pool
    # skill pool
    entry_psr: float | None
    entry_pass: bool
    skill_adjusted: float  # s
    volume_window_usd: float  # V over the window
    volume_factor: float  # v
    agent_verified: bool
    skill_weight: float
    skill_pool_reason: str | None  # why not in the skill pool
    # pay
    volume_pay_alpha: float
    skill_pay_alpha: float
    pay_alpha: float
    counterparty_share: float = 0.0
    position_share: float = 0.0


@dataclass(frozen=True)
class DayResult:
    day_index: int
    allocation: Allocation
    records: list[MinerRecord]
    states: dict[str, MinerState]

    def weights(self, fallback_hotkey: str) -> dict[str, float]:
        return weights(self.allocation, fallback_hotkey)


@dataclass
class _Work:
    m: MinerInputs
    state: MinerState
    reset: str | None
    fees: DayFees
    credited: DayFees
    qual: Qualification
    eligible: bool
    reason: str | None
    mirrored: float
    credit_factor: float
    reported: bool
    zeroed: bool
    parts: SkillParts
    g: float
    skill: float
    gates: bool
    rt: int
    ad: int
    phi_hat: float
    fee_hat: float
    cp_share: float
    pos_share: float
    psr: float | None
    entry: bool
    s: float
    volume_window: float
    v: float
    weight: float
    pool_reason: str | None


def score_day(inputs: DayInputs, params: Params) -> DayResult:
    # exclusions on the day's fills
    perps = [f for f in inputs.fills if is_perp(f.coin)]  # perpetuals only; spot fills count for nothing
    fills = clip_to_mark(drop_cluster_matches(perps, inputs.cluster_of), params)
    bound = {m.address for m in inputs.miners}
    bound_fills = {a: f for a, f in by_user(fills).items() if a in bound}
    against_bound = bound_counterparty_notional(bound_fills)  # measured before the fills are dropped
    bound_fills = {a: drop_bound_counterparties(f, bound) for a, f in bound_fills.items()}
    position_share = mirror_position_share(inputs.positions, params, inputs.owner_group_of)

    work: list[_Work] = []
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
        cp_share = against_bound.get(m.address, 0.0) / gross if gross > 0 else 0.0  # recorded: removed, not reported
        pos_share = position_share.get(m.address, 0.0)
        mirrored = pos_share
        action = mirror_action(mirrored, params)
        zeroed = (
            action.zero_multiplier
            or state.is_zeroed(inputs.day_index)
            or inputs.penalized_until.get(m.address, -1) > inputs.day_index
        )
        parts = skill_raw(m.returns, params)
        g = (1 - params.smoothing) * state.g + params.smoothing * parts.raw
        skill = skill_factor(g, params)
        rt = (
            m.round_trips_window if m.round_trips_window is not None else round_trips(m.window_fills, m.start_positions)
        )
        ad = m.active_days_window if m.active_days_window is not None else active_days(m.window_fills)
        gates = passes_gates(rt, ad, m.mean_equity_usd, params)
        phi_hat = (1 - params.smoothing) * state.phi_hat + params.smoothing * credited.credit * action.credit_factor
        fee_hat = (1 - params.smoothing) * state.fee_hat + params.smoothing * credited.total_fee
        # the skill pool
        psr = entry_psr(m.returns, params)
        entry = psr is not None and psr >= params.entry_psr
        s = adjusted_skill(skill, params)
        volume_window = m.credited_notional_prior_usd + credited.notional
        v = volume_factor(volume_window, params)
        pool_reason = None
        if not eligible:
            pool_reason = "not eligible today"
        elif zeroed:
            pool_reason = "mirror penalty"
        elif not gates:
            pool_reason = "activity gates"
        elif not entry:
            pool_reason = "entry test" if psr is not None else f"fewer than {params.entry_days} days of returns"
        elif s <= 0:
            pool_reason = "skill at or below the floor"
        elif v <= 0:
            pool_reason = "no Kokoah volume in the window"
        weight = skill_weight(skill, volume_window, m.agent_verified, params) if pool_reason is None else 0.0
        work.append(
            _Work(
                m, state, reset, fees, credited, qual, eligible, reason, mirrored, action.credit_factor,
                action.zero_multiplier, zeroed, parts, g, skill, gates, rt, ad, phi_hat, fee_hat, cp_share,
                pos_share, psr, entry, s, volume_window, v, weight, pool_reason,
            )
        )  # fmt: skip

    skill_weights = {w.m.hotkey: w.weight for w in work}
    volume_weights = {w.m.hotkey: w.phi_hat for w in work if w.eligible}
    allocation = allocate(volume_weights, skill_weights, inputs.price_usd, params, inputs.pool_alpha)

    records: list[MinerRecord] = []
    states: dict[str, MinerState] = dict(inputs.states)
    for w in work:
        vp = allocation.volume_pays.get(w.m.hotkey, 0.0)
        sp = allocation.skill_pays.get(w.m.hotkey, 0.0)
        q = w.qual
        records.append(
            MinerRecord(
                hotkey=w.m.hotkey,
                address=w.m.address,
                fees=w.fees,
                credited=w.credited,
                mirrored_share=w.mirrored,
                credit_factor=w.credit_factor,
                reported=w.reported,
                eligible=w.eligible,
                ineligible_reason=w.reason,
                avg_equity_usd=w.m.avg_equity_usd,
                qualified=q.qualified,
                qual_trades=q.trades,
                qual_notional=q.notional,
                qualified_at_ms=q.qualified_at_ms,
                identity_reset=w.reset,
                phi_hat=w.phi_hat,
                fee_hat=w.fee_hat,
                confidence=w.parts.confidence,
                quality=w.parts.quality,
                skill_raw=w.parts.raw,
                g=w.g,
                skill=w.skill,
                gates_pass=w.gates,
                round_trips_window=w.rt,
                active_days_window=w.ad,
                returns_days=len(w.m.returns),
                zeroed=w.zeroed,
                entry_psr=w.psr,
                entry_pass=w.entry,
                skill_adjusted=w.s,
                volume_window_usd=w.volume_window,
                volume_factor=w.v,
                agent_verified=w.m.agent_verified,
                skill_weight=skill_weights.get(w.m.hotkey, 0.0),
                skill_pool_reason=w.pool_reason,
                volume_pay_alpha=vp,
                skill_pay_alpha=sp,
                pay_alpha=vp + sp,
                counterparty_share=w.cp_share,
                position_share=w.pos_share,
            )
        )
        advanced = advance(
            w.state,
            w.credited,
            w.parts.raw,
            vp + sp,
            params,
            credit_factor=w.credit_factor,
            zero_from_day=inputs.day_index if w.reported else None,
            day_index=inputs.day_index,
        )
        states[w.m.hotkey] = replace(
            advanced,
            qualified=q.qualified,
            qual_trades=q.trades,
            qual_notional=q.notional,
            address=w.m.address,
            registered_block=w.m.registered_block or w.state.registered_block,
        )

    return DayResult(inputs.day_index, allocation, records, states)
