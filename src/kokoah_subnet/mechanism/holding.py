"""Holding factor (specification section 8)."""

from __future__ import annotations


def holding_factor(staked_min_alpha: float, paid_window_alpha: float, collateral_min_alpha: float) -> float:
    """`h' = min(1, staked / max(target, collateral_min))` with `staked` the day's minimum own stake and `target`
    the alpha paid in the holding window. Zero when the denominator is not positive."""
    target = max(paid_window_alpha, collateral_min_alpha)
    if target <= 0:
        return 0.0
    return max(0.0, min(1.0, staked_min_alpha / target))
