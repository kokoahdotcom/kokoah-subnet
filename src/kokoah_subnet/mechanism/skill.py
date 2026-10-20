"""Skill factor (specification section 7): confidence × quality, no drawdown anywhere.

Returns are daily, newest last, with deposits and withdrawals removed, each capped at ±10%.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from .params import Params


def daily_return(equity_prev: float, equity: float, flows: float) -> float:
    """`(E_d − E_{d−1} − flows_d) / E_{d−1}`; zero when yesterday's equity was not positive."""
    if equity_prev <= 0:
        return 0.0
    return (equity - equity_prev - flows) / equity_prev


def capped(returns: Sequence[float], cap: float) -> list[float]:
    return [max(-cap, min(cap, r)) for r in returns]


def prob_sharpe(returns: Sequence[float], benchmark: float, min_days: int) -> float:
    """Probabilistic Sharpe ratio (Bailey and López de Prado 2012): P(true daily Sharpe > benchmark), with the
    skew and kurtosis adjustment. 0.5 when nothing can be said."""
    n = len(returns)
    if n < min_days:
        return 0.5
    mu = statistics.fmean(returns)
    sd = statistics.pstdev(returns)
    if sd <= 1e-12:
        return 0.5
    sr = mu / sd
    z3 = sum(((r - mu) / sd) ** 3 for r in returns) / n
    z4 = sum(((r - mu) / sd) ** 4 for r in returns) / n
    denom = math.sqrt(max(1e-9, 1 - z3 * sr + (z4 - 1) / 4 * sr * sr))
    z = (sr - benchmark) * math.sqrt(n - 1) / denom
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def ew_gain_to_pain(returns: Sequence[float], half_life_days: float) -> float:
    """Recency-weighted gain-to-pain (newest day weight 1, halving every `half_life_days`)."""
    n = len(returns)
    gains = pains = 0.0
    for i, r in enumerate(returns):
        w = 0.5 ** ((n - 1 - i) / half_life_days)
        if r > 0:
            gains += w * r
        else:
            pains -= w * r
    if pains <= 0:
        return 10.0 if gains > 0 else 0.0
    return gains / pains


def blowups(returns: Sequence[float], threshold: float) -> int:
    return sum(1 for r in returns if r < threshold)


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass(frozen=True)
class SkillParts:
    confidence: float
    quality: float
    raw: float  # g_raw = confidence × quality


def skill_raw(returns: Sequence[float], params: Params) -> SkillParts:
    """`g_raw` from raw daily returns (newest last). Capping and windows are applied here; the blow-up rule reads
    the uncapped returns."""
    conf_window = capped(returns[-params.confidence_days :], params.return_cap)
    confidence = clip01((prob_sharpe(conf_window, params.psr_benchmark, params.psr_min_days) - 0.5) / 0.45)
    qual_window = capped(returns[-params.quality_days :], params.return_cap)
    quality = clip01((ew_gain_to_pain(qual_window, params.quality_half_life_days) - 1) / params.gpr_full)
    if blowups(returns[-params.blowup_days :], params.blowup_threshold) >= params.blowup_count:
        quality *= 0.5
    return SkillParts(confidence, quality, confidence * quality)


def skill_factor(g: float, params: Params) -> float:
    """`g' = min(1, g / g_full)` from the smoothed `g`."""
    return clip01(g / params.g_full)
