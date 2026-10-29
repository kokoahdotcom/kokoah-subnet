"""The account key's copy, encrypted with the miner's secret and kept by the owner's service (spec section 16).

- **Store.** When a sealed machine creates an account it encrypts the key (AES-256-GCM under a key derived from a
  secret only the miner holds, given to the machine at start) and sends the ciphertext to the owner's service,
  with an attestation token proving a genuine sealed machine of an approved image sent it, and the miner's start
  permission: a message signed by their hotkey naming the subnet, the hotkey, the master address and the code
  (`agent authorize`), so only a hotkey's owner can file copies under it. The service cannot read the copy; the
  miner never sees it.
- **Restore.** A new sealed machine started with the same secret gets the ciphertext back after proving, with a
  fresh attestation token, that it is a genuine sealed machine of an approved image; it decrypts the key inside.
  No person ever holds the key, so the account stays an agent account.
- Both tokens carry the key store's own audience, which the machine's public `/attestation` never uses: only the
  machine's supervisor asks for them, for the key it created or restores itself.
- **Release.** The miner can take the ciphertext themselves, with a request signed by their own address (the
  machine's master). Then the miner holds the key and can trade the account by hand, so the release is public and
  the account never earns the agent bonus again.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from eth_account import Account
from eth_utils.address import is_checksum_address

KEY_MAGIC = b"KAK1"
MIN_SECRET_BYTES = 32
MIN_DISTINCT_BYTES = 20  # 32 random bytes hold about 30 different values; fewer than 20 means a made-up secret


def check_secret(secret: bytes) -> None:
    """Refuse a secret that is clearly not random: the stored copy of the key is only as safe as the secret, so it
    must come from `kokoah-subnet agent secret` (a strong password, stretched; or random bytes)."""
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError(f"the miner's secret must be at least {MIN_SECRET_BYTES} bytes")
    head = secret[:MIN_SECRET_BYTES]
    steps = {(b - a) % 256 for a, b in zip(head, head[1:], strict=False)}
    if (
        len(set(head)) < MIN_DISTINCT_BYTES
        or len(steps) == 1  # a counting pattern
        or all(0x20 <= b < 0x7F for b in head)  # typed text written as hex: 32 random bytes are never all text
    ):
        raise ValueError("the miner's secret is too weak: make it with `kokoah-subnet agent secret`")


def _derive(secret: bytes, address: str) -> bytes:
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError(f"the miner's secret must be at least {MIN_SECRET_BYTES} bytes")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"kokoah-agent-key-1 " + address.encode())
    return hkdf.derive(secret)


def encrypt_key(key: bytes, address: str, secret: bytes) -> bytes:
    a = address.lower()
    nonce = os.urandom(12)
    return KEY_MAGIC + nonce + AESGCM(_derive(secret, a)).encrypt(nonce, key, a.encode())


def decrypt_key(blob: bytes, address: str, secret: bytes) -> bytes:
    a = address.lower()
    if not blob.startswith(KEY_MAGIC):
        raise ValueError("not an encrypted agent key")
    key = AESGCM(_derive(secret, a)).decrypt(blob[4:16], blob[16:], a.encode())
    if Account.from_key(key).address.lower() != a:
        raise ValueError("the key belongs to another account")
    return key


def checked_address(address: str) -> str:
    """A Hyperliquid (EVM) address as typed by the miner, lowercase; raises ValueError when it is not one, or when
    its mixed case does not match its EIP-55 checksum (a typo)."""
    a = address.strip()
    if len(a) != 42 or not a.startswith("0x") or any(ch not in "0123456789abcdefABCDEF" for ch in a[2:]):
        raise ValueError(f"{address!r} is not an address (0x and 40 hex characters)")
    if a[2:] != a[2:].lower() and a[2:] != a[2:].upper() and not is_checksum_address(a):
        raise ValueError(f"{address} does not match its checksum: check it for a typo")
    return a.lower()


PERMIT_DAYS = 7  # a start permission is good for one machine start within this many days of signing


def start_message(netuid: int, hotkey: str, master: str, submission: str, time_ms: int) -> str:
    """What the miner's hotkey signs to let one sealed machine file its key copy under that hotkey."""
    return (
        f"kokoah-agent-start-1 netuid={netuid} hotkey={hotkey} master={master.lower()} "
        f"submission={submission} time={time_ms}"
    )


