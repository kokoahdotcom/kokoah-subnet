"""Uploads to the agent service: everything about the sender is checked before the body is read, the body must have
the signed hash and fit the cap, one hotkey cannot slip two uploads past the 12-hour rule at once, and old bundles
are cleaned up unless an account's key copy names them."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import insert

from kokoah_subnet.agent import service as service_module
from kokoah_subnet.agent.bundle import MAX_BUNDLE_BYTES, package, sha256_hex
from kokoah_subnet.agent.service import create_app


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def agent(tmp_path: Path, name: str) -> bytes:
    d = tmp_path / name
    d.mkdir()
    (d / "entry.py").write_text(f"print({name!r})\n")
    return package(d)


def headers(hotkey: str, blob_id: str, size: int, signature: str = "ok") -> dict[str, str]:
    return {
        "content-type": "application/octet-stream",
        "content-length": str(size),
        "x-kokoah-hotkey": hotkey,
        "x-kokoah-bundle-sha256": blob_id,
        "x-kokoah-signature": signature,
    }


def app(tmp_path: Path, clock: Clock | None = None) -> Any:
    return create_app(tmp_path / "svc", verify=lambda m, s, h: s == "ok", clock=clock or Clock())


async def test_nothing_of_the_body_is_read_before_the_sender_is_checked(tmp_path: Path) -> None:
    read = []

    async def body() -> AsyncIterator[bytes]:
        read.append(True)
        yield b"x" * 1024

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app(tmp_path)), base_url="http://svc") as c:
        forged = await c.post("/agents", content=body(), headers=headers("h1", "ab" * 32, 1024, signature="forged"))
        too_big = await c.post("/agents", content=body(), headers=headers("h1", "ab" * 32, MAX_BUNDLE_BYTES + 65))
        missing = await c.post("/agents", content=b"x", headers={"x-kokoah-hotkey": "h1"})
    assert forged.status_code == 403 and too_big.status_code == 413 and missing.status_code == 400
    assert read == []


async def test_the_body_must_have_the_signed_hash(tmp_path: Path) -> None:
    blob = agent(tmp_path, "a")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app(tmp_path)), base_url="http://svc") as c:
        r = await c.post("/agents", content=blob, headers=headers("h1", "ab" * 32, len(blob)))
    assert r.status_code == 400 and "sha256" in r.json()["detail"]
    assert [p.name for p in (tmp_path / "svc" / "bundles").iterdir()] == []  # nothing left behind


async def test_one_hotkey_cannot_slip_two_uploads_past_the_cooldown(tmp_path: Path) -> None:
    first, second = agent(tmp_path, "a"), agent(tmp_path, "b")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app(tmp_path)), base_url="http://svc") as c:
        results = await asyncio.gather(
            *(c.post("/agents", content=b, headers=headers("h1", sha256_hex(b), len(b))) for b in (first, second))
        )
    assert sorted(r.status_code for r in results) == [200, 429]


async def test_old_bundles_are_cleaned_up_unless_a_key_copy_names_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "KEEP_BUNDLES_PER_HOTKEY", 2)
    clock = Clock()
    application = app(tmp_path, clock)
    ids = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://svc") as c:
        for i in range(4):
            blob = agent(tmp_path, f"v{i}")
            r = await c.post("/agents", content=blob, headers=headers("h1", sha256_hex(blob), len(blob)))
            assert r.status_code == 200
            ids.append(sha256_hex(blob))
            if i == 0:  # an account's key copy was filed with the first bundle
                from sqlalchemy import create_engine

                engine = create_engine(f"sqlite:///{tmp_path / 'svc' / 'agents.sqlite'}")
                with engine.begin() as conn:
                    conn.execute(
                        insert(service_module.key_events).values(
                            address="0x" + "11" * 20,
                            event="store",
                            at=clock.now,
                            detail=f"hotkey h1 submission {ids[0]}",
                        )
                    )
            clock.now += 13 * 3600
    kept = {p.name for p in (tmp_path / "svc" / "bundles").iterdir()}
    assert kept == {ids[0], ids[2], ids[3]}  # the newest two, and the one in use
