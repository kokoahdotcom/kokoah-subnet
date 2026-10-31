"""The validator's agent check is bounded: a slow or endless answer, a malformed answer, or an endpoint on a private
network costs only that one account's agent bonus, never the day's scoring or another account's bonus."""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from eth_account import Account

from kokoah_subnet.agent.attest import DevelopmentAttestor, StaticKeys
from kokoah_subnet.agent.harness import Harness, HarnessConfig, public_app
from kokoah_subnet.binding import AgentRef, Binding
from kokoah_subnet.validator import agents
from kokoah_subnet.validator.agents import check_agent, check_agents

IMAGE = "sha256:" + "ab" * 32
CODE = "cd" * 32


class Endless(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes, pause: float) -> None:
        self.chunk, self.pause = chunk, pause

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            yield self.chunk
            await asyncio.sleep(self.pause)


class Machine(httpx.AsyncBaseTransport):
    """Answers for each host: an ASGI app, a fixed body, or an endless stream; records every host it was asked."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.asked: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request.url.host)
        route = self.routes[request.url.host]
        if isinstance(route, httpx.AsyncByteStream):
            return httpx.Response(200, stream=route)
        if isinstance(route, bytes):
            return httpx.Response(200, content=route)
        response: httpx.Response = await httpx.ASGITransport(app=route).handle_async_request(request)
        return response


def good_machine(hotkey: str) -> tuple[Binding, Any, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    acct = Account.create()
    h = Harness(
        HarnessConfig(netuid=2, hotkey=hotkey, master=Account.create().address, code_sha256=CODE, builder="0xb"),
        acct.key,
        DevelopmentAttestor(key, IMAGE),
    )
    line = h.binding()
    return Binding(line["address"], line["signature"], agent=AgentRef(CODE, "203.0.113.20", 8080)), public_app(h), key


def binding_at(host: str, port: int = 8080) -> Binding:
    return Binding(Account.create().address, "0x" + "00" * 65, agent=AgentRef(CODE, host, port))


async def no_extra(_: str) -> list[Any]:
    return []


async def one(binding: Binding, transport: Machine, keys: Any = None) -> Any:
    async with httpx.AsyncClient(transport=transport) as http:
        return await check_agent("hk", binding, 2, keys or StaticKeys(None), [IMAGE], http, no_extra)


async def test_a_machine_that_answers_one_byte_at_a_time_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "ANSWER_TIMEOUT_S", 0.5)
    started = time.monotonic()
    result = await one(binding_at("203.0.113.30"), Machine({"203.0.113.30": Endless(b" ", 0.2)}))
    assert not result.ok and "slow" in str(result.reason) and time.monotonic() - started < 3


async def test_an_endless_answer_is_cut_off_at_the_size_cap() -> None:
    result = await one(binding_at("203.0.113.31"), Machine({"203.0.113.31": Endless(b"x" * 65536, 0.0)}))
    assert not result.ok and "too large" in str(result.reason)


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "localhost"])
async def test_a_private_endpoint_is_never_contacted(host: str) -> None:
    transport = Machine({})
    result = await one(binding_at(host, 48130), transport)
    assert not result.ok and "endpoint refused" in str(result.reason) and transport.asked == []


async def test_a_host_name_that_resolves_to_a_private_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(_: str) -> list[str]:
        return ["10.1.2.3"]

    monkeypatch.setattr(agents, "resolve_host", resolve)
    transport = Machine({})
    result = await one(binding_at("agent.example.com"), transport)
    assert not result.ok and "endpoint refused" in str(result.reason) and transport.asked == []


async def test_one_malformed_answer_costs_only_its_own_bonus() -> None:
    good, app, key = good_machine("hkGood")
    bad_list = binding_at("203.0.113.40")
    bad_netuid = binding_at("203.0.113.41")
    transport = Machine(
        {"203.0.113.20": app, "203.0.113.40": b"[]", "203.0.113.41": b'{"address": "x", "netuid": "x"}'}
    )
    async with httpx.AsyncClient(transport=transport) as http:
        results = await check_agents(
            {"hkGood": good, "hkList": bad_list, "hkNetuid": bad_netuid},
            2,
            StaticKeys(key.public_key()),
            [IMAGE],
            http,
            no_extra,
            released=set(),
        )
    by_hotkey = {r.hotkey: r for r in results}
    assert by_hotkey["hkGood"].ok
    assert not by_hotkey["hkList"].ok and not by_hotkey["hkNetuid"].ok


async def test_the_batch_has_an_overall_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "BATCH_TIMEOUT_S", 0.5)
    monkeypatch.setattr(agents, "ANSWER_TIMEOUT_S", 30.0)
    slow = {f"hk{i}": binding_at(f"203.0.113.{50 + i}") for i in range(3)}
    transport = Machine({f"203.0.113.{50 + i}": Endless(b" ", 0.1) for i in range(3)})
    started = time.monotonic()
    async with httpx.AsyncClient(transport=transport) as http:
        results = await check_agents(slow, 2, StaticKeys(None), [IMAGE], http, no_extra, released=set())
    assert time.monotonic() - started < 3
    assert len(results) == 3 and all(not r.ok and "timed out" in str(r.reason) for r in results)


async def test_with_no_approved_image_no_account_is_an_agent_and_no_machine_is_asked() -> None:
    good, app, key = good_machine("hkGood")
    transport = Machine({"203.0.113.20": app})
    async with httpx.AsyncClient(transport=transport) as http:
        results = await check_agents(
            {"hkGood": good}, 2, StaticKeys(key.public_key()), [], http, no_extra, released=set()
        )
    assert [(r.ok, r.reason) for r in results] == [(False, "no harness image is approved")]
    assert transport.asked == []


def test_withdrawn_harness_images_are_never_approved() -> None:
    from kokoah_subnet.agent.images import APPROVED_HARNESS_IMAGES

    withdrawn = {
        "sha256:4c4576cf7f406661342d74f98feb7ce80b3c7f53045b6816c5bbbe143aada527",  # 1.2.0: bundle code ran as root
    }
    assert not withdrawn & set(APPROVED_HARNESS_IMAGES)


async def test_a_slow_name_lookup_counts_against_the_machine_s_own_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(host: str) -> list[str]:
        await asyncio.sleep(30)
        return ["203.0.113.9"]

    monkeypatch.setattr(agents, "resolve_host", slow)
    monkeypatch.setattr(agents, "ANSWER_TIMEOUT_S", 0.3)
    started = time.monotonic()
    result = await one(binding_at("agent.example.com"), Machine({}))
    assert not result.ok and result.reason == "machine too slow to answer" and time.monotonic() - started < 2
