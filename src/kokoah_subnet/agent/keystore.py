"""The account key across restarts (specification v6, section 16).

The key is created inside the sealed machine and never leaves it in the clear. So that a restart does not strand
the funds, the harness stores it wrapped twice before it trades:

1. **Inner layer:** AES-256-GCM under a key derived (HKDF-SHA256) from a secret only the miner holds, given to the
   machine at start. Kokoah alone cannot open it.
2. **Outer layer:** a Kokoah Cloud KMS key that can be used only with a fresh attestation token of an approved
   harness image (Workload Identity Federation with an attribute condition on the image digest, the production
   build and the confidential hardware). The miner alone cannot open it: outside an approved sealed machine there
   is no such token.

The wrapped key is kept write-once by the owner's service under the account address. A restart with the same
secret and the same address unwraps it inside an approved machine and resumes the same account.
"""

from __future__ import annotations

import base64
from typing import Any, Protocol

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from eth_account import Account

from .attest import Attestor

INNER_MAGIC = b"KAK1"
STS_URL = "https://sts.googleapis.com/v1/token"
STS_AUDIENCE = "https://sts.googleapis.com"  # the audience Confidential Space tokens carry for Workload Identity
KMS_URL = "https://cloudkms.googleapis.com/v1"


class Sealer(Protocol):
    def encrypt(self, plain: bytes) -> bytes: ...
    def decrypt(self, sealed: bytes) -> bytes: ...


def _inner_key(seal_secret: bytes, address: str) -> bytes:
    if len(seal_secret) < 32:
        raise ValueError("the seal secret must be at least 32 bytes")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"kokoah-agent-seal-1 " + address.encode())
    return hkdf.derive(seal_secret)


def wrap(key: bytes, address: str, seal_secret: bytes, sealer: Sealer) -> bytes:
    import os

    nonce = os.urandom(12)
    inner = INNER_MAGIC + nonce + AESGCM(_inner_key(seal_secret, address)).encrypt(nonce, key, address.encode())
    return sealer.encrypt(inner)


def unwrap(blob: bytes, address: str, seal_secret: bytes, sealer: Sealer) -> bytes:
    inner = sealer.decrypt(blob)
    if not inner.startswith(INNER_MAGIC):
        raise ValueError("not a wrapped agent key")
    nonce, ct = inner[4:16], inner[16:]
    key = AESGCM(_inner_key(seal_secret, address)).decrypt(nonce, ct, address.encode())
    if Account.from_key(key).address.lower() != address.lower():
        raise ValueError("the unwrapped key belongs to another account")
    return key


class GcpKmsSealer:
    """The outer layer: Cloud KMS, reached with an access token exchanged for a fresh attestation token."""

    def __init__(self, attestor: Attestor, wif_provider: str, kms_key: str, http: httpx.Client | None = None) -> None:
        self.attestor = attestor
        self.wif_provider = (
            wif_provider  # //iam.googleapis.com/projects/N/locations/global/workloadIdentityPools/P/providers/Q
        )
        self.kms_key = kms_key  # projects/.../locations/.../keyRings/.../cryptoKeys/...
        self.http = http or httpx.Client(timeout=30)

    def _access_token(self) -> str:
        subject = self.attestor.token([], audience=STS_AUDIENCE)
        r = self.http.post(
            STS_URL,
            json={
                "audience": self.wif_provider,
                "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
                "requestedTokenType": "urn:ietf:params:oauth:token-type:access_token",
                "scope": "https://www.googleapis.com/auth/cloud-platform",
                "subjectTokenType": "urn:ietf:params:oauth:token-type:jwt",
                "subjectToken": subject,
            },
        )
        r.raise_for_status()
        return str(r.json()["access_token"])

    def _kms(self, op: str, field: str, data: bytes, out_field: str) -> bytes:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        r = self.http.post(
            f"{KMS_URL}/{self.kms_key}:{op}", json={field: base64.b64encode(data).decode()}, headers=headers
        )
        r.raise_for_status()
        return base64.b64decode(r.json()[out_field])

    def encrypt(self, plain: bytes) -> bytes:
        return self._kms("encrypt", "plaintext", plain, "ciphertext")

    def decrypt(self, sealed: bytes) -> bytes:
        return self._kms("decrypt", "ciphertext", sealed, "plaintext")


class DevelopmentSealer:
    """No outer layer (local runs only): the inner layer alone, so a development key never needs a cloud."""

    def encrypt(self, plain: bytes) -> bytes:
        return plain

    def decrypt(self, sealed: bytes) -> bytes:
        return sealed


class KeyVault:
    """Where wrapped keys are kept: the owner's service, write-once per account address."""

    def __init__(self, service: str, http: httpx.Client | None = None) -> None:
        self.service = service.rstrip("/")
        self.http = http or httpx.Client(timeout=30)

    def put(self, address: str, blob: bytes, hotkey: str) -> None:
        r = self.http.put(
            f"{self.service}/v1/keys/{address.lower()}",
            json={"blob": base64.b64encode(blob).decode(), "hotkey": hotkey},
        )
        r.raise_for_status()

    def get(self, address: str) -> bytes:
        r = self.http.get(f"{self.service}/v1/keys/{address.lower()}")
        r.raise_for_status()
        return base64.b64decode(r.json()["blob"])


def load_or_create(
    vault: KeyVault, sealer: Sealer, seal_secret: bytes, hotkey: str, restore: str | None = None
) -> tuple[bytes, str]:
    """The account key: unwrapped from the vault when `restore` names an address, otherwise newly created and
    stored wrapped before it is used. Returns (key, lowercase address)."""
    if restore:
        address = restore.lower()
        return unwrap(vault.get(address), address, seal_secret, sealer), address
    account: Any = Account.create()
    address = account.address.lower()
    key = bytes(account.key)
    vault.put(address, wrap(key, address, seal_secret, sealer), hotkey)
    return key, address
