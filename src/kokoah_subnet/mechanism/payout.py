"""The two pools, the pay and the weights (specification sections 5 and 6).

The day's miner emission `A` is split into a volume pool (`volume_share`, shared by eligible smoothed builder fees)
and a skill pool (`skill_share`, shared by skill weight). Nothing is capped and nothing is burned: when nobody has a
skill weight the skill pool joins the volume pool, and only a day with no weight at all goes to the fallback hotkey.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .params import Params

U16_MAX = 65535


@dataclass(frozen=True)
class Allocation:
    pool_alpha: float
    price_usd: float
    volume_pool: float
    skill_pool: float
    volume_pays: dict[str, float]  # alpha
    skill_pays: dict[str, float]  # alpha
    burn_alpha: float  # only when no miner has any weight

    @property
    def pays(self) -> dict[str, float]:
        keys = set(self.volume_pays) | set(self.skill_pays)
        return {k: self.volume_pays.get(k, 0.0) + self.skill_pays.get(k, 0.0) for k in sorted(keys)}

    @property
    def paid_alpha(self) -> float:
        return sum(self.pays.values())


def _share(pool: float, weights: Mapping[str, float]) -> dict[str, float]:
    positive = {k: v for k, v in weights.items() if v > 0}
    total = sum(positive.values())
    return {k: pool * v / total for k, v in positive.items()} if total > 0 else {}


def allocate(
    volume_weights: Mapping[str, float],
    skill_weights: Mapping[str, float],
    price_usd: float,
    params: Params,
    pool_alpha: float | None = None,
) -> Allocation:
    """`volume_weights`: eligible smoothed builder fees by hotkey; `skill_weights`: skill weights by hotkey."""
    pool = params.miner_pool_alpha if pool_alpha is None else pool_alpha
    has_volume = any(v > 0 for v in volume_weights.values())
    has_skill = any(v > 0 for v in skill_weights.values())
    skill_pool = pool * params.skill_share if has_skill else 0.0
    volume_pool = pool - skill_pool
    if not has_volume and has_skill:  # nobody to share the volume pool: the skill pool takes everything
        skill_pool, volume_pool = pool, 0.0
    volume_pays = _share(volume_pool, volume_weights)
    skill_pays = _share(skill_pool, skill_weights)
    burn = pool if not volume_pays and not skill_pays else 0.0
    return Allocation(pool, price_usd, volume_pool, skill_pool, volume_pays, skill_pays, burn)


def weights(allocation: Allocation, fallback_hotkey: str) -> dict[str, float]:
    """Weight fractions summing to one: miners by pay; only when no miner has any weight, everything to the
    fallback hotkey (the burn hotkey, or the owner hotkey when no burn hotkey is configured)."""
    if allocation.pool_alpha <= 0 or allocation.burn_alpha > 0:
        return {fallback_hotkey: 1.0}
    return {k: v / allocation.pool_alpha for k, v in allocation.pays.items() if v > 0}


def to_u16(fractions: Mapping[str, float]) -> dict[str, int]:
    """Scale fractions to integers summing to exactly 65535 (largest remainder), dropping zero rows."""
    total = sum(v for v in fractions.values() if v > 0)
    if total <= 0:
        return {}
    raw = {k: v / total * U16_MAX for k, v in fractions.items() if v > 0}
    floored = {k: int(v) for k, v in raw.items()}
    short = U16_MAX - sum(floored.values())
    for k in sorted(raw, key=lambda k: raw[k] - floored[k], reverse=True)[:short]:
        floored[k] += 1
    return {k: v for k, v in floored.items() if v > 0}
