"""Holding factor (specification section 8)."""

from __future__ import annotations

MIN_TARGET_ALPHA = 1.0  # below this much pay in the window, holding is judged against one alpha


def holding_factor(
    staked_min_alpha: float,
    paid_window_alpha: float,
    collateral_locked_alpha: float = 0.0,
    min_target_alpha: float = MIN_TARGET_ALPHA,
) -> float:
    """`h' = min(1, (staked − locked collateral) / max(paid in the window, 1 alpha))`.

    `staked` is the day's minimum own stake on the hotkey; the registration collateral is subtracted so that a fresh
    hotkey starts at 0 and a trader who re-registers to shed a selling record gains nothing: holding is earned by
    keeping pay (or buying alpha), never by registering.
    """
    kept = max(0.0, staked_min_alpha - collateral_locked_alpha)
    target = max(paid_window_alpha, min_target_alpha)
    if target <= 0:
        return 0.0
    return max(0.0, min(1.0, kept / target))
