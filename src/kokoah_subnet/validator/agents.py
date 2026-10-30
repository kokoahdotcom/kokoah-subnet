"""Checking agent accounts (specification section 16): is this bound account really a sealed-machine agent?

For every bound hotkey whose commitment carries an agent field, the validator:
1. challenges the machine with a fresh random value (`GET <endpoint>/attestation?challenge=...`);
2. checks that the answer names this subnet, hotkey, account and the committed code fingerprint;
3. verifies the attestation token itself, against Google's published keys: production Confidential Space machine,
   confidential hardware, an approved harness image, and both nonces (its own challenge and the binding digest);
4. checks on the exchange that the account has no other trading key approved (`extraAgents` empty), so nothing
   but the machine can sign for it;
5. checks that the account's key was never released to the miner (the owner's public released list): a released
   account is traded by a person from then on and never counts as an agent again, whatever its machine says.
Only an account that passes all five that day gets the agent bonus. Nothing here trusts the operator or the miner.

The miner chooses the endpoint, so every check is bounded: only public addresses are contacted (a host name must
resolve to public addresses only, and the connection goes to the address checked), redirects are not followed, the
answer is read up to a size cap within a deadline, every check runs in its own error boundary, and the batch has an
overall deadline. A bad machine costs only its own account's bonus.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import secrets
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from kokoah_subnet.agent.attest import AttestationError, KeySource, binding_digest, verify_token
from kokoah_subnet.binding import AgentRef, Binding, Unlink

log = logging.getLogger("kokoah_subnet.validator.agents")

ANSWER_TIMEOUT_S = 30.0  # the whole answer, connection included
MAX_ANSWER_BYTES = 64 * 1024
BATCH_TIMEOUT_S = 180.0  # every agent check of the day together
CONCURRENCY = 16
_HOST_NAME = re.compile(r"^(?=.{1,51}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$")


class EndpointRefused(Exception):
    """An agent endpoint the validator does not contact or read."""


def address_allowed(ip: str) -> bool:
    """Only public unicast addresses: never loopback, private, link-local, reserved or multicast ones."""
    address = ipaddress.ip_address(ip)
    return address.is_global and not address.is_multicast


async def resolve_host(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def public_address(host: str) -> str:
    """The address to connect to: the host itself when it is an IP address, else what it resolves to; every
    address must be public."""
    try:
        candidates = [str(ipaddress.ip_address(host))]
    except ValueError:
        if not _HOST_NAME.match(host.lower()):
            raise EndpointRefused("not an IP address or host name") from None
        candidates = await resolve_host(host)
        if not candidates:
            raise EndpointRefused("the host name does not resolve") from None
    if not all(address_allowed(c) for c in candidates):
        raise EndpointRefused("not a public address")
    return candidates[0]


async def fetch_answer(http: httpx.AsyncClient, agent: AgentRef, challenge: str) -> dict[str, Any]:
    """The machine's answer to a challenge: a JSON object of at most MAX_ANSWER_BYTES, read within ANSWER_TIMEOUT_S."""
    address = await public_address(agent.host)
    host = f"[{address}]" if ":" in address else address
    headers = {} if address == agent.host else {"Host": f"{agent.host}:{agent.port}"}

    async def read() -> bytes:
        async with http.stream(
            "GET",
            f"http://{host}:{agent.port}/attestation",
            params={"challenge": challenge},
            headers=headers,
            timeout=10.0,
            follow_redirects=False,
        ) as r:
            r.raise_for_status()
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_ANSWER_BYTES:
                    raise EndpointRefused("answer too large")
            return bytes(body)

    answer = json.loads(await asyncio.wait_for(read(), ANSWER_TIMEOUT_S))
    if not isinstance(answer, dict):
        raise ValueError("the answer is not a JSON object")
    return answer


@dataclass(frozen=True)
class AgentCheck:
    hotkey: str
    address: str
    code_sha256: str
    endpoint: str
    ok: bool
    reason: str | None
    image_digest: str | None = None
    hardware: str | None = None
    instance_id: str | None = None

    def as_row(self) -> dict[str, Any]:
        return dict(self.__dict__)


async def check_agent(
    hotkey: str,
    binding: Binding,
    netuid: int,
    keys: KeySource,
    images: Iterable[str],
    http: httpx.AsyncClient,
    extra_agents: Any,
) -> AgentCheck:
    """One account's check; whatever its machine does, it ends in a result for that account alone."""
    agent = binding.agent
    if agent is None:
        raise ValueError("not an agent binding")
    try:
        return await _check_agent(hotkey, binding, netuid, keys, images, http, extra_agents)
    except Exception as exc:  # noqa: BLE001 - a bad answer costs this account's bonus, nothing else
        return AgentCheck(hotkey, binding.address, agent.code_sha256, agent.url, False, f"bad answer: {exc!r}"[:160])


