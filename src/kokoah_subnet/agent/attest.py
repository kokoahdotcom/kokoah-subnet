"""Attestation for the sealed machine (Google Cloud Confidential Space), both halves.

**In the machine** (`ConfidentialSpaceAttestor`): ask the launcher for a fresh token
(`POST /v1/token` on `/run/container_launcher/teeserver.sock`) whose nonces are the validator's challenge and the
binding digest. Google signs the token; it carries the container image digest, whether the machine is a debug
build, the hardware, and the nonces.

**At the validator** (`verify_token`): check Google's signature against its published keys, the issuer, audience
and expiry, that the machine is a production (non-debug) Confidential Space build on confidential hardware, that the
image is an approved harness, and that both nonces are present: the challenge (so the answer is fresh) and the
binding digest (so the token speaks for exactly this subnet, hotkey, Hyperliquid address and code fingerprint).
Nothing here trusts the operator or the miner.

**At the key store**: a token for storing or restoring an account key must carry exactly one nonce, the store or
restore digest. The public `/attestation` always signs two (the caller's challenge and the binding digest), so an
answer to anyone's challenge can never stand in for a key-store token, from any harness version. From harness 1.2.0
the machine's key-store tokens also carry their own audience (`KEYSTORE_AUDIENCE`), the only one the key store takes.

The binding digest is `sha256("kokoah-agent-1 netuid=<n> hotkey=<ss58> address=<0x..> code=<sha256>")`.
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import jwt

AUDIENCE = "kokoah-subnet-agent"
KEYSTORE_AUDIENCE = "kokoah-agent-keystore"  # only the supervisor asks for it, for its own key copy
ISSUER = "https://confidentialcomputing.googleapis.com"
DISCOVERY = "https://confidentialcomputing.googleapis.com/.well-known/openid-configuration"
LAUNCHER_SOCKET = "/run/container_launcher/teeserver.sock"
PRODUCTION_DEBUG_STATUS = "disabled-since-boot"
CONFIDENTIAL_HARDWARE = frozenset({"GCP_INTEL_TDX", "GCP_AMD_SEV_SNP", "GCP_AMD_SEV"})


def binding_digest(netuid: int, hotkey: str, address: str, code_sha256: str) -> str:
    text = f"kokoah-agent-1 netuid={netuid} hotkey={hotkey} address={address.lower()} code={code_sha256.lower()}"
    return hashlib.sha256(text.encode()).hexdigest()


class Attestor(Protocol):
    def token(self, nonces: list[str], audience: str | None = None) -> str: ...


class ConfidentialSpaceAttestor:
    """Fetches tokens from the Confidential Space launcher inside the machine."""

    def __init__(self, audience: str = AUDIENCE, socket_path: str = LAUNCHER_SOCKET) -> None:
        self.audience = audience
        self.socket_path = socket_path

    def token(self, nonces: list[str], audience: str | None = None) -> str:
        fields = {"audience": audience or self.audience, "token_type": "OIDC", "nonces": nonces}
        body = json.dumps(fields).encode()
        request = (
            b"POST /v1/token HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(30)
            s.connect(self.socket_path)
            s.sendall(request)
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
        head, _, payload = b"".join(chunks).partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 200 " not in status:
            raise RuntimeError(f"launcher refused the token request: {status.decode(errors='replace')}")
        if b"transfer-encoding: chunked" in head.lower():
            payload = _dechunk(payload)
        return payload.decode().strip()


def _dechunk(data: bytes) -> bytes:
    out = b""
    while data:
        size_line, _, rest = data.partition(b"\r\n")
        size = int(size_line.split(b";")[0], 16)
        if size == 0:
            break
        out += rest[:size]
        data = rest[size + 2 :]
    return out


@dataclass(frozen=True)
class Verified:
    image_digest: str
    hardware: str
    instance: str
    claims: dict[str, Any] = field(repr=False)


class AttestationError(Exception):
    pass


class KeySource(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


def google_keys() -> KeySource:
    """Google's published token-signing keys (fetched from the OIDC discovery document, cached by PyJWT)."""
    import httpx

    jwks_uri = httpx.get(DISCOVERY, timeout=20).json()["jwks_uri"]
    return jwt.PyJWKClient(jwks_uri, cache_keys=True)


