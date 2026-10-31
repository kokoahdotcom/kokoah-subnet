"""Every exchange action the harness sends, on mainnet and on testnet, is byte-for-byte what Hyperliquid's official
Python SDK sends for the same key, nonce and order id (`data/hyperliquid_sdk_vectors.json`); and a signed stop
cancels the open orders and ends the agent's process."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from kokoah_subnet.agent.attest import DevelopmentAttestor
from kokoah_subnet.agent.harness import Harness, HarnessConfig, stop_message, withdraw_message
from kokoah_subnet.agent.supervisor import AgentRunner

VECTORS = json.loads((Path(__file__).parent / "data" / "hyperliquid_sdk_vectors.json").read_text())["vectors"]
KEY = bytes.fromhex("11" * 32)
MASTER = Account.from_key("0x" + "22" * 32)
NONCE = 1792700000000
CLOID = "0x6b6f0603eb18dfb05fef6732c0442bed"
BUILDER = "0x43ccc0618e4358494f87a4b8699230203dc4ab67"


def harness(mainnet: bool, posted: list[dict[str, Any]], open_orders: list[dict[str, Any]] | None = None) -> Harness:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/info":
            kind = body["type"]
            if kind == "meta":
                return httpx.Response(
                    200, json={"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH", "szDecimals": 4}]}
                )
            if kind == "allMids":
                return httpx.Response(200, json={"BTC": "80500", "ETH": "4000"})
            if kind == "spotClearinghouseState":
                return httpx.Response(200, json={"balances": [{"coin": "USDC", "total": "250.05", "hold": "0"}]})
            if kind == "openOrders":
                return httpx.Response(200, json=open_orders or [])
            return httpx.Response(200, json=[])
        posted.append(body)
        return httpx.Response(200, json={"status": "ok", "response": {"type": "default"}})

    cfg = HarnessConfig(
        netuid=385, hotkey="5Hk", master=MASTER.address, code_sha256="eb" * 32, builder=BUILDER, mainnet=mainnet
    )
    h = Harness(
        cfg,
        KEY,
        DevelopmentAttestor(None, "dev"),
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x"),
    )
    h.builder_approved = True
    return h


@pytest.mark.parametrize("net", ["mainnet", "testnet"])
def test_every_action_matches_the_official_sdk(net: str, monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict[str, Any]] = []
    h = harness(net == "mainnet", posted)
    monkeypatch.setattr(h, "_nonce", lambda: NONCE)
    monkeypatch.setattr(h, "new_cloid", lambda: CLOID)
    h.call("update_leverage", {"coin": "BTC", "leverage": 5, "is_cross": True})
    h.call("update_leverage", {"coin": "ETH", "leverage": 3, "is_cross": False})
    h.call("update_isolated_margin", {"coin": "BTC", "amount_usd": 12.5})
    h.call("schedule_cancel", {"time_ms": NONCE + 60_000})
    h.call("schedule_cancel", {})
    h.call("cancel", {"coin": "BTC", "oid": 123456})
    h.call("cancel_by_cloid", {"coin": "BTC", "cloid": CLOID})
    h.call(
        "order",
        {
            "orders": [
                {
                    "coin": "BTC",
                    "is_buy": True,
                    "sz": 0.00015,
                    "limit_px": 80063.0,
                    "order_type": {"limit": {"tif": "Alo"}},
                }
            ]
        },
    )
    h.call(
        "order",
        {
            "orders": [
                {
                    "coin": "BTC",
                    "is_buy": False,
                    "sz": 0.00015,
                    "limit_px": 80000.0,
                    "reduce_only": True,
                    "order_type": {"trigger": {"triggerPx": 79000.0, "isMarket": True, "tpsl": "sl"}},
                }
            ]
        },
    )
    h.approve_builder()
    t = int(time.time() * 1000)
    signed = MASTER.sign_message(encode_defunct(text=withdraw_message(h.address, "100.5", t)))
    h.withdraw("100.5", t, "0x" + bytes(signed.signature).hex())
    h.fund_perps()
    expected = VECTORS[net]
    assert len(posted) == len(expected)
    for sent, want in zip(posted, expected, strict=True):
        assert sent["action"] == want["action"], want["name"]
        assert sent["nonce"] == want["nonce"], want["name"]
        got = {k: str(v).lower() for k, v in sent["signature"].items()}
        assert got == {k: str(v).lower() for k, v in want["signature"].items()}, want["name"]


def test_a_signed_stop_cancels_the_orders_and_ends_the_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kokoah_subnet.agent import supervisor

    monkeypatch.setattr(supervisor, "AGENT_HOME", tmp_path)  # the agent's directory inside the sealed machine
    posted: list[dict[str, Any]] = []
    orders = [{"coin": "BTC", "oid": 11}, {"coin": "ETH", "oid": 12}]
    runner = AgentRunner([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path)
    h = harness(True, posted, orders)
    h.on_stop = runner.stop
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    for _ in range(100):
        if runner.proc is not None:
            break
        time.sleep(0.05)
    proc = runner.proc
    assert proc is not None and proc.poll() is None
    t = int(time.time() * 1000)
    signed = MASTER.sign_message(encode_defunct(text=stop_message(h.address, t)))
    assert h.stop(t, "0x" + bytes(signed.signature).hex()) == {"cancelled": 2}
    assert [p["action"]["cancels"][0]["o"] for p in posted] == [11, 12]
    thread.join(timeout=10)
    assert not thread.is_alive() and proc.poll() is not None  # the agent's process is gone and is not restarted
    assert isinstance(proc, subprocess.Popen)
