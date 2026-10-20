"""What the validator reads from and writes to the Bittensor chain.

Reads: the metagraph (hotkey → uid, owner hotkey, collateral), own stake per hotkey at sampled blocks (the day's
minimum), the pool price at sampled blocks (the day's mean). Write: the weights, through the SDK's `SetWeights`
intent, which conforms them to the subnet's hyperparameters and picks plaintext or commit-reveal by itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import bittensor as bt

from kokoah_subnet.mechanism.payout import to_u16


@dataclass(frozen=True)
class Neuron:
    """One UID as the metagraph reports it (everything, for the record)."""

    uid: int
    hotkey: str
    coldkey: str
    collateral_min_alpha: float
    validator_permit: bool = False
    active: bool = True
    alpha_stake: float = 0.0
    tao_stake: float = 0.0
    incentive: float = 0.0
    emission: float = 0.0
    dividends: float = 0.0
    rank: float = 0.0
    trust: float = 0.0
    consensus: float = 0.0
    pruning_score: float = 0.0
    last_update: int = 0
    block_at_registration: int = 0
    collateral_locked_alpha: float = 0.0

    def as_row(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "hotkey": self.hotkey,
            "coldkey": self.coldkey,
            "active": self.active,
            "validator_permit": self.validator_permit,
            "alpha_stake": self.alpha_stake,
            "tao_stake": self.tao_stake,
            "incentive": self.incentive,
            "emission": self.emission,
            "dividends": self.dividends,
            "rank": self.rank,
            "trust": self.trust,
            "consensus": self.consensus,
            "pruning_score": self.pruning_score,
            "last_update": self.last_update,
            "block_at_registration": self.block_at_registration,
            "collateral_locked": self.collateral_locked_alpha,
            "collateral_min": self.collateral_min_alpha,
        }


@dataclass(frozen=True)
class SubnetSnapshot:
    netuid: int
    block: int
    tempo: int
    owner_hotkey: str
    neurons: list[Neuron]
    price_tao: float | None  # spot, τ per alpha

    def uid_of(self) -> dict[str, int]:
        return {n.hotkey: n.uid for n in self.neurons}


def _alpha(value: Any) -> float:
    """A balance (TAO or alpha) as a plain number of whole units; None or anything else as 0."""
    if value is None:
        return 0.0
    try:
        return float(value.rao) / 1e9
    except Exception:  # noqa: BLE001 - not a balance
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def snapshot(client: Any, netuid: int, block: int | None = None) -> SubnetSnapshot:
    mg = await client.subnets.metagraph(netuid, block=block, commitments=False)
    if mg is None:
        raise RuntimeError(f"subnet {netuid} has no metagraph")
    neurons = [
        Neuron(
            uid=int(n.uid),
            hotkey=n.hotkey,
            coldkey=n.coldkey,
            collateral_min_alpha=_alpha(getattr(n, "collateral_min", None)),
            validator_permit=bool(getattr(n, "validator_permit", False)),
            active=bool(getattr(n, "active", True)),
            alpha_stake=_alpha(getattr(n, "alpha_stake", None)),
            tao_stake=_alpha(getattr(n, "tao_stake", None)),
            incentive=float(getattr(n, "incentive", 0.0) or 0.0),
            emission=_alpha(getattr(n, "emission", None)),
            dividends=float(getattr(n, "dividends", 0.0) or 0.0),
            rank=float(getattr(n, "rank", 0.0) or 0.0),
            trust=float(getattr(n, "trust", 0.0) or 0.0),
            consensus=float(getattr(n, "consensus", 0.0) or 0.0),
            pruning_score=float(getattr(n, "pruning_score", 0.0) or 0.0),
            last_update=int(getattr(n, "last_update", 0) or 0),
            block_at_registration=int(getattr(n, "block_at_registration", 0) or 0),
            collateral_locked_alpha=_alpha(getattr(n, "collateral_locked", None)),
        )
        for n in mg.neurons
    ]
    return SubnetSnapshot(netuid, int(mg.block), int(mg.tempo), mg.owner_hotkey, neurons, mg.price)


def sample_blocks(end_block: int, tempo: int, samples: int) -> list[int]:
    """`samples` block heights, one per tempo, ending at `end_block` (the day's tempo boundaries)."""
    return [max(1, end_block - i * tempo) for i in range(samples)][::-1]


async def stake_samples(
    client: Any, netuid: int, coldkey: str, hotkey: str, blocks: Sequence[int]
) -> list[tuple[int, float]]:
    """Own stake (alpha) on the hotkey at each sampled block."""
    out: list[tuple[int, float]] = []
    for b in blocks:
        out.append((int(b), _alpha(await client.staking.stake(coldkey, hotkey, netuid, block=b))))
    return out


async def own_stake_min(client: Any, netuid: int, coldkey: str, hotkey: str, blocks: Sequence[int]) -> float:
    """The smallest own stake (alpha) on the hotkey across the sampled blocks (spec section 8)."""
    values = [v for _, v in await stake_samples(client, netuid, coldkey, hotkey, blocks)]
    return min(values) if values else 0.0


async def price_samples(client: Any, netuid: int, blocks: Sequence[int]) -> list[tuple[int, float]]:
    """Spot pool rate (τ per alpha) at each sampled block."""
    out: list[tuple[int, float]] = []
    for b in blocks:
        mg = await client.subnets.metagraph(netuid, block=b, commitments=False)
        if mg is not None and mg.price is not None:
            out.append((int(b), float(mg.price)))
    return out


async def mean_price_tao(client: Any, netuid: int, blocks: Sequence[int]) -> float:
    """Mean spot pool rate (τ per alpha) over the sampled blocks (spec section 2, `p_d` before the TAO/USD mark)."""
    prices = [p for _, p in await price_samples(client, netuid, blocks)]
    if not prices:
        raise RuntimeError("no price samples")
    return sum(prices) / len(prices)


def weights_by_uid(fractions: Mapping[str, float], uid_of: Mapping[str, int]) -> tuple[list[int], list[int]]:
    """Hotkey fractions → (uids, u16 weights) for the chain. Unknown hotkeys are dropped and the rest renormalized."""
    known = {hk: f for hk, f in fractions.items() if hk in uid_of and f > 0}
    u16 = to_u16(known)
    uids = sorted(uid_of[hk] for hk in u16)
    by_uid = {uid_of[hk]: w for hk, w in u16.items()}
    return uids, [by_uid[u] for u in uids]


async def set_weights(client: Any, wallet: Any, netuid: int, uids: Sequence[int], values: Sequence[int]) -> Any:
    """Submit weights with the SDK intent (hotkey-signed, conformed to the subnet's hyperparameters)."""
    intent = bt.SetWeights(netuid=netuid, uids=list(uids), weights=[float(v) for v in values])
    return await client.execute(intent, wallet)
