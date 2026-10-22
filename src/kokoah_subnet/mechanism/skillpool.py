"""The skill pool (specification v6, section 7a): who takes part, and with what weight.

    s_i = max(0, (g'_i - floor) / (1 - floor))                      the skill above the luck floor
    v_i = min(1, V_i,30d / cap)                                     participation: eligible Kokoah notional, capped
    w_i = s_i x v_i x (1 + agent_bonus if a verified agent)          the skill weight

A miner takes part only when it is eligible that day, passes the activity gates, has no mirror penalty, and passes
the entry test: the probabilistic Sharpe ratio of its capped daily returns over the last `entry_days` days is at
least `entry_psr` (with fewer days recorded, it does not pass). Accounts whose daily returns move almost together
(the copy rule) share one weight: their weights are scaled so the group's total is its largest member's weight.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

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


def _correlation(x: Sequence[float], y: Sequence[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if sx <= 1e-12 or sy <= 1e-12:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / (sx * sy)


def copy_groups(dated_returns: Mapping[str, Mapping[str, float]], params: Params) -> list[list[str]]:
    """Groups (two or more hotkeys, sorted) whose daily returns correlate above `copy_correlation` over at least
    `copy_min_days` common days, joined transitively."""
    keys = sorted(dated_returns)
    parent = {k: k for k in keys}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            common = sorted(set(dated_returns[a]) & set(dated_returns[b]))
            if len(common) < params.copy_min_days:
                continue
            ra = [dated_returns[a][d] for d in common]
            rb = [dated_returns[b][d] for d in common]
            if _correlation(ra, rb) > params.copy_correlation:
                parent[find(b)] = find(a)
    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    return sorted((sorted(g) for g in groups.values() if len(g) > 1), key=lambda g: g[0])


def apply_copy_groups(weights: Mapping[str, float], groups: Sequence[Sequence[str]]) -> dict[str, float]:
    """Scale each group's weights so their total is the largest member's weight (one strategy, one weight)."""
    out = dict(weights)
    for g in groups:
        members = [k for k in g if out.get(k, 0.0) > 0]
        total = sum(out[k] for k in members)
        if len(members) < 2 or total <= 0:
            continue
        top = max(out[k] for k in members)
        for k in members:
            out[k] = out[k] * top / total
    return out
