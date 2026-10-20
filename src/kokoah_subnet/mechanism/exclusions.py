"""What never counts (specification section 3): cluster self-matches, off-mark prices, mirrored volume."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from .fills import Fill
from .params import Params


def drop_cluster_matches(fills: Iterable[Fill], cluster_of: Mapping[str, str]) -> list[Fill]:
    """Remove fills whose counterparty is in the same cluster as the user (an unknown address is its own cluster)."""
    out: list[Fill] = []
    for f in fills:
        if f.counterparty:
            a = cluster_of.get(f.user, f.user)
            b = cluster_of.get(f.counterparty, f.counterparty)
            if a == b:
                continue
        out.append(f)
    return out


def clip_to_mark(fills: Iterable[Fill], params: Params) -> list[Fill]:
    """Fills priced more than `mark_clip` from the mark are valued at the mark (price replaced; size kept)."""
    out: list[Fill] = []
    for f in fills:
        if f.mark_px is not None and f.mark_px > 0 and abs(f.px / f.mark_px - 1) > params.mark_clip:
            out.append(replace(f, px=f.mark_px))
        else:
            out.append(f)
    return out


def bound_counterparty_notional(fills_by_address: Mapping[str, Sequence[Fill]]) -> dict[str, float]:
    """Mirror rule (counterparties): notional each bound address traded directly against another bound address,
    read from the fill's counterparty field. Exact, so an honest trader is never flagged by coincidence."""
    bound = set(fills_by_address)
    return {
        a: sum(f.notional for f in fills if f.counterparty in bound and f.counterparty != a)
        for a, fills in fills_by_address.items()
    }


def drop_bound_counterparties(fills: Iterable[Fill], bound: set[str]) -> list[Fill]:
    """Remove fills whose counterparty is another bound address: the subnet does not pay for volume between its own
    miners (specification section 3)."""
    return [f for f in fills if not (f.counterparty and f.counterparty in bound and f.counterparty != f.user)]


def _pearson(x: Sequence[float], y: Sequence[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return 0.0
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    return sxy / math.sqrt(sxx * syy)


def mirror_position_share(positions: Mapping[str, Mapping[str, Sequence[float]]], params: Params) -> dict[str, float]:
    """Mirror rule (positions): `positions[address][coin]` is the signed daily net position notional over the same
    days for every address. For each pair and coin, days where both hold a position are compared; a correlation
    below the threshold on at least `mirror_min_days` such days marks `min(|a|, |b|)` of each day as mirrored.
    Returns mirrored notional / total position notional per address."""
    totals: dict[str, float] = {}
    # mirrored notional per address, coin and day; capped at the own position so several partners cannot count
    # the same day twice
    mirrored: dict[str, dict[str, list[float]]] = {}
    for a, coins in positions.items():
        totals[a] = sum(abs(v) for series in coins.values() for v in series)
        mirrored[a] = {coin: [0.0] * len(series) for coin, series in coins.items()}
    addresses = sorted(positions)
    for i, a in enumerate(addresses):
        for b in addresses[i + 1 :]:
            for coin, sa in positions[a].items():
                sb = positions[b].get(coin)
                if sb is None:
                    continue
                days = [d for d, (x, y) in enumerate(zip(sa, sb, strict=True)) if x != 0 and y != 0]
                if len(days) < params.mirror_min_days:
                    continue
                corr = _pearson([sa[d] for d in days], [sb[d] for d in days])
                if corr < params.mirror_correlation:
                    for d in days:
                        overlap = min(abs(sa[d]), abs(sb[d]))
                        mirrored[a][coin][d] = min(abs(sa[d]), mirrored[a][coin][d] + overlap)
                        mirrored[b][coin][d] = min(abs(sb[d]), mirrored[b][coin][d] + overlap)
    mirrored_total = {a: sum(sum(series) for series in coins.values()) for a, coins in mirrored.items()}
    return {a: (mirrored_total[a] / totals[a] if totals[a] > 0 else 0.0) for a in addresses}


@dataclass(frozen=True)
class MirrorAction:
    credit_factor: float  # multiply the day's credit by this
    zero_multiplier: bool  # g' = h' = 0 for `mirror_zero_days`, and report


def mirror_action(mirrored_share: float, params: Params) -> MirrorAction:
    """Above 25% of notional mirrored: the mirrored part is removed from credit; above 50%: also the quality
    multiplier is zeroed and the address reported."""
    share = max(0.0, min(1.0, mirrored_share))
    if share > params.mirror_zero_share:
        return MirrorAction(1.0 - share, True)
    if share > params.mirror_exclude_share:
        return MirrorAction(1.0 - share, False)
    return MirrorAction(1.0, False)
