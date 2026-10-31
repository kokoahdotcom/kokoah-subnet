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


def _orders(fills: Sequence[Fill]) -> dict[tuple[str, str], tuple[list[int], list[float]]]:
    """One member's orders by coin and side: the pieces filled in one millisecond are one order (their notional
    summed), in time order."""
    by_key: dict[tuple[str, str], dict[int, float]] = {}
    for f in fills:
        at = by_key.setdefault((f.coin, f.side), {})
        at[f.time_ms] = at.get(f.time_ms, 0.0) + f.notional
    return {key: (sorted(at), [at[t] for t in sorted(at)]) for key, at in by_key.items()}


def _last_unspent(spent: list[int], j: int) -> int:
    """The nearest order at or before `j` with notional left, or -1 (each spent order points to the one before it;
    paths are shortened as they are walked)."""
    root = j
    while root >= 0 and spent[root] != root:
        root = spent[root]
    while j >= 0 and spent[j] != j:
        spent[j], j = root, spent[j]
    return root


def copy_leads(
    group: Sequence[str],
    fills_of: Mapping[str, Sequence[Fill]],
    earlier_of: Mapping[str, Sequence[Fill]] | None = None,
    window_s: float = 3 * 86_400.0,
    reaction_s: float = 600.0,
    scale_s: float = 60.0,
) -> dict[str, dict[str, float]]:
    """`leads[x][y]`: the notional by which member `x` traded before member `y` today. Each of `y`'s orders today
    (`fills_of`) is matched with `x`'s most recent earlier order on the same coin and side within `window_s`, today's
    or from the days before (`earlier_of`), and when that one is smaller, with the ones before it within
    `reaction_s` in turn until `y`'s order is matched in full (an order the original sent in slices is copied as a
    whole). Each piece counts for `x` weighed by how soon `y` followed, `scale_s / (scale_s + wait)` (half at a
    minute), and each of `x`'s orders gives at most its own notional in a day's matching. A copier trades soon after
    what it copies, while the original's next trade comes on its own schedule, later: the shorter waits decide.
    Small orders placed just before weigh no more than their size, and a copy made hours later still counts, a
    little."""
    earlier_of = earlier_of or {}
    window_ms, reaction_ms, scale_ms = window_s * 1000, reaction_s * 1000, scale_s * 1000
    today = {k: _orders(fills_of.get(k, ())) for k in group}
    known = {k: _orders([*earlier_of.get(k, ()), *fills_of.get(k, ())]) for k in group}
    leads: dict[str, dict[str, float]] = {x: {} for x in group}
    for x in group:
        for y in group:
            if x == y:
                continue
            total = 0.0
            for key, (times, sizes) in today[y].items():
                if key not in known[x]:
                    continue
                lead_times, lead_sizes = known[x][key]
                left = list(lead_sizes)  # what each of x's orders can still give
                spent = list(range(len(left)))  # a skip list over x's orders with nothing left
                for t, n in zip(times, sizes, strict=True):
                    # walk back through x's orders before t until this order's notional is matched: through
                    # those within the reaction time, else just the most recent one
                    need = n
                    j = _last_unspent(spent, bisect.bisect_left(lead_times, t) - 1)
                    while need > 0 and j >= 0 and t - lead_times[j] <= window_ms:
                        if need < n and t - lead_times[j] > reaction_ms:
                            break
                        given = min(left[j], need)
                        left[j] -= given
                        need -= given
                        total += given * scale_ms / (scale_ms + t - lead_times[j])
                        if left[j] <= 0:
                            spent[j] = j - 1
                            j = _last_unspent(spent, j - 1)
            if total > 0:
                leads[x][y] = total
    return leads


def copy_leader(
    group: Sequence[str],
    led: Mapping[str, Mapping[str, float]],
    registered_block: Mapping[str, int | None],
    params: Params,
) -> str:
    """The member a copy group follows: the one that leads every other member head to head (`led[x][y]`: smoothed
    over the days, see `copy_leads`) by at least `copy_lead_min_usd` and `copy_lead_margin` times what that member
    led it by, so a copier's second account copying the copier makes up no lead. Without such a member, the earliest
    registration on the subnet (the same on every validator, and no later account can have an earlier one), then the
    hotkey order."""

    def beats(x: str, y: str) -> bool:
        ahead, behind = led.get(x, {}).get(y, 0.0), led.get(y, {}).get(x, 0.0)
        return ahead >= params.copy_lead_min_usd and ahead >= params.copy_lead_margin * behind

    ahead_of_all = [x for x in group if all(beats(x, y) for y in group if y != x)]
    if len(ahead_of_all) == 1:
        return ahead_of_all[0]
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
