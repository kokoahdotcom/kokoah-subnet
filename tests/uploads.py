"""Uploading a bundle to the agent service the way `kokoah-subnet agent submit` does: the raw body, with the hotkey,
the bundle's sha256 and the hotkey's signature as headers."""

from typing import Any

from kokoah_subnet.agent.bundle import sha256_hex


def upload(client: Any, hotkey: str, blob: bytes, signature: str, sha256: str | None = None) -> Any:
    return client.post(
        "/agents",
        content=blob,
        headers={
            "content-type": "application/octet-stream",
            "x-kokoah-hotkey": hotkey,
            "x-kokoah-bundle-sha256": sha256 or sha256_hex(blob),
            "x-kokoah-signature": signature,
        },
    )
