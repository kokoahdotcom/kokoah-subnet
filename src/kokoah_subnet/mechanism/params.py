"""Mechanism parameters (specification section 12).

Frozen and validated, so a scoring run cannot drift from the published values by accident.
"""

from __future__ import annotations

from dataclasses import dataclass

SPEC_VERSION = "6.4"


@dataclass(frozen=True)
class Params:
    """Every number the mechanism uses. Money is USD, rates are fractions, windows are days."""

    builder_fee_rate: float = 0.00025  # 0.025% of notional per fill
    maker_credit: float = 1.0  # maker fills earn their builder fee in full (the fee is charged on both)
    smoothing: float = 0.3  # today's weight in the daily smoothing (70/30)
    # the two pools: the whole miner emission is paid out, nothing is capped or burned
    volume_share: float = 0.80  # shared by eligible smoothed builder fees
    skill_share: float = 0.20  # shared by skill weight; empty: joins the volume pool
    # skill score (section 7)
    return_cap: float = 0.10
    confidence_days: int = 60
    quality_days: int = 30
    quality_half_life_days: float = 10.0
    psr_benchmark: float = 0.03  # benchmark daily Sharpe ratio
    psr_min_days: int = 10
    gpr_full: float = 1.2  # gain-to-pain of 2.2 scores a full quality
    blowup_threshold: float = -0.10
    blowup_count: int = 2
    blowup_days: int = 60
    g_full: float = 0.65
    # skill pool
    skill_floor: float = 0.25  # s = max(0, (g' - floor) / (1 - floor))
    skill_volume_cap_usd: float = 1_000_000.0  # v = min(1, eligible Kokoah notional over the window / cap)
    skill_volume_days: int = 30
    entry_days: int = 90  # entry test: PSR of the capped daily returns over this many days
    entry_psr: float = 0.95  # must reach this (benchmark `psr_benchmark`)
    agent_bonus: float = 0.10  # skill weight x (1 + bonus) for a verified sealed-machine agent account
    # activity gates (section 9): for the skill pool
    gate_days: int = 30
    gate_round_trips: int = 10
    gate_active_days: int = 7
    gate_mean_equity_usd: float = 500.0
    # eligibility (section 9a): no emission on a day the account is below the floor or before qualifying
    min_equity_usd: float = 10_000.0  # the day's time-weighted average account value
    equity_window: str = "perpWeek"  # the exchange's portfolio history read for the average (perps account value)
    equity_origin_window: str = "perpAllTime"  # its first point marks when the account's history begins
    equity_min_points: int = 4  # fewer recorded points inside the day: the average is unknown (never estimated)
    qualify_trades: int = 3  # a new identity's first trades: qualifying trades needed
    qualify_notional_usd: float = 1_000.0  # and their eligible notional
    # data plane (section 3): the builder file is incomplete, the address's own history is the primary source
    file_deadline_hours: float = 12.0  # after the day ends: no file by then, the day is scored from the histories
    max_linked_accounts: int = 25  # linked accounts per address whose trades are read to name counterparties
    # exclusions (section 3)
    mark_clip: float = 0.005
    mirror_window_s: float = 60.0
    mirror_size_tolerance: float = 0.20
    mirror_correlation: float = -0.8
    mirror_min_days: int = 10
    mirror_exclude_share: float = 0.25
    mirror_zero_share: float = 0.50
    mirror_zero_days: int = 30
    thin_market_share: float = 0.25  # above this share of a coin's day volume, the address's own trades set its marks
    # calendar: day index 0 (penalty windows count from here; the same for every validator)
    day_zero: str = "2026-10-19"
    # chain (section 1)
    daily_emission_alpha: float = 7200.0
    miner_slot: float = 0.41  # owner 0.18, validators 0.41, miners 0.41 (the chain's default)
    price_samples: int = 20

    def __post_init__(self) -> None:
        if not 0 < self.smoothing <= 1:
            raise ValueError("smoothing must be in (0, 1]")
        if abs(self.volume_share + self.skill_share - 1.0) > 1e-9 or min(self.volume_share, self.skill_share) < 0:
            raise ValueError("volume_share + skill_share must be 1, both non-negative")
        if self.builder_fee_rate <= 0:
            raise ValueError("builder_fee_rate must be positive")
        if not 0 < self.maker_credit <= 1:
            raise ValueError("maker_credit must be in (0, 1]")
        if not 0 <= self.skill_floor < 1:
            raise ValueError("skill_floor must be in [0, 1)")
        if self.skill_volume_cap_usd <= 0 or self.agent_bonus < 0:
            raise ValueError("skill_volume_cap_usd must be positive and agent_bonus non-negative")
        if not 0.5 < self.entry_psr < 1 or self.entry_days < self.psr_min_days:
            raise ValueError("entry_psr must be in (0.5, 1) and entry_days at least psr_min_days")
        if self.min_equity_usd < 0 or self.qualify_trades < 1 or self.qualify_notional_usd < 0:
            raise ValueError("eligibility thresholds must be non-negative and at least one trade")
        if self.equity_min_points < 2:
            raise ValueError("equity_min_points must be at least 2")

    @property
    def builder_fee_bps(self) -> float:
        return self.builder_fee_rate * 1e4

    @property
    def miner_pool_alpha(self) -> float:
        return self.daily_emission_alpha * self.miner_slot


DEFAULT = Params()
