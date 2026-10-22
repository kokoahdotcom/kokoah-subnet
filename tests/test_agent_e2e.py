"""The whole agent path, end to end on one machine (development attestor and sealer in place of the cloud's)."""

import asyncio
import base64
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from bittensor.keyfiles import Keypair
from cryptography.hazmat.primitives.asymmetric import rsa
from eth_account import Account
from fastapi.testclient import TestClient

from kokoah_subnet.agent.attest import DevelopmentAttestor, StaticKeys
from kokoah_subnet.agent.bundle import encrypt, new_key, package, sha256_hex
from kokoah_subnet.agent.harness import Harness, HarnessConfig, agent_app, public_app
from kokoah_subnet.agent.service import create_app, submit_message
from kokoah_subnet.agent.supervisor import fetch_code
from kokoah_subnet.binding import AgentRef, Binding
from kokoah_subnet.validator.agents import check_agent

BUILDER = "0x43ccc0618e4358494f87a4b8699230203dc4ab67"
IMAGE = "sha256:" + "ee" * 32


class Exchange:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/info":
            if body["type"] == "meta":
                return httpx.Response(200, json={"universe": [{"name": "BTC"}]})
            if body["type"] == "allMids":
                return httpx.Response(200, json={"BTC": "100000"})
            return httpx.Response(200, json=[])
        self.posted.append(body)
        return httpx.Response(200, json={"status": "ok"})


def make_agent(tmp_path: Path) -> Path:
    d = tmp_path / "agent"
    d.mkdir(parents=True)
    (d / "entry.py").write_text("print('agent')\n")
    return d


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    hotkey = Keypair.create_from_uri("//Alice")
    service = TestClient(create_app(tmp_path / "svc", accept_unverified_machines=True))
    plain = package(make_agent(tmp_path))
    bundle_key = new_key()
    blob = encrypt(plain, bundle_key)  # private code: the service stores only ciphertext
    blob_id = sha256_hex(blob)
    sig = bytes(hotkey.sign(submit_message(blob_id, hotkey.ss58_address).encode())).hex()
    r = service.post(
        "/v1/agents", json={"hotkey": hotkey.ss58_address, "bundle": base64.b64encode(blob).decode(), "signature": sig}
    )
    assert r.status_code == 200 and r.json()["encrypted"] and r.json()["code_sha256"] is None
    # the sealed machine: fetch, check, decrypt, fingerprint
    got, code = fetch_code("http://svc", blob_id, bundle_key.hex(), service)
    assert got == plain and code == sha256_hex(plain)
    key = bytes(Account.create().key)  # created in the machine's memory, never stored
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    exchange = Exchange()
    master = Account.create()
    h = Harness(
        HarnessConfig(2, hotkey.ss58_address, master.address, code, BUILDER),
        key,
        DevelopmentAttestor(rsa_key, IMAGE),
        http=httpx.Client(transport=httpx.MockTransport(exchange), base_url="https://x"),
    )
    h.builder_approved = True
    return {"h": h, "code": code, "rsa": rsa_key, "exchange": exchange, "hotkey": hotkey.ss58_address, "svc": service}


def test_submission_rules(world: dict[str, Any], tmp_path: Path) -> None:
    svc: TestClient = world["svc"]
    hotkey = Keypair.create_from_uri("//Alice")
    blob = package(make_agent(tmp_path / "second"))
    blob_id = sha256_hex(blob)
    good = bytes(hotkey.sign(submit_message(blob_id, hotkey.ss58_address).encode())).hex()
    body = {"hotkey": hotkey.ss58_address, "bundle": base64.b64encode(blob).decode(), "signature": good}
    assert svc.post("/v1/agents", json=body).status_code == 429  # one per 12 hours
    bob = Keypair.create_from_uri("//Bob")
    forged = {**body, "hotkey": bob.ss58_address}
    assert svc.post("/v1/agents", json=forged).status_code == 403


async def _check(world: dict[str, Any], binding: Binding, images: list[str], others: list[Any]) -> Any:
    transport = httpx.ASGITransport(app=public_app(world["h"]))
    async with httpx.AsyncClient(transport=transport) as http:

        async def extra(_: str) -> Any:
            return others

        return await check_agent(
            world["hotkey"], binding, 2, StaticKeys(world["rsa"].public_key()), images, http, extra
        )


def binding_for(world: dict[str, Any], code: str) -> Binding:
    b = world["h"].binding()
    return Binding(b["address"], b["signature"], agent=AgentRef(code, "203.0.113.9", 8080))


def test_a_validator_verifies_the_machine(world: dict[str, Any]) -> None:
    ok = asyncio.run(_check(world, binding_for(world, world["code"]), [IMAGE], []))
    assert ok.ok and ok.image_digest == IMAGE and ok.hardware == "GCP_INTEL_TDX"
    wrong_code = asyncio.run(_check(world, binding_for(world, "00" * 32), [IMAGE], []))
    assert not wrong_code.ok and "other code" in str(wrong_code.reason)
    unapproved = asyncio.run(_check(world, binding_for(world, world["code"]), ["sha256:" + "11" * 32], []))
    assert not unapproved.ok and "approved harness" in str(unapproved.reason)
    extra_key = asyncio.run(_check(world, binding_for(world, world["code"]), [IMAGE], [{"address": "0x1"}]))
    assert not extra_key.ok and "other trading keys" in str(extra_key.reason)


def test_a_sandboxed_agent_trades_only_through_the_harness(world: dict[str, Any], tmp_path: Path) -> None:
    sock = tmp_path / "agent.sock"
    server = uvicorn.Server(uvicorn.Config(agent_app(world["h"]), uds=str(sock), log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.05)
    script = tmp_path / "entry.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import http.client, json, socket
            class UnixConn(http.client.HTTPConnection):
                def connect(self):
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.sock.connect({str(sock)!r})
            c = UnixConn("harness")
            order = {{"coin": "BTC", "is_buy": True, "sz": 0.001, "limit_px": 100100,
                      "order_type": {{"limit": {{"tif": "Ioc"}}}}}}
            c.request("POST", "/call", json.dumps({{"method": "order", "params": {{"orders": [order]}}}}),
                      {{"Content-Type": "application/json"}})
            print(json.loads(c.getresponse().read())["ok"])
            try:
                socket.create_connection(("api.hyperliquid.xyz", 443), timeout=3); print("NETWORK")
            except OSError:  # even the name lookup fails: it needs a network socket
                print("no network")
            try:
                socket.create_connection(("1.1.1.1", 443), timeout=3); print("NETWORK")
            except PermissionError:
                print("refused")
            """
        )
    )
    out = subprocess.run(
        [sys.executable, "-m", "kokoah_subnet.agent.sandbox", "--", sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    server.should_exit = True
    thread.join(timeout=10)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split("\n")[:3] == ["True", "no network", "refused"]
    action = world["exchange"].posted[-1]["action"]
    assert action["builder"] == {"b": BUILDER, "f": 25} and action["orders"][0]["c"].startswith("0x6b6f0603")
