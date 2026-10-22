"""Checking agent accounts (specification v6, section 16): is this bound account really a sealed-machine agent?

For every bound hotkey whose commitment carries an agent field, the validator:
1. challenges the machine with a fresh random value (`GET <endpoint>/attestation?challenge=...`);
2. checks that the answer names this subnet, hotkey, account and the committed code fingerprint;
3. verifies the attestation token itself, against Google's published keys: production Confidential Space machine,
   confidential hardware, an approved harness image, and both nonces (its own challenge and the binding digest);
4. checks on the exchange that the account has no other trading key approved (`extraAgents` empty), so nothing
   but the machine can sign for it;
5. checks that the account's key was never released to the miner (the owner's public released list): a released
   account is traded by a person from then on and never counts as an agent again, whatever its machine says.
Only an account that passes all four that day gets the agent bonus. Nothing here trusts the operator or the miner.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from kokoah_subnet.agent.attest import AttestationError, KeySource, binding_digest, verify_token
from kokoah_subnet.binding import Binding

log = logging.getLogger("kokoah_subnet.validator.agents")


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
    agent = binding.agent
    if agent is None:
        raise ValueError("not an agent binding")

    def fail(reason: str) -> AgentCheck:
        return AgentCheck(hotkey, binding.address, agent.code_sha256, agent.url, False, reason)

    challenge = secrets.token_hex(32)
    try:
        r = await http.get(f"{agent.url}/attestation", params={"challenge": challenge}, timeout=20)
        r.raise_for_status()
        answer = r.json()
    except (httpx.HTTPError, ValueError) as exc:
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


async def released_accounts(http: httpx.AsyncClient, service: str) -> set[str]:
    """The accounts whose key the owner's service released to their miner (`GET /v1/released`)."""
    r = await http.get(f"{service.rstrip('/')}/v1/released", timeout=20)
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
    if keys is None:
        from kokoah_subnet.agent.attest import google_keys

        keys = await asyncio.to_thread(google_keys)
    approved = list(images)
    checked = await asyncio.gather(
        *(check_agent(hk, b, netuid, keys, approved, http, extra_agents) for hk, b in sorted(agents.items()))
    )
    return sorted([*out, *checked], key=lambda c: c.hotkey)
