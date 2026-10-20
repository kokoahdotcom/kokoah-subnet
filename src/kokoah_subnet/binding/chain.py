"""Publishing and reading bindings as on-chain commitments (Commitments pallet, hotkey-signed)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bittensor import calls

from .message import Binding, decode_binding, encode_fields, verify_binding


@dataclass(frozen=True)
class PublishedBinding:
    hotkey: str
    block: int
    binding: Binding
    valid: bool  # signatures check out for this hotkey and netuid


async def publish_binding(client: Any, wallet: Any, netuid: int, binding: Binding) -> Any:
    """Submit the binding as the hotkey's commitment on the subnet (replaces any earlier commitment)."""
    fields = [{f"Raw{len(f)}": f.encode("ascii")} for f in encode_fields(binding)]
    call = calls.Commitments.set_commitment(netuid=netuid, info={"fields": fields})
    return await client.submit_call(call, wallet, signer="hotkey")


async def read_bindings(client: Any, netuid: int) -> list[PublishedBinding]:
    """Every hotkey's commitment on the subnet that parses as a binding, with its signature check."""
    commitments = await client.subnets.commitments(netuid)
    out: list[PublishedBinding] = []
    for hotkey, c in commitments.items():
        if getattr(c, "encrypted", False):
            continue
        b = decode_binding(str(c.data))
        if b is None:
            continue
        out.append(PublishedBinding(hotkey, int(c.block), b, verify_binding(netuid, hotkey, b)))
    return out
