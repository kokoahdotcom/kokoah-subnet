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


FILE_COMMIT_VERSION = "kokoah-file-1"


def file_commitment_text(day: str, sha256_hex: str) -> str:
    """What the operator's owner hotkey commits each day: the builder file's hash, so validators can check theirs."""
    return f"{FILE_COMMIT_VERSION} {day} {sha256_hex}"


def parse_file_commitment(text: str) -> tuple[str, str] | None:
    parts = text.split()
    if len(parts) == 3 and parts[0] == FILE_COMMIT_VERSION and len(parts[2]) == 64:
        return parts[1], parts[2].lower()
    return None


async def publish_text(client: Any, wallet: Any, netuid: int, text: str) -> Any:
    """Commit a short text (≤ 128 bytes per field) from the wallet's hotkey."""
    raw = text.encode("utf-8")
    fields = [{f"Raw{len(raw[i : i + 128])}": raw[i : i + 128]} for i in range(0, len(raw), 128)]
    call = calls.Commitments.set_commitment(netuid=netuid, info={"fields": fields})
    return await client.submit_call(call, wallet, signer="hotkey")


async def read_file_commitment(client: Any, netuid: int, owner_hotkey: str) -> tuple[str, str] | None:
    """(day, sha256) committed by the owner hotkey, or None."""
    commitments = await client.subnets.commitments(netuid)
    c = commitments.get(owner_hotkey)
    if c is None or getattr(c, "encrypted", False):
        return None
    return parse_file_commitment(str(c.data))
