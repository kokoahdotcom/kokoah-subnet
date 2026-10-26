"""Unlinking (spec section 14): only the address itself can end its link, from the next UTC day; the address is then
free, the pair never comes back, and the subnet's service can neither make an unlink up nor take one back."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from fastapi.testclient import TestClient

from kokoah_subnet.agent.service import create_app
from kokoah_subnet.binding import AcceptedBinding, Binding, PublishedBinding, resolve_bindings
from kokoah_subnet.binding.unlink import Unlink, effective_day, sign_unlink, unlink_message
from kokoah_subnet.validator.agents import fetch_unlinks
from kokoah_subnet.validator.store import Store

NETUID = 385
H1 = "5FAATKVCG4wwonNBm4Z5Hf132AyWY5JFEyt9Q7on37La7adM"
H2 = "5GgJC4ZbcGvDGQYWggR5LLqjrHrE1jJm5qR2aYVTg8tLCfa6"


def ms(day: str, hh: int = 12) -> int:
    return int(datetime.fromisoformat(f"{day}T{hh:02d}:00:00+00:00").timestamp() * 1000)


def signed(key: Any, hotkey: str, time_ms: int, netuid: int = NETUID) -> Unlink:
    return Unlink(hotkey, key.address, time_ms, sign_unlink(key.key, netuid, hotkey, key.address, time_ms))


def test_the_message_and_the_day_it_counts_from() -> None:
    assert unlink_message(NETUID, H1, "0xABC", 5) == f"kokoah-unlink-1 netuid=385 hotkey={H1} address=0xabc time=5"
    assert effective_day(ms("2026-10-24", 0)) == "2026-10-25"
    assert effective_day(int(datetime(2026, 10, 24, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)) == "2026-10-25"


def test_only_the_address_itself_can_sign_an_unlink() -> None:
    owner, other = Account.create(), Account.create()
    t = ms("2026-10-24")
    u = signed(owner, H1, t)
    assert u.signed_by_address(NETUID)
    assert not u.signed_by_address(NETUID + 1)  # another subnet
    assert not Unlink(H2, owner.address, t, u.signature).signed_by_address(NETUID)  # another hotkey
    forged = Unlink(H1, owner.address, t, sign_unlink(other.key, NETUID, H1, owner.address, t))
    assert not forged.signed_by_address(NETUID)
    assert not Unlink(H1, owner.address, t, "0x1234").signed_by_address(NETUID)


def published(hotkey: str, address: str, block: int = 10) -> PublishedBinding:
    return PublishedBinding(hotkey, block, Binding(address, "0x" + "00" * 65), True)


def test_an_unlink_ends_the_link_from_its_day_frees_the_address_and_the_pair_never_returns() -> None:
    x, y = "0x" + "11" * 20, "0x" + "22" * 20
    before = {H1: AcceptedBinding(H1, x, 5, 5)}
    unlink = [Unlink(H1, x, ms("2026-10-24"), "0x")]
    # the day it was signed still counts; another hotkey cannot take the address yet
    same_day = resolve_bindings(before, [published(H1, x), published(H2, x)], 100, unlinks=unlink, day="2026-10-24")
    assert same_day.accepted[H1].address == x and H2 not in same_day.accepted and not same_day.unlinked
    # from the next day: the link is gone, the address is free, the old commitment is refused
    next_day = resolve_bindings(before, [published(H1, x), published(H2, x)], 200, unlinks=unlink, day="2026-10-25")
    assert H1 not in next_day.accepted and next_day.unlinked == {H1: x}
    assert next_day.accepted[H2].address == x
    assert next_day.refused[H1] == "the address unlinked itself from this hotkey"
    # the hotkey can link another address at once; never the same one again
    moved = resolve_bindings({}, [published(H1, y)], 300, unlinks=unlink, day="2026-10-26")
    assert moved.accepted[H1].address == y
    again = resolve_bindings({}, [published(H1, x)], 400, unlinks=unlink, day="2026-12-31")
    assert H1 not in again.accepted


def test_an_unlink_of_a_pair_that_is_not_linked_changes_nothing_else() -> None:
    x, z = "0x" + "11" * 20, "0x" + "33" * 20
    before = {H1: AcceptedBinding(H1, z, 5, 5)}
    r = resolve_bindings(
        before, [published(H1, z)], 100, unlinks=[Unlink(H1, x, ms("2026-10-01"), "0x")], day="2026-10-24"
    )
    assert r.accepted[H1].address == z and not r.unlinked and not r.refused


class Clock:
    def __init__(self) -> None:
        self.now = ms("2026-10-24") / 1000

    def __call__(self) -> float:
        return self.now


def service(tmp_path: Path, clock: Clock) -> TestClient:
    app = create_app(tmp_path / "svc", verify=lambda m, s, h: False, is_registered=lambda hk: hk == H1, clock=clock,
                     netuid=NETUID)  # fmt: skip
    return TestClient(app)


def test_the_service_files_an_unlink_only_when_the_address_signed_it_just_now(tmp_path: Path) -> None:
    clock, owner, other = Clock(), Account.create(), Account.create()
    c = service(tmp_path, clock)
    m = c.get("/unlinks/message", params={"hotkey": H1, "address": owner.address}).json()
    assert m["message"] == unlink_message(NETUID, H1, owner.address, m["time_ms"])
    body = {"hotkey": H1, "address": owner.address, "time_ms": m["time_ms"]}
    sig = sign_unlink(owner.key, NETUID, H1, owner.address, m["time_ms"])
    assert c.post("/unlinks", json={**body, "signature": sign_unlink(other.key, NETUID, H1, owner.address,
                                                                       m["time_ms"])}).status_code == 403  # fmt: skip
    clock.now += 11 * 60
    assert c.post("/unlinks", json={**body, "signature": sig}).status_code == 400  # signed too long ago
    clock.now -= 11 * 60
    ok = c.post("/unlinks", json={**body, "signature": sig}).json()
    assert ok["existing"] is False and ok["effective_day"] == "2026-10-25"
    again = c.post("/unlinks", json={**body, "signature": sig}).json()
    assert again["existing"] is True and again["time_ms"] == m["time_ms"]
    assert c.get("/unlinks/message", params={"hotkey": H2, "address": owner.address}).status_code == 403
    assert c.get("/unlinks/message", params={"hotkey": "nope", "address": owner.address}).status_code == 400
    listed = c.get("/unlinks", params={"address": owner.address}).json()["unlinks"]
    assert [u["hotkey"] for u in listed] == [H1] and c.get("/unlinks", params={"address": other.address}).json() == {
        "unlinks": []
    }


async def test_validators_keep_only_unlinks_the_address_itself_signed() -> None:
    owner, other = Account.create(), Account.create()
    t = ms("2026-10-24")
    good = signed(owner, H1, t)
    forged = Unlink(H1, owner.address, t, sign_unlink(other.key, NETUID, H1, owner.address, t))
    rows = [good, forged]

    def answer(request: httpx.Request) -> httpx.Response:
        listed = [
            {"hotkey": u.hotkey, "address": u.address, "time_ms": u.time_ms, "signature": u.signature} for u in rows
        ]
        return httpx.Response(200, json={"unlinks": [*listed, {"hotkey": H2}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        assert await fetch_unlinks(http, "http://svc", NETUID) == [good]


def test_a_validator_keeps_every_unlink_it_saw_and_the_first_per_pair(tmp_path: Path) -> None:
    owner = Account.create()
    store = Store(tmp_path / "v.sqlite")
    first, later = signed(owner, H1, ms("2026-10-24")), signed(owner, H1, ms("2026-10-26"))
    store.put_unlinks("2026-10-24", [first])
    store.put_unlinks("2026-10-27", [later])
    assert store.unlinks() == [first]
