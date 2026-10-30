"""The skill pool (specification section 7a): who takes part, and with what weight.

    s_i = max(0, (g'_i - floor) / (1 - floor))                      the skill above the luck floor
    v_i = min(1, V_i,30d / cap)                                     participation: eligible Kokoah notional, capped
    w_i = s_i x v_i x (1 + agent_bonus if a verified agent)          the skill weight

A miner takes part only when it is eligible that day, passes the activity gates, has no mirror penalty, and passes
the entry test: the probabilistic Sharpe ratio of its capped daily returns over the last `entry_days` days is at
least `entry_psr` (with fewer days recorded, it does not pass). Accounts whose daily returns move almost together
(the copy rule) share one weight: only the group's leader (the member that trades first, by notional over the days,
else the earliest registration) keeps its weight.
"""

from __future__ import annotations

import bisect
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


def copy_leads(
    group: Sequence[str], fills_of: Mapping[str, Sequence[Fill]], window_s: float = 600.0
) -> dict[str, dict[str, float]]:
    """`leads[x][y]`: the notional by which member `x` traded before member `y` today. Every fill of `x` is matched
    with the nearest fill of `y` on the same coin and side; when `y`'s fill comes after it within `window_s`, the
    pair counts the smaller of the two fills' notional, and each fill of `y` counts once, for its largest match
    (a copier always trades after what it copies, and many small fills weigh no more than their size)."""
    index: dict[str, dict[tuple[str, str], tuple[list[int], list[float]]]] = {}
    for k in group:
        by_key: dict[tuple[str, str], list[tuple[int, float]]] = {}
        for f in fills_of.get(k, ()):
            by_key.setdefault((f.coin, f.side), []).append((f.time_ms, f.notional))
        index[k] = {key: ([t for t, _ in sorted(v)], [n for _, n in sorted(v)]) for key, v in by_key.items()}
    window_ms = window_s * 1000
    leads: dict[str, dict[str, float]] = {x: {} for x in group}
    for x in group:
        for y in group:
            if x == y:
                continue
            total = 0.0
            for key, (times, sizes) in index[x].items():
                if key not in index[y]:
                    continue
                other_times, other_sizes = index[y][key]
                best: dict[int, float] = {}  # y's fill -> its largest match
                for t, n in zip(times, sizes, strict=True):
                    i = bisect.bisect_left(other_times, t)
                    # the nearest of y's fills: the last before t or the first at or after it (ties go to the earlier)
                    near = min(
                        (j for j in (i - 1, i) if 0 <= j < len(other_times)),
                        key=lambda j: (abs(other_times[j] - t), other_times[j]),
                        default=None,
                    )
                    if near is not None and 0 < other_times[near] - t <= window_ms:
                        best[near] = max(best.get(near, 0.0), min(n, other_sizes[near]))
                total += sum(best.values())
            if total > 0:
                leads[x][y] = total
    return leads


def copy_leader(
    group: Sequence[str],
    led: Mapping[str, Mapping[str, float]],
    registered_block: Mapping[str, int | None],
) -> str:
    """The member a copy group follows: the one that led the most notional (`led[x][y]`: smoothed over the days, see
    `copy_leads`), when it led strictly the most. Without that, the earliest registration on the subnet (the same on
    every validator, and no later account can have an earlier one), then the hotkey order."""
    totals = {k: sum(v for y, v in led.get(k, {}).items() if y in group and y != k) for k in group}
    best = max(totals.values(), default=0.0)
    top = [k for k in group if totals[k] == best]
    if best > 0 and len(top) == 1:
        return top[0]
    return min(group, key=lambda k: (registered_block.get(k) is None, registered_block.get(k) or 0, k))


LEAD_FLOOR = 0.01  # USD: smaller smoothed leads are dropped from the state


def smoothed_leads(
    previous: Mapping[str, Mapping[str, float]], today: Mapping[str, Mapping[str, float]], params: Params
) -> dict[str, dict[str, float]]:
    """Each member's lead over each other, smoothed over the days like credit and skill (`params.smoothing` for
    today), so one day of many quick trades does not decide who leads."""
    out: dict[str, dict[str, float]] = {}
    for x in set(previous) | set(today):
        before, now = previous.get(x, {}), today.get(x, {})
        row = {
            y: (1 - params.smoothing) * before.get(y, 0.0) + params.smoothing * now.get(y, 0.0)
            for y in set(before) | set(now)
        }
        kept = {y: v for y, v in row.items() if v >= LEAD_FLOOR}
        if kept:
            out[x] = kept
    return out


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
