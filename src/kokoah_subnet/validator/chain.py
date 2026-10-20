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
    uid: int
    hotkey: str
    coldkey: str
    collateral_min_alpha: float


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
    if value is None:
        return 0.0
    for attr in ("alpha", "rao"):
        if hasattr(value, attr):
            v = getattr(value, attr)
            return float(v) if attr == "alpha" else float(v) / 1e9
    return float(value)


async def snapshot(client: Any, netuid: int, block: int | None = None) -> SubnetSnapshot:
    mg = await client.subnets.metagraph(netuid, block=block, commitments=False)
    if mg is None:
        raise RuntimeError(f"subnet {netuid} has no metagraph")
    neurons = [Neuron(n.uid, n.hotkey, n.coldkey, _alpha(n.collateral_min)) for n in mg.neurons]
    return SubnetSnapshot(netuid, int(mg.block), int(mg.tempo), mg.owner_hotkey, neurons, mg.price)


def sample_blocks(end_block: int, tempo: int, samples: int) -> list[int]:
    """`samples` block heights, one per tempo, ending at `end_block` (the day's tempo boundaries)."""
    return [max(1, end_block - i * tempo) for i in range(samples)][::-1]


async def own_stake_min(client: Any, netuid: int, coldkey: str, hotkey: str, blocks: Sequence[int]) -> float:
    """The smallest own stake (alpha) on the hotkey across the sampled blocks (spec section 8)."""
    values = []
    for b in blocks:
        values.append(_alpha(await client.staking.stake(coldkey, hotkey, netuid, block=b)))
    return min(values) if values else 0.0


async def mean_price_tao(client: Any, netuid: int, blocks: Sequence[int]) -> float:
    """Mean spot pool price (τ per alpha) over the sampled blocks (spec section 2, `p_d` before the TAO/USD mark)."""
    prices: list[float] = []
    for b in blocks:
        mg = await client.subnets.metagraph(netuid, block=b, commitments=False)
        if mg is not None and mg.price is not None:
            prices.append(float(mg.price))
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
