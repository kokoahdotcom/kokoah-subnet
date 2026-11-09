"""The skill pool (specification section 7a): who takes part, and with what weight.

    s_i = max(0, (g'_i - floor) / (1 - floor))                      the skill above the luck floor
    v_i = min(1, V_i,30d / cap)                                     participation: eligible Kokoah notional, capped
    r_i = start + (1 - start) x clip01((n_i - min_days) / (days - min_days))   the record: longer weighs more
    w_i = s_i x v_i x r_i x (1 + agent_bonus if a verified agent)   the skill weight

A miner takes part only when it is eligible that day, passes the activity gates, has no mirror penalty, and passes
the entry test: the probabilistic Sharpe ratio of its capped daily returns since it joined (the last `entry_days` of
them at most) is at least `entry_psr`, with at least `entry_min_days` of them known (fewer: it does not pass). `n_i`
is how many returns the test read, so the weight grows from `record_start_weight` at `entry_min_days` to full at
`entry_days`. Each account is weighed on its own record.
"""

from __future__ import annotations

from collections.abc import Sequence

from .params import Params
from .skill import capped, prob_sharpe


def adjusted_skill(g_prime: float, params: Params) -> float:
    return max(0.0, (g_prime - params.skill_floor) / (1 - params.skill_floor))


def volume_factor(notional_window_usd: float, params: Params) -> float:
    return max(0.0, min(1.0, notional_window_usd / params.skill_volume_cap_usd))


def record_days(returns: Sequence[float], params: Params) -> int:
    """How many of the returns since joining the entry test reads (the last `entry_days` at most)."""
    return min(len(returns), params.entry_days)


def entry_psr(returns: Sequence[float], params: Params) -> float | None:
    """The entry test's PSR over the last `entry_days` capped returns since joining; None with fewer than
    `entry_min_days` of them."""
    window = list(returns[-params.entry_days :])
    if len(window) < params.entry_min_days:
        return None
    return prob_sharpe(capped(window, params.return_cap), params.psr_benchmark, params.psr_min_days)


def passes_entry(returns: Sequence[float], params: Params) -> bool:
    psr = entry_psr(returns, params)
    return psr is not None and psr >= params.entry_psr


def record_factor(days: int, params: Params) -> float:
    """`record_start_weight` at `entry_min_days` of record, rising evenly to 1 at `entry_days`."""
    span = params.entry_days - params.entry_min_days
    grown = 1.0 if span <= 0 else max(0.0, min(1.0, (days - params.entry_min_days) / span))
    return params.record_start_weight + (1 - params.record_start_weight) * grown


def skill_weight(g_prime: float, notional_window_usd: float, agent: bool, params: Params, *, days: int) -> float:
    """The skill weight of a miner that passed the entry test with `days` of record (see `record_days`)."""
    w = adjusted_skill(g_prime, params) * volume_factor(notional_window_usd, params) * record_factor(days, params)
    return w * (1 + params.agent_bonus) if agent else w