async def _check_agent(
    hotkey: str,
    binding: Binding,
    netuid: int,
    keys: KeySource,
    images: Iterable[str],
    http: httpx.AsyncClient,
    extra_agents: Any,
) -> AgentCheck:
    agent = binding.agent
    if agent is None:
        raise ValueError("not an agent binding")

    def fail(reason: str) -> AgentCheck:
        return AgentCheck(hotkey, binding.address, agent.code_sha256, agent.url, False, reason)

    challenge = secrets.token_hex(32)
    try:
        answer = await fetch_answer(http, agent, challenge)
    except TimeoutError:
        return fail("machine too slow to answer")
    except EndpointRefused as exc:
        return fail(f"endpoint refused: {exc}")
    except (httpx.HTTPError, ValueError, OSError) as exc:
        return fail(f"machine unreachable: {str(exc)[:120]}")
    if str(answer.get("address", "")).lower() != binding.address:
        return fail("the machine speaks for another account")
    if str(answer.get("code_sha256", "")).lower() != agent.code_sha256:
        return fail("the machine runs other code than the committed fingerprint")
    if answer.get("hotkey") != hotkey or int(answer.get("netuid", -1)) != netuid:
        return fail("the machine is bound to another hotkey or subnet")
    expected = [challenge, binding_digest(netuid, hotkey, binding.address, agent.code_sha256)]
    try:
        verified = await asyncio.to_thread(verify_token, str(answer.get("token", "")), keys, images, expected)
    except AttestationError as exc:
        return fail(f"attestation: {exc}")
    try:
        others = await extra_agents(binding.address)
    except Exception as exc:  # noqa: BLE001
        return fail(f"exchange: {str(exc)[:120]}")
    if others:
        return fail("the account has other trading keys approved")
    return AgentCheck(
        hotkey,
        binding.address,
        agent.code_sha256,
        agent.url,
        True,
        None,
        verified.image_digest,
        verified.hardware,
        verified.instance,
    )


UNLINK_PAGES = 1000  # the most pages read in one go


async def fetch_unlinks(
    http: httpx.AsyncClient, service: str, netuid: int, known: Iterable[tuple[str, str]] = ()
) -> list[Unlink]:
    """The unlinks the subnet's service published (`GET /unlinks`, page by page), keeping only those the address
    itself signed for this subnet: the service cannot make one up. Pairs in `known` (hotkey, address: unlinks already
    kept, of which only the first per pair counts) are skipped unchecked, so each signature is checked once."""
    out: list[Unlink] = []
    seen = set(known)
    since = 0.0
    for _ in range(UNLINK_PAGES):
        r = await http.get(f"{service.rstrip('/')}/unlinks", params={"since": since}, timeout=20)
        r.raise_for_status()
        page = r.json()
        for x in page["unlinks"]:
            try:
                u = Unlink(str(x["hotkey"]), str(x["address"]), int(x["time_ms"]), str(x["signature"]))
            except (KeyError, TypeError, ValueError):
                continue
            if (u.hotkey, u.address) not in seen and u.signed_by_address(netuid):
                seen.add((u.hotkey, u.address))
                out.append(u)
        following = page.get("next")
        if following is None or float(following) <= since:
            break
        since = float(following)
    return out


async def released_accounts(http: httpx.AsyncClient, service: str) -> set[str]:
    """The accounts whose key the owner's service released to their miner (`GET /key-store/released`)."""
    r = await http.get(f"{service.rstrip('/')}/key-store/released", timeout=20)
    r.raise_for_status()
    return {str(x["address"]).lower() for x in r.json()["released"]}


async def check_agents(
    bindings: dict[str, Binding],
    netuid: int,
    keys: KeySource | None,
    images: Iterable[str],
    http: httpx.AsyncClient,
    extra_agents: Any,
    service: str = "",
    released: set[str] | None = None,
) -> list[AgentCheck]:
    """Every agent binding among `bindings` (hotkey -> binding), checked concurrently. `released` (or the
    owner's `service` to read it from) is required: without the released list no account counts as an agent."""
    agents = {hk: b for hk, b in bindings.items() if b.agent is not None}
    if not agents:
        return []
    reason: str | None = None
    if released is None:
        if not service:
            reason = "no agent service configured: the released list cannot be read"
        else:
            try:
                released = await released_accounts(http, service)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                reason = f"released list unavailable: {str(exc)[:120]}"
    if reason is not None or released is None:
        why = reason or "released list unavailable"
        return [
            AgentCheck(hk, b.address, b.agent.code_sha256, b.agent.url, False, why)
            for hk, b in sorted(agents.items())
            if b.agent is not None
        ]
    out = [
        AgentCheck(hk, b.address, b.agent.code_sha256, b.agent.url, False, "the key was released to the miner")
        for hk, b in sorted(agents.items())
        if b.agent is not None and b.address in released
    ]
    agents = {hk: b for hk, b in agents.items() if b.address not in released}
    if not agents:
        return out
    approved = list(images)
    if not approved:  # no machine can pass: nothing to ask
        refused = [
            AgentCheck(hk, b.address, b.agent.code_sha256, b.agent.url, False, "no harness image is approved")
            for hk, b in agents.items()
            if b.agent is not None
        ]
        return sorted([*out, *refused], key=lambda c: c.hotkey)
    if keys is None:
        from kokoah_subnet.agent.attest import google_keys

        keys = await asyncio.to_thread(google_keys)
    gate = asyncio.Semaphore(CONCURRENCY)

    async def bounded(hk: str, b: Binding) -> AgentCheck:
        async with gate:
            return await check_agent(hk, b, netuid, keys, approved, http, extra_agents)

    tasks = {hk: asyncio.create_task(bounded(hk, b)) for hk, b in sorted(agents.items())}
    done, pending = await asyncio.wait(tasks.values(), timeout=BATCH_TIMEOUT_S)
    for task in pending:
        task.cancel()
    checked = []
    for hk, task in tasks.items():
        b = agents[hk]
        if task in done and not task.cancelled() and task.exception() is None:
            checked.append(task.result())
        elif b.agent is not None:
            checked.append(AgentCheck(hk, b.address, b.agent.code_sha256, b.agent.url, False, "check timed out"))
    return sorted([*out, *checked], key=lambda c: c.hotkey)