def parse_permit(permit: str) -> tuple[int, str]:
    """A start permission as passed to the machine: `<time_ms>:<hex sr25519 signature>`. The signature comes back in
    one spelling (lower-case hex, no 0x), the one the service records as used: the signature check reads the same
    bytes from upper case or spaced hex too."""
    t, _, signature = permit.strip().partition(":")
    try:
        raw = bytes.fromhex(signature.removeprefix("0x"))
    except ValueError:
        raw = b""
    if not t.isdigit() or not raw:
        raise ValueError("a start permission looks like <time_ms>:<signature> (from `kokoah-subnet agent authorize`)")
    return int(t), raw.hex()


def store_digest(address: str, master: str, blob: bytes, hotkey: str, submission: str, permit: str) -> str:
    """The attested nonce of a store: the machine's own account, copy, master, hotkey, code and permission."""
    blob_sha = hashlib.sha256(blob).hexdigest()
    permit_sha = hashlib.sha256(permit.strip().encode()).hexdigest()
    text = (
        f"kokoah-agent-store-2 address={address.lower()} master={master.lower()} hotkey={hotkey} "
        f"submission={submission} permit={permit_sha} blob={blob_sha}"
    )
    return hashlib.sha256(text.encode()).hexdigest()


def restore_digest(address: str, challenge: str) -> str:
    return hashlib.sha256(
        f"kokoah-agent-restore-1 address={address.lower()} challenge={challenge}".encode()
    ).hexdigest()


def release_message(address: str, time_ms: int) -> str:
    return f"kokoah-agent-release-1 account={address.lower()} time={time_ms}"


class KeyService:
    """The owner's service, as the sealed machine and the miner's tools reach it."""

    def __init__(self, service: str, http: httpx.Client | None = None) -> None:
        self.service = service.rstrip("/")
        self.http = http or httpx.Client(timeout=30)

    def _url(self, address: str, tail: str = "") -> str:
        return f"{self.service}/key-store/{address.lower()}{tail}"

    def store(
        self,
        address: str,
        master: str,
        hotkey: str,
        blob: bytes,
        token: str,
        netuid: int,
        submission: str,
        permit: str,
    ) -> None:
        body = {
            "blob": base64.b64encode(blob).decode(),
            "master": master.lower(),
            "hotkey": hotkey,
            "token": token,
            "netuid": netuid,
            "submission": submission,
            "permit": permit.strip(),
        }
        self.http.put(self._url(address), json=body).raise_for_status()

    def kept(self, address: str) -> bool:
        """Whether the service keeps a copy for this account."""
        r = self.http.get(self._url(address, "/challenge"))
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

    def challenge(self, address: str) -> str:
        r = self.http.get(self._url(address, "/challenge"))
        r.raise_for_status()
        return str(r.json()["challenge"])

    def restore(self, address: str, token: str) -> bytes:
        r = self.http.post(self._url(address, "/restore"), json={"token": token})
        r.raise_for_status()
        return base64.b64decode(r.json()["blob"])

    def release(self, address: str, time_ms: int, signature: str) -> bytes:
        r = self.http.post(self._url(address, "/release"), json={"time_ms": time_ms, "signature": signature})
        r.raise_for_status()
        return base64.b64decode(r.json()["blob"])

    def released(self) -> list[dict[str, Any]]:
        r = self.http.get(f"{self.service}/key-store/released")
        r.raise_for_status()
        return list(r.json()["released"])
