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


def headers(hotkey: str, blob_id: str, size: int, signature: str = "ok", clock: Clock | None = None) -> dict[str, str]:
    return {
        "content-type": "application/octet-stream",
        "content-length": str(size),
        "x-kokoah-hotkey": hotkey,
        "x-kokoah-bundle-sha256": blob_id,
        "x-kokoah-signature": signature,
        "x-kokoah-time": str(int((clock or Clock()).now * 1000)),
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
    codes = sorted(r.status_code for r in results)
    assert codes[0] == 200 and codes[1] in (409, 429)  # the other one waits its turn or meets the cooldown


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
            r = await c.post("/agents", content=blob, headers=headers("h1", sha256_hex(blob), len(blob), clock=clock))
            assert r.status_code == 200
            ids.append(sha256_hex(blob))
            if i < 2:  # an account's key copy was filed with each of the first two bundles; the second was released
                file_key_copy(tmp_path, f"0x{i + 1:040x}", ids[i], clock.now, released=i == 1)
            clock.now += 13 * 3600
    kept = {p.name for p in (tmp_path / "svc" / "bundles").iterdir()}
    assert kept == {ids[0], ids[2], ids[3]}  # the newest two, and the one an unreleased copy names


def file_key_copy(tmp_path: Path, address: str, submission: str, at: float, released: bool = False) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'svc' / 'agents.sqlite'}")
    with engine.begin() as conn:
        conn.execute(
            insert(service_module.keys).values(
                address=address, hotkey="h1", master="0x" + "22" * 20, blob="x", stored_at=at,
                released_at=at if released else None,
            )
        )  # fmt: skip
        conn.execute(
            insert(service_module.key_events).values(
                address=address, event="store", at=at, detail=f"hotkey h1 submission {submission}"
            )
        )


async def test_every_upload_counts_refused_ones_too_and_bundles_are_checked_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    checked_on: list[str] = []
    from kokoah_subnet.agent.bundle import check_zip as real

    def check(plain: bytes) -> Any:
        checked_on.append(threading.current_thread().name)
        return real(plain)

    monkeypatch.setattr("kokoah_subnet.agent.service.check_zip", check)
    bad = b"PK not a zip"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app(tmp_path)), base_url="http://svc") as c:
        codes = [
            (await c.post("/agents", content=bad + bytes([i]), headers=headers("h1", sha256_hex(bad + bytes([i])),
                                                                               len(bad) + 1))).status_code
            for i in range(service_module.SUBMIT_ATTEMPTS_PER_HOUR + 1)
        ]  # fmt: skip
        other = agent(tmp_path, "a")
        assert (
            await c.post("/agents", content=other, headers=headers("h2", sha256_hex(other), len(other)))
        ).status_code == 200
    assert codes == [400] * service_module.SUBMIT_ATTEMPTS_PER_HOUR + [429]  # one hotkey's tries; others unaffected
    assert checked_on and threading.main_thread().name not in checked_on


async def test_bundles_of_a_hotkey_that_left_are_deleted_after_a_month_and_can_be_sent_again(tmp_path: Path) -> None:
    clock = Clock()
    registered = {"h1", "h2"}
    application = create_app(tmp_path / "svc", verify=lambda m, s, h: s == "ok", clock=clock,
                             is_registered=lambda hk: hk in registered)  # fmt: skip
    blobs = {hk: agent(tmp_path, f"code-{hk}") for hk in ("h1", "h2", "h3")}
    registered.add("h3")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://svc") as c:
        for hk, blob in blobs.items():
            r = await c.post("/agents", content=blob, headers=headers(hk, sha256_hex(blob), len(blob), clock=clock))
            assert r.status_code == 200
        file_key_copy(tmp_path, "0x" + "33" * 20, sha256_hex(blobs["h3"]), clock.now)  # h3's account is kept
        clock.now += 60 * 86_400  # the bundles are old, but their hotkeys are still here
        assert application.state.prune_bundles() == 0
        registered -= {"h2", "h3"}  # h2 and h3 leave the subnet
        assert application.state.prune_bundles() == 0  # just left: the month starts now
        clock.now += 29 * 86_400
        restarted = create_app(tmp_path / "svc", verify=lambda m, s, h: s == "ok", clock=clock,
                               is_registered=lambda hk: hk in registered)  # fmt: skip
        assert restarted.state.prune_bundles() == 0  # not a month yet, though the service restarted meanwhile
        clock.now += 2 * 86_400
        assert application.state.prune_bundles() == 1
        kept = {p.name for p in (tmp_path / "svc" / "bundles").iterdir()}
        assert kept == {sha256_hex(blobs["h1"]), sha256_hex(blobs["h3"])}  # registered, or named by a kept copy
        gone = sha256_hex(blobs["h2"])
        assert (await c.get(f"/agents/{gone}/bundle")).status_code == 404
        registered.add("h2")  # h2 comes back and sends the same code again
        again = await c.post("/agents", content=blobs["h2"], headers=headers("h2", gone, len(blobs["h2"]), clock=clock))
        assert again.status_code == 200 and again.json()["existing"]
        assert (await c.get(f"/agents/{gone}/bundle")).content == blobs["h2"]  # kept again


async def test_another_hotkey_cannot_bring_back_a_deleted_bundle_or_hold_the_upload_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    registered = {"h1", "h2", "h3"}
    application = create_app(tmp_path / "svc", verify=lambda m, s, h: s == "ok", clock=clock,
                             is_registered=lambda hk: hk in registered)  # fmt: skip
    blob = agent(tmp_path, "mine")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://svc") as c:
        r = await c.post("/agents", content=blob, headers=headers("h1", sha256_hex(blob), len(blob), clock=clock))
        assert r.status_code == 200
        registered.discard("h1")
        application.state.prune_bundles()
        clock.now += 40 * 86_400
        assert application.state.prune_bundles() == 1
        registered.add("h1")  # h1 is back: its cooldown must be its own business
        clock.now += 13 * 3600
        r = await c.post("/agents", content=blob, headers=headers("h2", sha256_hex(blob), len(blob), clock=clock))
        assert r.status_code == 409  # h2 replays h1's old bundle: refused, nothing changes for h1
        mine = agent(tmp_path, "new")
        r = await c.post("/agents", content=mine, headers=headers("h1", sha256_hex(mine), len(mine), clock=clock))
        assert r.status_code == 200

        # a slow body is cut off at the deadline, and a hotkey holds at most one slot at a time
        monkeypatch.setattr(service_module, "UPLOAD_DEADLINE_S", 0.3)
        clock.now += 13 * 3600

        async def slow() -> AsyncIterator[bytes]:
            yield b"PK"
            await asyncio.sleep(5)
            yield b"x"

        other = agent(tmp_path, "third")
        first = asyncio.create_task(c.post("/agents", content=slow(), headers=headers("h3", "ab" * 32, 3, clock=clock)))
        await asyncio.sleep(0.05)
        second = await c.post(
            "/agents", content=other, headers=headers("h3", sha256_hex(other), len(other), clock=clock)
        )
        assert second.status_code == 409  # one upload at a time per hotkey
        assert (await first).status_code == 408
