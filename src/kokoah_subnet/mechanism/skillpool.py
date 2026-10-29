"""The skill pool (specification section 7a): who takes part, and with what weight.

    s_i = max(0, (g'_i - floor) / (1 - floor))                      the skill above the luck floor
    v_i = min(1, V_i,30d / cap)                                     participation: eligible Kokoah notional, capped
    w_i = s_i x v_i x (1 + agent_bonus if a verified agent)          the skill weight

A miner takes part only when it is eligible that day, passes the activity gates, has no mirror penalty, and passes
the entry test: the probabilistic Sharpe ratio of its capped daily returns over the last `entry_days` days is at
least `entry_psr` (with fewer days recorded, it does not pass). Accounts whose daily returns move almost together
(the copy rule) share one weight: only the group's leader (the member that trades first, else the longest record)
keeps its weight.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from .fills import Fill
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


def copy_leader(
    group: Sequence[str],
    fills_of: Mapping[str, Sequence[Fill]],
    record_start: Mapping[str, str | None],
    window_s: float = 600.0,
    min_pairs: int = 5,
) -> str:
    """The member a copy group follows. Whoever trades first leads: every fill of one member is matched with the
    nearest fill of another member on the same coin and side within `window_s`, and the member whose fills come
    first most often leads (a copier always trades after what it copies). Without `min_pairs` matched fills the
    longest record leads (the day its account history on the exchange begins), then the hotkey order."""
    index: dict[str, dict[tuple[str, str], list[int]]] = {}
    for k in group:
        by_key: dict[tuple[str, str], list[int]] = {}
        for f in fills_of.get(k, ()):
            by_key.setdefault((f.coin, f.side), []).append(f.time_ms)
        index[k] = {key: sorted(ts) for key, ts in by_key.items()}
    led = dict.fromkeys(group, 0)
    pairs = 0
    window_ms = window_s * 1000
    for x in group:
        for y in group:
            if x == y:
                continue
            for key, times in index[x].items():
                others = index[y].get(key, [])
                for t in times:
                    near = min(others, key=lambda u: abs(u - t), default=None)
                    if near is not None and 0 < near - t <= window_ms:
                        led[x] += 1
                        pairs += 1
    best = max(led.values())
    top = [k for k in group if led[k] == best]
    if pairs >= min_pairs and len(top) == 1:
        return top[0]
    return min(group, key=lambda k: (record_start.get(k) or "9999-99-99", k))


def apply_copy_groups(
    weights: Mapping[str, float], groups: Sequence[Sequence[str]], leaders: Mapping[tuple[str, ...], str]
) -> dict[str, float]:
    """One strategy, one weight: in each group only the leader keeps its weight; the others get none."""
    out = dict(weights)
    for g in groups:
        leader = leaders.get(tuple(g))
        if leader is None:
            continue
        for k in g:
            if k != leader:
                out[k] = 0.0
    return out
