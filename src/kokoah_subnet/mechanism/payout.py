"""Multiplier, share, caps, pay, burn and weights (specification sections 5 and 6)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .params import Params

U16_MAX = 65535


def multiplier(skill: float, holding: float, gates_pass: bool, params: Params) -> float:
    """`M = m_min + w_s × g' + w_h × h'`, or the floor when a gate fails."""
    if not gates_pass:
        return params.m_min
    g = max(0.0, min(1.0, skill))
    h = max(0.0, min(1.0, holding))
    return params.m_min + params.w_skill * g + params.w_hold * h


@dataclass(frozen=True)
class MinerDay:
    """What the payout needs about one miner on one day."""

    hotkey: str
    phi_hat: float  # smoothed fee credit, USD
    fee_hat: float  # smoothed total fees on the builder leg, USD
    multiplier: float


@dataclass(frozen=True)
class Allocation:
    pool_alpha: float
    price_usd: float
    shares: dict[str, float]  # alpha
    caps: dict[str, float]  # alpha
    pays: dict[str, float]  # alpha
    burn_alpha: float

    @property
    def paid_alpha(self) -> float:
        return sum(self.pays.values())


def cap_usd(miner: MinerDay, params: Params) -> float:
    """`M × min(c_max × Φ̂, c_tot × F̂)` in USD."""
    return miner.multiplier * min(params.c_max * miner.phi_hat, params.c_tot * max(0.0, miner.fee_hat))


def allocate(
    miners: Sequence[MinerDay], price_usd: float, params: Params, pool_alpha: float | None = None
) -> Allocation:
    """Share the day's pool by `Φ̂ × M`, cap each miner in alpha at the day's average price, burn the rest."""
    if price_usd <= 0:
        raise ValueError("price must be positive")
    pool = params.miner_pool_alpha if pool_alpha is None else pool_alpha
    scores = {m.hotkey: max(0.0, m.phi_hat) * m.multiplier for m in miners}
    total = sum(scores.values())
    shares = {k: (pool * s / total if total > 0 else 0.0) for k, s in scores.items()}
    caps = {m.hotkey: cap_usd(m, params) / price_usd for m in miners}
    pays = {k: min(shares[k], caps[k]) for k in scores}
    burn = pool - sum(pays.values())
    return Allocation(pool, price_usd, shares, caps, pays, max(0.0, burn))


MIN_BURN_WEIGHT = 0.001  # the burn hotkey always receives this slice, so a full subnet never prunes it


def weights(allocation: Allocation, burn_hotkey: str, min_burn_weight: float = MIN_BURN_WEIGHT) -> dict[str, float]:
    """Weight fractions summing to one: miners by pay, the unawarded remainder to the burn hotkey (a dedicated
    hotkey on its own coldkey whose alpha the operator burns with `burn_alpha`; spec section 6). The burn hotkey
    keeps at least `min_burn_weight` even when the pool is scarce, taken proportionally from the miners."""
    if allocation.pool_alpha <= 0:
        return {burn_hotkey: 1.0}
    out = {k: v / allocation.pool_alpha for k, v in allocation.pays.items() if v > 0}
    burn = allocation.burn_alpha / allocation.pool_alpha
    if burn < min_burn_weight and out:
        scale = (1.0 - min_burn_weight) / max(1e-12, sum(out.values()))
        out = {k: v * scale for k, v in out.items()}
        burn = min_burn_weight
    out[burn_hotkey] = out.get(burn_hotkey, 0.0) + burn
    return out


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
