"""Thin, read-only helpers over the Bittensor SDK for the chain facts this project needs.

Everything here is a read; nothing signs or submits. Writes go through the SDK's intents (`client.execute`) in code
that is explicit about the network and the wallet.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

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


async def registered_hotkeys(network: str, netuid: int) -> set[str]:
    """The hotkeys registered on the subnet now."""
    async with bt.Subtensor(network) as client:
        mg = await client.subnets.metagraph(netuid, commitments=False)
    if mg is None:
        raise RuntimeError(f"subnet {netuid} has no metagraph")
    return {str(n.hotkey) for n in mg.neurons}


async def bound_links(network: str, netuid: int) -> set[tuple[str, str]]:
    """(hotkey, address) for every address the subnet's valid bindings on chain name now: the bound one, and on a
    migration the one it moves from."""
    from kokoah_subnet.binding.chain import read_bindings

    async with bt.Subtensor(network) as client:
        published = await read_bindings(client, netuid)
    links = set()
    for pb in published:
        if pb.valid:
            links.add((pb.hotkey, pb.binding.address))
            if pb.binding.previous_address:
                links.add((pb.hotkey, pb.binding.previous_address))
    return links


class RegisteredHotkeys:
    """`check(hotkey)`: whether the hotkey is registered on the subnet. The list is read again when it is older than
    `max_age_s`, or on an unknown hotkey once `refresh_s` has passed (a just-registered miner waits at most that)."""

    def __init__(
        self,
        fetch: Callable[[], Coroutine[Any, Any, set[str]]],
        max_age_s: float = 600.0,
        refresh_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fetch = fetch
        self.max_age_s = max_age_s
        self.refresh_s = refresh_s
        self.clock = clock
        self._hotkeys: set[str] = set()
        self._read_at: float | None = None
        self._lock = threading.Lock()
        self._refreshing = threading.Lock()

    def _read(self) -> None:
        try:
            hotkeys = asyncio.run(self.fetch())
        except Exception:
            if self._read_at is None or self.clock() - self._read_at > 6 * self.max_age_s:
                raise  # nothing recent enough to fall back on
            return  # keep the last list; read again on the next unknown hotkey
        with self._lock:
            self._hotkeys, self._read_at = hotkeys, self.clock()

    def check(self, hotkey: str) -> bool:
        """One reader at a time refreshes the list; everyone else answers from the current list meanwhile (only the
        very first read is waited for), so a slow chain never holds up other requests."""
        with self._lock:
            age = None if self._read_at is None else self.clock() - self._read_at
            stale = age is None or age > self.max_age_s or (hotkey not in self._hotkeys and age > self.refresh_s)
        if stale:
            if self._refreshing.acquire(blocking=self._read_at is None):
                try:
                    with self._lock:
                        age = None if self._read_at is None else self.clock() - self._read_at
                        still = (
                            age is None
                            or age > self.max_age_s
                            or (hotkey not in self._hotkeys and age > self.refresh_s)
                        )
                    if still:
                        self._read()
                finally:
                    self._refreshing.release()
        with self._lock:
            return hotkey in self._hotkeys
