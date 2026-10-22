"""The subnet owner's agent service (specification v6, section 16): where miners submit agent code and where the
sealed machines fetch it.

    POST /v1/agents            submit a bundle, signed by the miner's hotkey
    GET  /v1/agents/{id}       a submission's record      GET /v1/agents/{id}/bundle   its exact bytes
    GET  /v1/images            the approved harness images

A submission's id is the sha256 of the stored bytes (encrypted or not), so a machine can check it fetched exactly
what was submitted. A plain bundle is checked here (a zip with an entry.py, safe paths, within the size limits) and
its code fingerprint recorded; an encrypted one cannot be read here and is checked by the harness after it
decrypts it inside the sealed machine. Nothing here is trusted by validators: they compare the fingerprint the
attested harness reports with the one the miner committed on chain.

The hotkey signs `kokoah-agent-submit-1 sha256=<id> hotkey=<ss58>`. One submission per hotkey per 12 hours.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, Float, MetaData, String, Table, create_engine, insert, select

from .bundle import MAX_BUNDLE_BYTES, check_zip, is_encrypted, sha256_hex

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


def submit_message(blob_sha256: str, hotkey: str) -> str:
    return f"kokoah-agent-submit-1 sha256={blob_sha256} hotkey={hotkey}"


def default_verifier(message: str, signature: str, hotkey: str) -> bool:
    from bittensor.wallets import verify_message

    return bool(verify_message(message, signature, hotkey))


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
) -> FastAPI:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "bundles").mkdir(exist_ok=True)
    engine = create_engine(db_url or f"sqlite:///{data_dir / 'agents.sqlite'}", future=True)
    metadata.create_all(engine)
    app = FastAPI(title="Kokoah subnet agent service")

    @app.post("/v1/agents")
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
        if is_registered is not None and not is_registered(body.hotkey):
            raise HTTPException(status_code=403, detail="the hotkey is not registered on the subnet")
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

    @app.get("/v1/agents/{blob_id}")
    def record(blob_id: str) -> dict[str, Any]:
        with engine.connect() as c:
            row = c.execute(select(submissions).where(submissions.c.id == blob_id)).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="no such submission")
        return dict(row)

    @app.get("/v1/agents/{blob_id}/bundle")
    def bundle(blob_id: str) -> Response:
        if len(blob_id) != 64 or any(ch not in "0123456789abcdef" for ch in blob_id):
            raise HTTPException(status_code=400, detail="bad id")
        path = data_dir / "bundles" / blob_id
        if not path.exists():
            raise HTTPException(status_code=404, detail="no such submission")
        return Response(path.read_bytes(), media_type="application/octet-stream")

    @app.get("/v1/images")
    def approved_images() -> dict[str, list[str]]:
        return {"images": list(images or [])}

    return app
