"""Uploading a bundle to the agent service the way `kokoah-subnet agent submit` does: the raw body, with the hotkey,
the bundle's sha256, the hotkey's signature and the signed time as headers."""

import time
from typing import Any

from kokoah_subnet.agent.bundle import sha256_hex
from kokoah_subnet.agent.service import submit_message


def now_ms() -> int:
    return int(time.time() * 1000)


def upload(
    client: Any, hotkey: str, blob: bytes, signature: str, sha256: str | None = None, time_ms: int | None = None
) -> Any:
    return client.post(
        "/agents",
        content=blob,
        headers={
            "content-type": "application/octet-stream",
            "x-kokoah-hotkey": hotkey,
            "x-kokoah-bundle-sha256": sha256 or sha256_hex(blob),
            "x-kokoah-signature": signature,
            "x-kokoah-time": str(now_ms() if time_ms is None else time_ms),
        },
    )


def signed_upload(client: Any, keypair: Any, blob: bytes, time_ms: int | None = None) -> Any:
    """An upload signed by the keypair (a hotkey), at `time_ms` (now when None)."""
    t = now_ms() if time_ms is None else time_ms
    signature = bytes(keypair.sign(submit_message(sha256_hex(blob), keypair.ss58_address, t).encode())).hex()
    return upload(client, keypair.ss58_address, blob, signature, time_ms=t)
