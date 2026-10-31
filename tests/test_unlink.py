"""Unlinking (spec section 14): only the address itself can end its link, from the next UTC day; the address is then
free, the pair never comes back, and the subnet's service can neither make an unlink up nor take one back."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
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
        "unlinks": [],
        "next": None,
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


def test_only_a_link_a_binding_on_chain_named_can_be_unlinked_and_it_is_remembered(tmp_path: Path) -> None:
    clock, owner, stranger = Clock(), Account.create(), Account.create()
    links = {(H1, owner.address.lower())}
    reads = []

    def bound() -> set[tuple[str, str]]:
        reads.append(clock.now)
        return set(links)

    app = create_app(tmp_path / "svc", verify=lambda m, s, h: False, is_registered=lambda hk: hk == H1, clock=clock,
                     netuid=NETUID, bound_pairs=bound)  # fmt: skip
    c = TestClient(app)
    t = int(clock.now * 1000)

    def unlink(key: Any) -> int:
        body = {"hotkey": H1, "address": key.address, "time_ms": t,
                "signature": sign_unlink(key.key, NETUID, H1, key.address, t)}  # fmt: skip
        return int(c.post("/unlinks", json=body).status_code)

    assert unlink(stranger) == 403  # a fresh address never linked to the hotkey: nothing to unlink
    assert unlink(stranger) == 403 and len(reads) == 1  # the chain is read again only after a while
    links.clear()  # the hotkey commits something else: the link it named before is remembered
    clock.now += 60
    assert unlink(owner) == 200 and c.get("/unlinks").json()["unlinks"][0]["address"] == owner.address.lower()


def test_the_unlink_list_comes_in_pages_oldest_first_and_misses_none(tmp_path: Path) -> None:
    clock = Clock()
    keys = [Account.create() for _ in range(5)]
    app = create_app(tmp_path / "svc", verify=lambda m, s, h: False, is_registered=lambda hk: True, clock=clock,
                     netuid=NETUID)  # fmt: skip
    c = TestClient(app)
    for i, key in enumerate(keys):
        t = int(clock.now * 1000)
        body = {"hotkey": H1, "address": key.address, "time_ms": t,
                "signature": sign_unlink(key.key, NETUID, H1, key.address, t)}  # fmt: skip
        assert c.post("/unlinks", json=body).status_code == 200
        clock.now += 1 if i == 0 else 0  # four of them received at the very same time
    seen, after, pages = [], "", 0
    while True:
        page = c.get("/unlinks", params={"after": after, "limit": 2}).json()
        seen += [u["address"] for u in page["unlinks"]]
        pages += 1
        if page["next"] is None:
            break
        after = page["next"]
    assert sorted(seen) == sorted(k.address.lower() for k in keys) and len(seen) == 5 and pages == 3
    assert c.get("/unlinks", params={"after": "nonsense"}).status_code == 400


async def test_a_validator_reads_every_page_and_checks_each_signature_once(monkeypatch: Any) -> None:
    owners = [Account.create() for _ in range(3)]
    rows = [signed(o, H1, ms("2026-10-24") + i) for i, o in enumerate(owners)]
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after", "")
        asked.append(after)
        start = int(after) + 1 if after else 0  # the service's cursor names the last row of the page before
        page = rows[start : start + 2]
        listed = [{"hotkey": u.hotkey, "address": u.address, "time_ms": u.time_ms, "signature": u.signature}
                  for u in page]  # fmt: skip
        return httpx.Response(200, json={"unlinks": listed, "next": str(start + 1) if len(page) == 2 else None})

    checked: list[str] = []
    real = Unlink.signed_by_address

    def counted(self: Unlink, netuid: int) -> bool:
        checked.append(self.address)
        return real(self, netuid)

    monkeypatch.setattr(Unlink, "signed_by_address", counted)
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        found = await fetch_unlinks(http, "http://svc", NETUID, known={(H1, rows[0].address)})
    assert found == rows[1:] and asked == ["", "1"]
    assert sorted(checked) == sorted(u.address for u in rows[1:])  # the kept one is never checked again


def test_a_chain_that_cannot_be_read_is_not_asked_again_on_every_request(tmp_path: Path) -> None:
    clock, owner = Clock(), Account.create()
    reads = []

    def down() -> set[tuple[str, str]]:
        reads.append(clock.now)
        raise RuntimeError("chain down")

    app = create_app(tmp_path / "svc", verify=lambda m, s, h: False, is_registered=lambda hk: hk == H1, clock=clock,
                     netuid=NETUID, bound_pairs=down)  # fmt: skip
    c = TestClient(app)
    ask = {"hotkey": H1, "address": owner.address}
    assert c.get("/unlinks/message", params=ask).status_code == 503
    assert [c.get("/unlinks/message", params=ask).status_code for _ in range(5)] == [503] * 5  # not read again yet
    assert len(reads) == 1
    clock.now += 31
    assert c.get("/unlinks/message", params=ask).status_code == 503 and len(reads) == 2


def test_registrations_that_cannot_be_read_are_retried_after_a_pause_and_never_called_unknown() -> None:
    from kokoah_subnet.chain import RegisteredHotkeys

    now, reads = [0.0], []
    up = [False]

    async def fetch() -> set[str]:
        reads.append(now[0])
        if not up[0]:
            raise RuntimeError("chain down")
        return {H1}

    hk = RegisteredHotkeys(fetch, max_age_s=600, refresh_s=30, clock=lambda: now[0])
    for _ in range(5):
        with pytest.raises(RuntimeError, match="cannot be read"):
            hk.check(H1)
    assert len(reads) == 1  # one try, then a pause
    up[0], now[0] = True, 31.0
    assert hk.check(H1) and not hk.check(H2) and len(reads) == 2
    up[0], now[0] = False, 700.0  # the list is old and the chain is down: the old list still answers for a while
    assert hk.check(H1) and len(reads) == 3
    assert hk.check(H1) and len(reads) == 3
