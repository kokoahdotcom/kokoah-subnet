"""The skill pool (specification section 7a): who takes part, and with what weight.

    s_i = max(0, (g'_i - floor) / (1 - floor))                      the skill above the luck floor
    v_i = min(1, V_i,30d / cap)                                     participation: eligible Kokoah notional, capped
    w_i = s_i x v_i x (1 + agent_bonus if a verified agent)          the skill weight

A miner takes part only when it is eligible that day, passes the activity gates, has no mirror penalty, and passes
the entry test: the probabilistic Sharpe ratio of its capped daily returns over the last `entry_days` days is at
least `entry_psr` (with fewer days recorded, it does not pass). Each account is weighed on its own record.
"""

from __future__ import annotations

from collections.abc import Sequence

from .params import Params
from .skill import capped, prob_sharpe


def adjusted_skill(g_prime: float, params: Params) -> float:
    return max(0.0, (g_prime - params.skill_floor) / (1 - params.skill_floor))


def volume_factor(notional_window_usd: float, params: Params) -> float:
    return max(0.0, min(1.0, notional_window_usd / params.skill_volume_cap_usd))


def entry_psr(returns: Sequence[float], params: Params) -> float | None:
    """The entry test's PSR over the last `entry_days` capped returns; None with fewer days recorded."""
    window = list(returns[-params.entry_days :])
    if len(window) < params.entry_days:
        return None
    return prob_sharpe(capped(window, params.return_cap), params.psr_benchmark, params.psr_min_days)


def passes_entry(returns: Sequence[float], params: Params) -> bool:
    psr = entry_psr(returns, params)
    return psr is not None and psr >= params.entry_psr


def skill_weight(g_prime: float, notional_window_usd: float, agent: bool, params: Params) -> float:
    w = adjusted_skill(g_prime, params) * volume_factor(notional_window_usd, params)
    return w * (1 + params.agent_bonus) if agent else w
