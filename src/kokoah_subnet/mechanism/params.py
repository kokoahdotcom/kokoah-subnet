"""Mechanism parameters (specification v5.6, section 12).

Frozen and validated, so a scoring run cannot drift from the published values by accident.
"""

from __future__ import annotations

from dataclasses import dataclass

SPEC_VERSION = "5.8"

# The best maker rebate anywhere on Hyperliquid, bps a side (fee schedule read 2026-10-19). It bounds the smallest
# total fee a round trip through the builder can carry, and with it the safe range of `c_tot` (spec section 11).
BEST_REBATE_BPS = 0.3


@dataclass(frozen=True)
class Params:
    """Every number the mechanism uses. Money is USD, rates are fractions, windows are days."""

    builder_fee_rate: float = 0.00025  # 0.025% of notional per fill
    maker_credit: float = 1.0  # maker fills earn their builder fee in full (the fee is charged on both; v5.7)
    smoothing: float = 0.3  # today's weight in the daily smoothing (70/30)
    c_max: float = 2.0  # first cap: multiple of the smoothed fee credit
    c_tot: float = 0.75  # second cap: share of the smoothed total fees on the builder leg
    m_min: float = 0.85  # multiplier floor
    w_skill: float = 0.05
    w_hold: float = 0.10
    # skill factor (section 7)
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
    # holding factor (section 8)
    holding_days: int = 60
    # activity gates (section 9)
    gate_days: int = 30
    gate_round_trips: int = 10
    gate_active_days: int = 7
    gate_mean_equity_usd: float = 500.0
    # exclusions (section 3)
    mark_clip: float = 0.005
    mirror_window_s: float = 60.0
    mirror_size_tolerance: float = 0.20
    mirror_correlation: float = -0.8
    mirror_min_days: int = 10
    mirror_exclude_share: float = 0.25
    mirror_zero_share: float = 0.50
    mirror_zero_days: int = 30
    # chain (section 1)
    daily_emission_alpha: float = 7200.0
    miner_slot: float = 0.33
    price_samples: int = 20

    def __post_init__(self) -> None:
        if not 0 < self.smoothing <= 1:
            raise ValueError("smoothing must be in (0, 1]")
        if abs(self.m_min + self.w_skill + self.w_hold - 1.0) > 1e-9:
            raise ValueError("m_min + w_skill + w_hold must be 1")
        if self.builder_fee_rate <= 0:
            raise ValueError("builder_fee_rate must be positive")
        if not 0 < self.maker_credit <= 1:
            raise ValueError("maker_credit must be in (0, 1]")
        if self.c_max <= 0 or self.c_tot <= 0:
            raise ValueError("caps must be positive")
        if self.c_tot >= self.c_tot_limit:
            raise ValueError(f"c_tot {self.c_tot} is not farm-proof; it must stay below {self.c_tot_limit:.3f}")

    @property
    def builder_fee_bps(self) -> float:
        return self.builder_fee_rate * 1e4

    @property
    def c_tot_limit(self) -> float:
        """Largest second-cap ratio at which every zero-edge strategy still loses (spec section 11).

        The cheapest round trip through the builder costs `C = 2 × (builder fee − best rebate)` bps and the other
        leg can earn at most `2 × best rebate`; `net ≤ −(1 − c_tot) × C + 2 × rebate < 0` gives the bound.
        """
        c_min = 2 * (self.builder_fee_bps - BEST_REBATE_BPS)
        if c_min <= 0:
            return 0.0
        return 1 - 2 * BEST_REBATE_BPS / c_min

    @property
    def miner_pool_alpha(self) -> float:
        return self.daily_emission_alpha * self.miner_slot


DEFAULT = Params()
