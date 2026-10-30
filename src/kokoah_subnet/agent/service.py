"""The subnet owner's agent service (specification section 16): where miners submit agent code, where the
sealed machines fetch it, and where they keep their account key encrypted with the miner's secret.

    POST /agents            submit a bundle (raw body; hotkey, sha256 and the hotkey's signature as headers)
    GET  /agents/{id}       a submission's record      GET /agents/{id}/bundle   its exact bytes
    GET  /images            the approved harness images
    PUT  /key-store/{address}                 store an encrypted account key (a sealed machine's attestation required)
    GET  /key-store/{address}/challenge       a fresh challenge for a restore
    POST /key-store/{address}/restore         the encrypted key back to a new sealed machine (attestation required)
    POST /key-store/{address}/release         the encrypted key to the miner (signed by the master address): public,
                                            and the account never earns the agent bonus again
    GET  /key-store/released                       every released account
    GET  /unlinks/message?hotkey=&address=      the text a linked address signs to end its link (with the time)
    POST /unlinks                                an address's signed unlink (spec section 14), of a link a binding
                                                 on chain has named
    GET  /unlinks[?address=][&since=&limit=]     every unlink accepted, oldest received first, in pages, for
                                                 validators (they check each signature)

A submission's id is the sha256 of the stored bytes (encrypted or not), so a machine can check it fetched exactly
what was submitted. A plain bundle is checked here (a zip with an entry.py, safe paths, within the size limits) and
its code fingerprint recorded; an encrypted one cannot be read here and is checked by the harness after it
decrypts it inside the sealed machine. Nothing here is trusted by validators: they compare the fingerprint the
attested harness reports with the one the miner committed on chain.

The hotkey signs `kokoah-agent-submit-2 sha256=<id> hotkey=<ss58> time=<unix ms>` (within 5 minutes of the
service's clock). One submission per hotkey per 12 hours, and at most 6 uploads an hour, accepted or refused.
Bundles nobody can run any more (older than a hotkey's newest five, or of a hotkey that left the subnet a month ago)
are deleted, unless a kept key copy names them.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    insert,
    select,
)
from starlette.concurrency import run_in_threadpool

from ..binding.unlink import Unlink, unlink_message
from .bundle import MAX_BUNDLE_BYTES, check_zip, is_encrypted
from .keystore import PERMIT_DAYS, parse_permit, release_message, restore_digest, start_message, store_digest

log = logging.getLogger("kokoah_subnet.agent.service")
SUBMIT_COOLDOWN_S = 12 * 3600
SUBMIT_ATTEMPTS_PER_HOUR = 6  # uploads one hotkey may send, accepted or refused (each one is read and checked)
UPLOADS_AT_ONCE = 4  # bodies received at the same time; more are told to try again
KEEP_BUNDLES_PER_HOTKEY = 5  # older bundles are deleted unless an unreleased key copy names them
KEEP_LEFT_BUNDLES_S = 30 * 86_400  # a bundle of a hotkey no longer registered, after this long
PRUNE_EVERY_S = 6 * 3600
UNLINK_PAGE = 5000  # the most unlinks one answer lists
BINDINGS_REFRESH_S = 30.0  # an unlink of a link not seen yet reads the chain's bindings again after this long
BINDINGS_POLL_S = 600.0  # and they are read this often anyway, so every link a binding names is remembered
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
permits = Table(  # every start permission used: each one files one key copy, once
    "agent_permits",
    metadata,
    Column("signature", String(200), primary_key=True),
    Column("hotkey", String(64), nullable=False, index=True),
    Column("address", String(42), nullable=False),
    Column("used_at", Float, nullable=False),
)
unlinks = Table(  # every unlink accepted: a linked address ended its link to a hotkey (the first one per pair counts)
    "subnet_unlinks",
    metadata,
    Column("hotkey", String(64), primary_key=True),
    Column("address", String(42), primary_key=True),
    Column("time_ms", BigInteger, nullable=False),
    Column("signature", String(200), nullable=False),
    Column("received_at", Float, nullable=False),
)
linked = Table(  # every link a binding on chain has named (hotkey and address): the links an unlink can end
    "subnet_links_seen",
    metadata,
    Column("hotkey", String(64), primary_key=True),
    Column("address", String(42), primary_key=True),
    Column("seen_at", Float, nullable=False),
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
CHALLENGE_TTL_S = 300  # a restore challenge's window; the one before it is accepted too
MAX_TOKEN_CHARS = 16_384
MAX_KEY_COPY_CHARS = 512  # base64 of a key copy (a 64-byte ciphertext); anything bigger is not from a harness
MAX_COPIES_PER_HOTKEY = 50  # kept, unreleased copies one hotkey may file (each needs its own signed permission)
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
UNLINK_WINDOW_MS = 600_000  # an unlink's time must be this close to the service's clock (wallets can be slow)
SS58_CHARS = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def submit_message(blob_sha256: str, hotkey: str, time_ms: int) -> str:
    return f"kokoah-agent-submit-2 sha256={blob_sha256} hotkey={hotkey} time={time_ms}"


def used_token(token: str) -> str:
    """A restore token's record, the same for every spelling of the token. The token's signature covers its header
    and payload exactly as sent, but the signature itself decodes to the same bytes from many spellings (the unused
    low bits of its last character, characters the decoder skips), and each one passes the check; so the record is
    taken over the signature written out again in its one plain form. A token already in that form, as issued, keeps
    the record it always had."""
    raw = token.encode()
    signed, dot, signature = raw.rpartition(b".")
    if dot:
        try:  # decoded exactly as the token check decodes it
            decoded = base64.urlsafe_b64decode(signature + b"=" * (-len(signature) % 4))
        except ValueError:  # it fails the token check too
            pass
        else:
            raw = signed + b"." + base64.urlsafe_b64encode(decoded).rstrip(b"=")
    return f"token {hashlib.sha256(raw).hexdigest()}"


def default_verifier(message: str, signature: str, hotkey: str) -> bool:
    from bittensor.wallets import verify_message

    return bool(verify_message(message, signature, hotkey))


class StoredKey(BaseModel):
    blob: str  # base64 of the encrypted key
    master: str
    hotkey: str
    token: str  # attestation token whose nonce is the store digest
    netuid: int
    submission: str  # the code the machine runs
    permit: str  # `<time_ms>:<signature>`: the hotkey's signature of `start_message`


class Restore(BaseModel):
    token: str  # attestation token whose nonce is the restore digest of an outstanding challenge


class Release(BaseModel):
    time_ms: int
    signature: str  # EIP-191 signature of `release_message` by the master address


class UnlinkRequest(BaseModel):
    hotkey: str
    address: str
    time_ms: int
    signature: str  # EIP-191 signature of `unlink_message` by the address


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
    netuid: int | None = None,
    bound_pairs: Callable[[], Iterable[tuple[str, str]]] | None = None,
) -> FastAPI:
    """`attestation_keys`: the token keys (Google's when None). `is_registered` (hotkey -> registered on the subnet;
    the public service always sets it) gates submissions and stored keys; restores and releases never depend on it,
    so a miner who lost their registration still gets their account back. `wrap_key` (32 bytes, kept out of the
    backups) adds the service's own encryption over every stored copy. `netuid`: the subnet the service serves (a
    start permission must name it). A key copy is filed only with the hotkey owner's start permission: signed by
    the hotkey, for this subnet, at most PERMIT_DAYS old, used once. `bound_pairs` (the public service always sets
    it): the (hotkey, address) links the subnet's bindings on chain name now; every one seen is remembered, and only
    those can be unlinked. `accept_unverified_machines` is for local development only: any token is accepted for
    storing and restoring keys."""
    if wrap_key is not None and len(wrap_key) != 32:
        raise ValueError("the service's key must be 32 bytes")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "bundles").mkdir(exist_ok=True)
    engine = create_engine(db_url or f"sqlite:///{data_dir / 'agents.sqlite'}", future=True)
    metadata.create_all(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def upkeep() -> None:
            while True:
                for job in (prune_bundles, poll_links):
                    try:
                        await anyio.to_thread.run_sync(job)
                    except Exception:  # noqa: BLE001 - tried again next round
                        log.exception("%s failed", job.__name__)
                await asyncio.sleep(min(PRUNE_EVERY_S, BINDINGS_POLL_S))

        task = asyncio.create_task(upkeep())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Kokoah subnet agent service", lifespan=lifespan)

    def require_registered(hotkey: str) -> None:
        if is_registered is None:
            return
        try:
            ok = is_registered(hotkey)
        except Exception as exc:  # noqa: BLE001 - the chain could not be read
            raise HTTPException(status_code=503, detail="the subnet's registrations cannot be read now") from exc
        if not ok:
            raise HTTPException(status_code=403, detail="the hotkey is not registered on the subnet")

    submit_lock = asyncio.Lock()
    uploading = asyncio.Semaphore(UPLOADS_AT_ONCE)
    checking = anyio.CapacityLimiter(2)  # bundle checks run off the event loop, two at a time
    attempts: dict[str, list[float]] = {}  # hotkey -> its upload times within the last hour
    attempts_lock = threading.Lock()

    def count_attempt(hotkey: str) -> None:
        now = clock()
        with attempts_lock:
            recent = [t for t in attempts.get(hotkey, []) if now - t < 3600]
            if len(recent) >= SUBMIT_ATTEMPTS_PER_HOUR:
                attempts[hotkey] = recent
                raise HTTPException(status_code=429, detail=f"{SUBMIT_ATTEMPTS_PER_HOUR} uploads an hour per hotkey")
            attempts[hotkey] = [*recent, now]
            for hk in [hk for hk, ts in attempts.items() if not ts or now - ts[-1] >= 3600]:
                del attempts[hk]

    def in_use(c: Any) -> set[str]:
        """The submissions an unreleased key copy names: a machine restoring that account fetches them again."""
        q = (
            select(key_events.c.detail)
            .select_from(key_events.join(keys, keys.c.address == key_events.c.address))
            .where(key_events.c.event == "store", keys.c.released_at.is_(None))
        )
        return {str(d).rsplit("submission ", 1)[-1] for (d,) in c.execute(q).all() if d and "submission " in str(d)}

    def drop_old_bundles(c: Any, hotkey: str) -> None:
        """Keep a hotkey's newest KEEP_BUNDLES_PER_HOTKEY bundles and any bundle an unreleased key copy names."""
        rows = c.execute(
            select(submissions.c.id).where(submissions.c.hotkey == hotkey).order_by(submissions.c.submitted_at.desc())
        ).all()
        needed = in_use(c)
        for (old,) in rows[KEEP_BUNDLES_PER_HOTKEY:]:
            if old not in needed:
                (data_dir / "bundles" / old).unlink(missing_ok=True)

    def prune_bundles() -> int:
        """Delete the bundles nobody can run any more: beyond a hotkey's newest KEEP_BUNDLES_PER_HOTKEY, and those of
        a hotkey no longer registered once KEEP_LEFT_BUNDLES_S have passed; never one an unreleased key copy names.
        Their records stay. Returns how many files were deleted."""
        now, gone = clock(), 0
        with engine.connect() as c:
            needed = in_use(c)
            rows = c.execute(
                select(submissions.c.id, submissions.c.hotkey, submissions.c.submitted_at).order_by(
                    submissions.c.hotkey, submissions.c.submitted_at.desc()
                )
            ).all()
        rank: dict[str, int] = {}
        for blob_id, hotkey, submitted_at in rows:
            rank[hotkey] = rank.get(hotkey, 0) + 1
            path = data_dir / "bundles" / blob_id
            if blob_id in needed or not path.exists():
                continue
            drop = rank[hotkey] > KEEP_BUNDLES_PER_HOTKEY
            if not drop and is_registered is not None and now - float(submitted_at) > KEEP_LEFT_BUNDLES_S:
                try:
                    drop = not is_registered(hotkey)
                except Exception:  # noqa: BLE001 - the chain cannot be read now: keep it
                    drop = False
            if drop:
                path.unlink(missing_ok=True)
                gone += 1
        return gone

    app.state.prune_bundles = prune_bundles

    @app.post("/agents")
    async def submit(request: Request) -> dict[str, Any]:
        """A bundle as the raw body, with `X-Kokoah-Hotkey`, `X-Kokoah-Bundle-Sha256` and `X-Kokoah-Signature` (the
        hotkey's signature of `submit_message`, with its `X-Kokoah-Time`). Everything about the sender is checked
        before the body is read, and the upload is counted; the body is then streamed to disk under the size cap and
        must have the signed hash."""
        hotkey = request.headers.get("x-kokoah-hotkey", "")
        signature = request.headers.get("x-kokoah-signature", "")
        blob_id = request.headers.get("x-kokoah-bundle-sha256", "").lower()
        if len(blob_id) != 64 or any(ch not in "0123456789abcdef" for ch in blob_id) or not hotkey or not signature:
            raise HTTPException(
                status_code=400, detail="the hotkey, the bundle's sha256 and the signature are required"
            )
        try:
            signed_at = int(request.headers.get("x-kokoah-time", ""))
        except ValueError:
            raise HTTPException(status_code=400, detail="the signed time (X-Kokoah-Time) is required") from None
        try:
            size = int(request.headers.get("content-length", ""))
        except ValueError:
            raise HTTPException(status_code=411, detail="the bundle's length is required") from None
        if not 0 < size <= MAX_BUNDLE_BYTES + 64:
            raise HTTPException(status_code=413, detail=f"bundle must be 1 to {MAX_BUNDLE_BYTES} bytes")
        if abs(clock() * 1000 - signed_at) > SIGNED_REQUEST_WINDOW_MS:
            raise HTTPException(status_code=403, detail="the signed time is not within 5 minutes of now: sign again")
        if not verify(submit_message(blob_id, hotkey, signed_at), signature, hotkey):
            raise HTTPException(status_code=403, detail="not signed by the hotkey")
        await run_in_threadpool(require_registered, hotkey)
        with engine.connect() as c:
            existing = c.execute(select(submissions).where(submissions.c.id == blob_id)).mappings().first()
            last = c.execute(
                select(submissions.c.submitted_at)
                .where(submissions.c.hotkey == hotkey)
                .order_by(submissions.c.submitted_at.desc())
                .limit(1)
            ).scalar()
        if existing is not None and (data_dir / "bundles" / blob_id).exists():
            return {
                "id": blob_id,
                "encrypted": existing["encrypted"],
                "code_sha256": existing["code_sha256"],
                "existing": True,
            }
        if last is not None and clock() - float(last) < SUBMIT_COOLDOWN_S:
            raise HTTPException(status_code=429, detail="one submission per hotkey per 12 hours")
        count_attempt(hotkey)
        if uploading.locked():
            raise HTTPException(status_code=503, detail="the service is receiving other bundles: try again shortly")
        async with uploading:
            return await receive(request, hotkey, blob_id, size)

    async def receive(request: Request, hotkey: str, blob_id: str, size: int) -> dict[str, Any]:
        incoming = data_dir / "bundles" / f".incoming-{secrets.token_hex(8)}"
        try:
            digest = hashlib.sha256()
            received = 0
            with incoming.open("wb") as f:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > size or received > MAX_BUNDLE_BYTES + 64:
                        raise HTTPException(status_code=413, detail="the body is longer than declared")
                    digest.update(chunk)
                    f.write(chunk)
            if digest.hexdigest() != blob_id:
                raise HTTPException(status_code=400, detail="the body does not have the signed sha256")
            blob = incoming.read_bytes()
            encrypted = is_encrypted(blob)
            code = None
            if not encrypted:
                try:
                    await anyio.to_thread.run_sync(check_zip, blob, limiter=checking)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=f"bundle refused: {exc}") from exc
                code = blob_id
            async with submit_lock:  # the cooldown check and the insert together
                with engine.begin() as c:
                    if c.execute(select(submissions.c.id).where(submissions.c.id == blob_id)).first():
                        target = data_dir / "bundles" / blob_id
                        if not target.exists():  # a deleted bundle sent again: kept again, as submitted now
                            incoming.replace(target)
                            c.execute(
                                submissions.update().where(submissions.c.id == blob_id).values(submitted_at=clock())
                            )
                        return {"id": blob_id, "encrypted": encrypted, "code_sha256": code, "existing": True}
                    last = c.execute(
                        select(submissions.c.submitted_at)
                        .where(submissions.c.hotkey == hotkey)
                        .order_by(submissions.c.submitted_at.desc())
                        .limit(1)
                    ).scalar()
                    if last is not None and clock() - float(last) < SUBMIT_COOLDOWN_S:
                        raise HTTPException(status_code=429, detail="one submission per hotkey per 12 hours")
                    incoming.replace(data_dir / "bundles" / blob_id)
                    c.execute(
                        insert(submissions).values(
                            id=blob_id,
                            hotkey=hotkey,
                            encrypted=encrypted,
                            code_sha256=code,
                            bytes=len(blob),
                            submitted_at=clock(),
                        )
                    )
                    drop_old_bundles(c, hotkey)
        finally:
            incoming.unlink(missing_ok=True)
        return {"id": blob_id, "encrypted": encrypted, "code_sha256": code, "existing": False}

    @app.get("/agents/{blob_id}")
    def record(blob_id: str) -> dict[str, Any]:
        with engine.connect() as c:
            row = c.execute(select(submissions).where(submissions.c.id == blob_id)).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="no such submission")
        return dict(row)

    @app.get("/agents/{blob_id}/bundle")
    def bundle(blob_id: str) -> FileResponse:
        if len(blob_id) != 64 or any(ch not in "0123456789abcdef" for ch in blob_id):
            raise HTTPException(status_code=400, detail="bad id")
        path = data_dir / "bundles" / blob_id
        if not path.exists():
            raise HTTPException(status_code=404, detail="no such submission (or no longer kept)")
        return FileResponse(path, media_type="application/octet-stream")  # streamed, never read whole

    @app.get("/images")
    def approved_images() -> dict[str, list[str]]:
        return {"images": list(images or [])}

    # restore challenges are derived, not stored: the same for everyone asking within a window, so nobody can replace
    # or use up a machine's challenge; a token that restored a key is not accepted again. With the service's own key
    # both outlast a restart: the challenge key comes from it, and every restore records its token.
    if wrap_key is not None:
        challenge_key = hmac.new(wrap_key, b"kokoah-restore-challenge-key-1", hashlib.sha256).digest()
    else:
        challenge_key = secrets.token_bytes(32)
    restore_lock = threading.Lock()

    def challenge_value(address: str, window: int) -> str:
        text = f"kokoah-restore-challenge-1 {address} {window}".encode()
        return hmac.new(challenge_key, text, hashlib.sha256).hexdigest()

    key_source: list[Any] = [attestation_keys]

    def proof_error(token: str, nonce: str) -> str | None:
        """Why the token is not a genuine sealed machine's answer to `nonce` (None when it is)."""
        if accept_unverified_machines:
            return None
        from .attest import KEYSTORE_AUDIENCE, AttestationError, google_keys, verify_token

        if key_source[0] is None:
            key_source[0] = google_keys()
        # the key store's own audience and exactly the one digest, never a public /attestation answer (which always
        # carries two nonces and the default audience)
        try:
            verify_token(
                token, key_source[0], list(images or []), [nonce], audience=KEYSTORE_AUDIENCE, exact_nonces=True
            )
        except AttestationError as exc:
            return str(exc)
        return None

    def machine_proof(token: str, nonce: str) -> None:
        error = proof_error(token, nonce)
        if error is not None:
            raise HTTPException(status_code=403, detail=f"not a genuine sealed machine: {error}")

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
        master = check_address(body.master)  # the only withdrawal destination: never anything but an address
        if len(body.blob) > MAX_KEY_COPY_CHARS:
            raise HTTPException(status_code=413, detail="not a key copy")
        try:
            blob = base64.b64decode(body.blob, validate=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="blob must be base64") from exc
        try:
            signed_at, signature = parse_permit(body.permit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if netuid is not None and body.netuid != netuid:
            raise HTTPException(
                status_code=403, detail=f"the start permission is for subnet {body.netuid}, not {netuid}"
            )
        now_ms = clock() * 1000
        if signed_at > now_ms + SIGNED_REQUEST_WINDOW_MS or now_ms - signed_at > PERMIT_DAYS * 86_400_000:
            raise HTTPException(status_code=403, detail="the start permission has expired: sign a new one")
        message = start_message(body.netuid, body.hotkey, body.master, body.submission, signed_at)
        if not verify(message, signature, body.hotkey):
            raise HTTPException(status_code=403, detail="the start permission is not signed by the hotkey")
        require_registered(body.hotkey)
        with engine.connect() as c:
            if not c.execute(select(submissions.c.id).where(submissions.c.id == body.submission)).first():
                raise HTTPException(status_code=403, detail="the start permission names an unknown submission")
            if c.execute(select(permits.c.signature).where(permits.c.signature == signature)).first():
                raise HTTPException(status_code=409, detail="this start permission was used already: one per machine")
            count = c.execute(
                select(func.count()).select_from(keys).where(keys.c.hotkey == body.hotkey, keys.c.released_at.is_(None))
            ).scalar_one()
            if count >= MAX_COPIES_PER_HOTKEY:
                raise HTTPException(status_code=429, detail=f"{MAX_COPIES_PER_HOTKEY} key copies kept for this hotkey")
        machine_proof(body.token, store_digest(a, body.master, blob, body.hotkey, body.submission, body.permit))
        stored = base64.b64encode(blob if wrap_key is None else wrap_blob(wrap_key, blob, a)).decode()
        with engine.begin() as c:
            if c.execute(select(keys.c.address).where(keys.c.address == a)).first():
                raise HTTPException(status_code=409, detail="a key is already kept for this address")
            if c.execute(select(permits.c.signature).where(permits.c.signature == signature)).first():
                raise HTTPException(status_code=409, detail="this start permission was used already: one per machine")
            c.execute(insert(keys).values(address=a, hotkey=body.hotkey, master=master, blob=stored, stored_at=clock()))
            c.execute(insert(permits).values(signature=signature, hotkey=body.hotkey, address=a, used_at=clock()))
            event(c, a, "store", f"hotkey {body.hotkey} submission {body.submission}")
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
        """Only for an account whose copy is kept (and not released). The value is derived from the service's own
        key, the account and the time window, so asking changes nothing."""
        a = check_address(address)
        with engine.connect() as c:
            if kept(c, a)["released_at"] is not None:
                raise HTTPException(status_code=410, detail="this key was released to the miner")
        return {"challenge": challenge_value(a, int(clock() // CHALLENGE_TTL_S))}

    @app.post("/key-store/{address}/restore")
    def restore(address: str, body: Restore) -> dict[str, Any]:
        a = check_address(address)
        if not body.token or len(body.token) > MAX_TOKEN_CHARS:
            raise HTTPException(status_code=400, detail="bad token")
        now = clock()
        used = used_token(body.token)

        def used_already(c: Any) -> bool:
            q = select(key_events.c.id).where(
                key_events.c.address == a, key_events.c.event == "restore", key_events.c.detail == used
            )
            return c.execute(q).first() is not None

        with engine.connect() as c:
            if used_already(c):
                raise HTTPException(status_code=403, detail="this token was used already")
        window = int(now // CHALLENGE_TTL_S)  # the current challenge, or the one just before it
        errors = [proof_error(body.token, restore_digest(a, challenge_value(a, w))) for w in (window, window - 1)]
        if all(e is not None for e in errors):
            raise HTTPException(status_code=403, detail=f"not a genuine sealed machine's answer: {errors[0]}")
        with restore_lock, engine.begin() as c:  # the token is checked and recorded together
            if used_already(c):
                raise HTTPException(status_code=403, detail="this token was used already")
            row = kept(c, a)
            if row["released_at"] is not None:
                raise HTTPException(status_code=410, detail="this key was released to the miner")
            blob = opened(row)  # before anything is recorded: a copy the service cannot open changes nothing
            event(c, a, "restore", used)
        # the record's master and hotkey: the machine resumes the account only for those, never for start settings
        # that name someone else (a secret alone must not let anyone redirect the account's withdrawals)
        return {"address": a, "blob": blob, "master": row["master"], "hotkey": row["hotkey"]}

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
            except Exception as exc:  # noqa: BLE001 - a signature that cannot be read is refused
                raise HTTPException(status_code=403, detail="bad signature") from exc
            if str(signer).lower() != row["master"]:
                raise HTTPException(status_code=403, detail="not signed by the account's master address")
            blob = opened(row)
            if row["released_at"] is None:
                c.execute(keys.update().where(keys.c.address == a).values(released_at=clock()))
            event(c, a, "release")
        return {"address": a, "blob": blob}

    def check_hotkey(hotkey: str) -> str:
        if not 46 <= len(hotkey) <= 48 or not set(hotkey) <= SS58_CHARS:
            raise HTTPException(status_code=400, detail="bad hotkey")
        return hotkey

    def unlink_row(row: Any, received_at: float | None = None) -> dict[str, Any]:
        u = Unlink(row.hotkey, row.address, int(row.time_ms), row.signature)
        return {
            "hotkey": u.hotkey,
            "address": u.address,
            "time_ms": u.time_ms,
            "signature": u.signature,
            "effective_day": u.effective_day,
            "received_at": getattr(row, "received_at", received_at),
        }

    links_lock = threading.Lock()
    links_read = [float("-inf")]  # when the chain's bindings were last read (the service's clock)

    def read_links() -> None:
        """The links the chain's bindings name now, added to those remembered."""
        assert bound_pairs is not None
        current = {(str(hk), str(a).lower()) for hk, a in bound_pairs()}
        links_read[0] = clock()
        with engine.begin() as c:
            seen = {(hk, a) for hk, a in c.execute(select(linked.c.hotkey, linked.c.address)).all()}
            new = [{"hotkey": hk, "address": a, "seen_at": clock()} for hk, a in sorted(current - seen)]
            if new:
                c.execute(insert(linked), new)

    def poll_links() -> None:
        if bound_pairs is not None:
            with links_lock:
                read_links()

    def link_seen(hotkey: str, address: str) -> bool:
        """Whether a binding on chain has named this link (always, without `bound_pairs`: development)."""
        if bound_pairs is None:
            return True

        def known() -> bool:
            q = select(linked.c.hotkey).where(linked.c.hotkey == hotkey, linked.c.address == address)
            with engine.connect() as c:
                return c.execute(q).first() is not None

        if known():
            return True
        with links_lock:
            if clock() - links_read[0] >= BINDINGS_REFRESH_S:
                try:
                    read_links()
                except Exception as exc:  # noqa: BLE001 - the chain could not be read
                    raise HTTPException(status_code=503, detail="the subnet's bindings cannot be read now") from exc
        return known()

    def require_link(hotkey: str, address: str) -> None:
        if not link_seen(hotkey, address):
            raise HTTPException(status_code=403, detail="no binding on chain links this address to this hotkey")

    @app.get("/unlinks/message")
    def unlink_text(hotkey: str, address: str) -> dict[str, Any]:
        if netuid is None:
            raise HTTPException(status_code=503, detail="this service serves no subnet")
        hk, a = check_hotkey(hotkey), check_address(address)
        require_registered(hk)
        require_link(hk, a)
        now_ms = int(clock() * 1000)
        return {"message": unlink_message(netuid, hk, a, now_ms), "time_ms": now_ms}

    @app.post("/unlinks")
    def unlink(body: UnlinkRequest) -> dict[str, Any]:
        if netuid is None:
            raise HTTPException(status_code=503, detail="this service serves no subnet")
        hk, a = check_hotkey(body.hotkey), check_address(body.address)
        if len(body.signature) > 200:
            raise HTTPException(status_code=400, detail="bad signature")
        with engine.connect() as c:
            row = c.execute(select(unlinks).where(unlinks.c.hotkey == hk, unlinks.c.address == a)).first()
        if row is not None:
            return {**unlink_row(row), "existing": True}
        if abs(clock() * 1000 - body.time_ms) > UNLINK_WINDOW_MS:
            raise HTTPException(status_code=400, detail="the time is not within 10 minutes of now: sign a new message")
        u = Unlink(hk, a, body.time_ms, body.signature)
        if not u.signed_by_address(netuid):
            raise HTTPException(status_code=403, detail="not signed by the address")
        require_registered(hk)
        require_link(hk, a)
        received_at = clock()
        with engine.begin() as c:
            row = c.execute(select(unlinks).where(unlinks.c.hotkey == hk, unlinks.c.address == a)).first()
            if row is not None:
                return {**unlink_row(row), "existing": True}
            c.execute(insert(unlinks).values(hotkey=hk, address=a, time_ms=u.time_ms, signature=u.signature,
                                             received_at=received_at))  # fmt: skip
        return {**unlink_row(u, received_at), "existing": False}

    @app.get("/unlinks")
    def unlink_list(address: str | None = None, since: float = 0.0, limit: int = UNLINK_PAGE) -> dict[str, Any]:
        """Oldest received first, from `since` (a `received_at`, included), at most `limit` (UNLINK_PAGE at most);
        `next` is the `since` of the next page, or null on the last one."""
        limit = max(1, min(limit, UNLINK_PAGE))
        q = (
            select(unlinks)
            .where(unlinks.c.received_at >= since)
            .order_by(unlinks.c.received_at, unlinks.c.hotkey, unlinks.c.address)
            .limit(limit)
        )
        if address is not None:
            q = q.where(unlinks.c.address == check_address(address))
        with engine.connect() as c:
            rows = c.execute(q).all()
        more = len(rows) == limit
        return {"unlinks": [unlink_row(r) for r in rows], "next": rows[-1].received_at if more else None}

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
