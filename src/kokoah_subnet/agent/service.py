"""The subnet owner's agent service (specification v6, section 16): where miners submit agent code, where the
sealed machines fetch it, and where they keep their account key encrypted with the miner's secret.

    POST /agents            submit a bundle, signed by the miner's hotkey
    GET  /agents/{id}       a submission's record      GET /agents/{id}/bundle   its exact bytes
    GET  /images            the approved harness images
    PUT  /key-store/{address}                 store an encrypted account key (a sealed machine's attestation required)
    GET  /key-store/{address}/challenge       a fresh challenge for a restore
    POST /key-store/{address}/restore         the encrypted key back to a new sealed machine (attestation required)
    POST /key-store/{address}/release         the encrypted key to the miner (signed by the master address): public,
                                            and the account never earns the agent bonus again
    GET  /key-store/released                       every released account

A submission's id is the sha256 of the stored bytes (encrypted or not), so a machine can check it fetched exactly
what was submitted. A plain bundle is checked here (a zip with an entry.py, safe paths, within the size limits) and
its code fingerprint recorded; an encrypted one cannot be read here and is checked by the harness after it
decrypts it inside the sealed machine. Nothing here is trusted by validators: they compare the fingerprint the
attested harness reports with the one the miner committed on chain.

The hotkey signs `kokoah-agent-submit-1 sha256=<id> hotkey=<ss58>`. One submission per hotkey per 12 hours.
"""

from __future__ import annotations

import base64
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, Float, Integer, MetaData, String, Table, Text, create_engine, insert, select

from .bundle import MAX_BUNDLE_BYTES, check_zip, is_encrypted, sha256_hex
from .keystore import release_message, restore_digest, store_digest

SUBMIT_COOLDOWN_S = 12 * 3600
metadata = MetaData()
submissions = Table(
    "agent_submissions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("hotkey", String(64), nullable=False, index=True),
    Column("encrypted", Boolean, nullable=False),
    Column("code_sha256", String(64)),  # plain bundles only
    Column("bytes", Float, nullable=False),
    Column("submitted_at", Float, nullable=False),
)


keys = Table(
    "agent_keys",
    metadata,
    Column("address", String(42), primary_key=True),
    Column("hotkey", String(64), nullable=False),
    Column("master", String(42), nullable=False),
    Column("blob", Text, nullable=False),
    Column("stored_at", Float, nullable=False),
    Column("released_at", Float),  # when the miner took the key (then never an agent again)
)
key_events = Table(  # every store, restore and release, for the record
    "agent_key_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("address", String(42), nullable=False, index=True),
    Column("event", String(16), nullable=False),
    Column("at", Float, nullable=False),
    Column("detail", Text),
)
CHALLENGE_TTL_S = 300
MAX_KEY_COPY_CHARS = 512  # base64 of a key copy (a 64-byte ciphertext); anything bigger is not from a harness
WRAP_MAGIC = b"KSW1"


def wrap_blob(wrap_key: bytes, blob: bytes, address: str) -> bytes:
    """The service's own layer over a stored copy (already encrypted with the miner's secret): a stolen database
    or backup alone is not enough, the service's key is needed too. That key never goes into the backups."""
    nonce = os.urandom(12)
    return WRAP_MAGIC + nonce + AESGCM(wrap_key).encrypt(nonce, blob, b"kokoah-agent-wrap-1 " + address.encode())


def unwrap_blob(wrap_key: bytes | None, stored: bytes, address: str) -> bytes:
    if not stored.startswith(WRAP_MAGIC):
        return stored  # stored before the service had its own key
    if wrap_key is None:
        raise ValueError("this copy is wrapped with the service's key, and the service has none")
    try:
        return AESGCM(wrap_key).decrypt(stored[4:16], stored[16:], b"kokoah-agent-wrap-1 " + address.encode())
    except InvalidTag as exc:
        raise ValueError("the service's key does not open this copy (a different key file?)") from exc


SIGNED_REQUEST_WINDOW_MS = 300_000


def submit_message(blob_sha256: str, hotkey: str) -> str:
    return f"kokoah-agent-submit-1 sha256={blob_sha256} hotkey={hotkey}"


def default_verifier(message: str, signature: str, hotkey: str) -> bool:
    from bittensor.wallets import verify_message

    return bool(verify_message(message, signature, hotkey))


class StoredKey(BaseModel):
    blob: str  # base64 of the encrypted key
    master: str
    hotkey: str
    token: str  # attestation token whose nonce is the store digest


class Restore(BaseModel):
    token: str  # attestation token whose nonce is the restore digest of an outstanding challenge


class Release(BaseModel):
    time_ms: int
    signature: str  # EIP-191 signature of `release_message` by the master address