def _nonces(claims: Mapping[str, Any]) -> set[str]:
    raw = claims.get("eat_nonce", [])
    return {raw} if isinstance(raw, str) else set(raw)


def verify_token(
    token: str,
    keys: KeySource,
    approved_images: Iterable[str],
    expected_nonces: Iterable[str],
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    hardware: Iterable[str] = CONFIDENTIAL_HARDWARE,
    now: float | None = None,
    exact_nonces: bool = False,
) -> Verified:
    """Every check a validator makes on a sealed machine's token; raises AttestationError on the first failure.
    `exact_nonces`: the token must carry the expected nonces and no other (the key store's check)."""
    try:
        key = keys.get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            key=getattr(key, "key", key),
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud"]},
            leeway=60,
        )
    except jwt.PyJWTError as exc:
        raise AttestationError(f"token signature or standard claims: {exc}") from exc
    if now is not None and claims["exp"] < now:
        raise AttestationError("token expired")
    if claims.get("swname") != "CONFIDENTIAL_SPACE":
        raise AttestationError(f"not a Confidential Space machine: {claims.get('swname')}")
    if claims.get("dbgstat") != PRODUCTION_DEBUG_STATUS:
        raise AttestationError(f"debug machine: {claims.get('dbgstat')}")
    if claims.get("hwmodel") not in set(hardware):
        raise AttestationError(f"not confidential hardware: {claims.get('hwmodel')}")
    if claims.get("secboot") is not True:
        raise AttestationError("secure boot is off")
    submods = claims.get("submods") or {}
    support = (submods.get("confidential_space") or {}).get("support_attributes") or []
    if "STABLE" not in support:
        raise AttestationError(f"not a stable production image: {support}")
    digest = str((submods.get("container") or {}).get("image_digest", ""))
    if digest not in set(approved_images):
        raise AttestationError(f"image {digest or '(none)'} is not an approved harness")
    expected = set(expected_nonces)
    missing = expected - _nonces(claims)
    if missing:
        raise AttestationError(f"token does not carry the expected nonces ({len(missing)} missing)")
    if exact_nonces and _nonces(claims) != expected:
        raise AttestationError("token carries other nonces than the expected ones")
    gce = submods.get("gce") or {}
    return Verified(digest, str(claims.get("hwmodel")), str(gce.get("instance_id", "")), claims)


class StaticKeys:
    """A fixed key (tests, and local runs with a development attestor)."""

    def __init__(self, public_key: Any) -> None:
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        return self.public_key


class DevelopmentAttestor:
    """Signs tokens shaped like Confidential Space's with a local key. Never accepted by a real validator (its key
    is not Google's); used by tests and by local runs of the harness outside a sealed machine."""

    def __init__(self, private_key: Any, image_digest: str, audience: str = AUDIENCE) -> None:
        self.private_key = private_key
        self.image_digest = image_digest
        self.audience = audience

    def token(self, nonces: list[str], audience: str | None = None) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": audience or self.audience,
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "swname": "CONFIDENTIAL_SPACE",
            "dbgstat": PRODUCTION_DEBUG_STATUS,
            "hwmodel": "GCP_INTEL_TDX",
            "secboot": True,
            "eat_nonce": nonces if len(nonces) != 1 else nonces[0],
            "submods": {
                "container": {"image_digest": self.image_digest},
                "confidential_space": {"support_attributes": ["LATEST", "STABLE", "USABLE"]},
                "gce": {"instance_id": "dev"},
            },
        }
        return jwt.encode(claims, self.private_key, algorithm="RS256")
