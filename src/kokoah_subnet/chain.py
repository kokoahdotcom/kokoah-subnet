"""Thin, read-only helpers over the Bittensor SDK for the chain facts this project needs.

Everything here is a read; nothing signs or submits. Writes go through the SDK's intents (`client.execute`) in code
that is explicit about the network and the wallet.
"""

from __future__ import annotations

from dataclasses import dataclass

import bittensor as bt

NETWORKS = ("local", "test", "finney")


@dataclass(frozen=True)
class ChainInfo:
    """A one-block snapshot of the facts that matter before touching a network."""

    network: str
    block: int
    fast_blocks: bool
    subnet_count: int
    subnet_registration_cost_tao: str


async def chain_info(network: str = "local") -> ChainInfo:
    """Read the block height, block mode, subnet count and the live subnet registration cost."""
    if network not in NETWORKS and not network.startswith("ws"):
        raise ValueError(f"unknown network {network!r}; use one of {NETWORKS} or a ws:// endpoint")
    async with bt.Subtensor(network) as client:
        block = await client.block()
        fast = await client.read("is_fast_blocks")
        subnets = await client.subnets.all()
        cost = await client.subnets.subnet_registration_cost()
    return ChainInfo(
        network=network,
        block=int(block),
        fast_blocks=bool(getattr(fast, "fast_blocks", fast)),
        subnet_count=len(subnets),
        subnet_registration_cost_tao=str(cost),
    )