class Submission(BaseModel):
    hotkey: str
    bundle: str  # base64 of the bundle bytes (plain zip, or encrypted)
    signature: str  # hex sr25519 signature of `submit_message` by the hotkey


def create_app(
    data_dir: Path,
    db_url: str = "",
    images: list[str] | None = None,
    verify: Callable[[str, str, str], bool] = default_verifier,
    is_registered: Callable[[str], bool] | None = None,
    clock: Callable[[], float] = time.time,
    attestation_keys: Any = None,
    accept_unverified_machines: bool = False,
    wrap_key: bytes | None = None,
) -> FastAPI:
    """`attestation_keys`: the token keys (Google's when None). `is_registered` (hotkey -> registered on the subnet;
    the public service always sets it) gates submissions and stored keys; restores and releases never depend on it,
    so a miner who lost their registration still gets their account back. `wrap_key` (32 bytes, kept out of the
    backups) adds the service's own encryption over every stored copy. `accept_unverified_machines` is for local
    development only: any token is accepted for storing and restoring keys."""
    if wrap_key is not None and len(wrap_key) != 32:
        raise ValueError("the service's key must be 32 bytes")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "bundles").mkdir(exist_ok=True)
    engine = create_engine(db_url or f"sqlite:///{data_dir / 'agents.sqlite'}", future=True)
    metadata.create_all(engine)
    app = FastAPI(title="Kokoah subnet agent service")

    def require_registered(hotkey: str) -> None:
        if is_registered is None:
            return
        try:
            ok = is_registered(hotkey)
        except Exception as exc:  # noqa: BLE001 - the chain could not be read
            raise HTTPException(status_code=503, detail="the subnet's registrations cannot be read now") from exc
        if not ok:
            raise HTTPException(status_code=403, detail="the hotkey is not registered on the subnet")

    @app.post("/agents")
    def submit(body: Submission) -> dict[str, Any]:
        try:
            blob = base64.b64decode(body.bundle, validate=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="bundle must be base64") from exc
        if not blob or len(blob) > MAX_BUNDLE_BYTES + 64:
            raise HTTPException(status_code=413, detail=f"bundle must be 1 to {MAX_BUNDLE_BYTES} bytes")
        blob_id = sha256_hex(blob)
        if not verify(submit_message(blob_id, body.hotkey), body.signature, body.hotkey):
            raise HTTPException(status_code=403, detail="not signed by the hotkey")
        require_registered(body.hotkey)
        encrypted = is_encrypted(blob)
        code = None
        if not encrypted:
            try:
                check_zip(blob)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"bundle refused: {exc}") from exc
            code = blob_id
        with engine.begin() as c:
            last = c.execute(
                select(submissions.c.submitted_at)
                .where(submissions.c.hotkey == body.hotkey)
                .order_by(submissions.c.submitted_at.desc())
                .limit(1)
            ).scalar()
            if c.execute(select(submissions.c.id).where(submissions.c.id == blob_id)).first():
                return {"id": blob_id, "encrypted": encrypted, "code_sha256": code, "existing": True}
            if last is not None and clock() - float(last) < SUBMIT_COOLDOWN_S:
                raise HTTPException(status_code=429, detail="one submission per hotkey per 12 hours")
            (data_dir / "bundles" / blob_id).write_bytes(blob)
            c.execute(
                insert(submissions).values(
                    id=blob_id,
                    hotkey=body.hotkey,
                    encrypted=encrypted,
                    code_sha256=code,
                    bytes=len(blob),
                    submitted_at=clock(),
                )
            )
        return {"id": blob_id, "encrypted": encrypted, "code_sha256": code, "existing": False}

    @app.get("/agents/{blob_id}")
    def record(blob_id: str) -> dict[str, Any]:
        with engine.connect() as c:
            row = c.execute(select(submissions).where(submissions.c.id == blob_id)).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="no such submission")
        return dict(row)

    @app.get("/agents/{blob_id}/bundle")
    def bundle(blob_id: str) -> Response:
        if len(blob_id) != 64 or any(ch not in "0123456789abcdef" for ch in blob_id):
            raise HTTPException(status_code=400, detail="bad id")
        path = data_dir / "bundles" / blob_id
        if not path.exists():
            raise HTTPException(status_code=404, detail="no such submission")
        return Response(path.read_bytes(), media_type="application/octet-stream")

    @app.get("/images")
    def approved_images() -> dict[str, list[str]]:
        return {"images": list(images or [])}

    challenges: dict[str, tuple[str, float]] = {}
    key_source: list[Any] = [attestation_keys]

    def machine_proof(token: str, nonce: str) -> None:
        if accept_unverified_machines:
            return
        from .attest import AttestationError, google_keys, verify_token

        if key_source[0] is None:
            key_source[0] = google_keys()
        try:
            verify_token(token, key_source[0], list(images or []), [nonce])
        except AttestationError as exc:
            raise HTTPException(status_code=403, detail=f"not a genuine sealed machine: {exc}") from exc

    def event(c: Any, address: str, kind: str, detail: str = "") -> None:
        c.execute(insert(key_events).values(address=address, event=kind, at=clock(), detail=detail or None))

    def check_address(address: str) -> str:
        a = address.lower()
        if len(a) != 42 or not a.startswith("0x") or any(ch not in "0123456789abcdef" for ch in a[2:]):
            raise HTTPException(status_code=400, detail="bad address")
        return a

    @app.put("/key-store/{address}")
    def store_key(address: str, body: StoredKey) -> dict[str, Any]:
        a = check_address(address)
        if len(body.blob) > MAX_KEY_COPY_CHARS:
            raise HTTPException(status_code=413, detail="not a key copy")
        try:
            blob = base64.b64decode(body.blob, validate=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="blob must be base64") from exc
        require_registered(body.hotkey)
        machine_proof(body.token, store_digest(a, body.master, blob))
        stored = base64.b64encode(blob if wrap_key is None else wrap_blob(wrap_key, blob, a)).decode()
        with engine.begin() as c:
            if c.execute(select(keys.c.address).where(keys.c.address == a)).first():
                raise HTTPException(status_code=409, detail="a key is already kept for this address")
            c.execute(
                insert(keys).values(
                    address=a, hotkey=body.hotkey, master=body.master.lower(), blob=stored, stored_at=clock()
                )
            )
            event(c, a, "store", f"hotkey {body.hotkey}")
        return {"stored": a}

    def kept(c: Any, a: str) -> Any:
        row = c.execute(select(keys).where(keys.c.address == a)).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="no key kept for this address")
        return row

    def opened(row: Any) -> str:
        """The copy as the machine sent it (the service's own layer removed), base64."""
        try:
            return base64.b64encode(unwrap_blob(wrap_key, base64.b64decode(row["blob"]), row["address"])).decode()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/key-store/{address}/challenge")
    def challenge(address: str) -> dict[str, str]:
        """Only for an account whose copy is kept (and not released), so requests for made-up accounts leave
        nothing behind; expired challenges are dropped on the way."""
        a = check_address(address)
        with engine.connect() as c:
            if kept(c, a)["released_at"] is not None:
                raise HTTPException(status_code=410, detail="this key was released to the miner")
        now = clock()
        for stale in [k for k, (_, until) in challenges.items() if until < now]:
            del challenges[stale]
        value = secrets.token_hex(32)
        challenges[a] = (value, now + CHALLENGE_TTL_S)
        return {"challenge": value}

    @app.post("/key-store/{address}/restore")
    def restore(address: str, body: Restore) -> dict[str, Any]:
        a = check_address(address)
        outstanding = challenges.pop(a, None)
        if outstanding is None or outstanding[1] < clock():
            raise HTTPException(status_code=409, detail="ask for a fresh challenge first")
        machine_proof(body.token, restore_digest(a, outstanding[0]))
        with engine.begin() as c:
            row = kept(c, a)
            if row["released_at"] is not None:
                raise HTTPException(status_code=410, detail="this key was released to the miner")
            blob = opened(row)  # before anything is recorded: a copy the service cannot open changes nothing
            event(c, a, "restore")
        return {"address": a, "blob": blob}

    @app.post("/key-store/{address}/release")
    def release(address: str, body: Release) -> dict[str, Any]:
        a = check_address(address)
        if abs(clock() * 1000 - body.time_ms) > SIGNED_REQUEST_WINDOW_MS:
            raise HTTPException(status_code=403, detail="stale request")
        with engine.begin() as c:
            row = kept(c, a)
            try:
                signer = Account.recover_message(
                    encode_defunct(text=release_message(a, body.time_ms)),
                    signature=bytes.fromhex(body.signature.removeprefix("0x")),
                )
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=403, detail="bad signature") from exc
            if str(signer).lower() != row["master"]:
                raise HTTPException(status_code=403, detail="not signed by the account's master address")
            blob = opened(row)
            if row["released_at"] is None:
                c.execute(keys.update().where(keys.c.address == a).values(released_at=clock()))
            event(c, a, "release")
        return {"address": a, "blob": blob}

    @app.get("/key-store/released")
    def released() -> dict[str, list[dict[str, Any]]]:
        with engine.connect() as c:
            rows = c.execute(
                select(keys.c.address, keys.c.released_at)
                .where(keys.c.released_at.is_not(None))
                .order_by(keys.c.address)
            ).all()
        return {"released": [{"address": a, "released_at": t} for a, t in rows]}

    return app
