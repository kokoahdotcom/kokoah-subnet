"""The trusted harness: builder code and agent marker on every order, signed by the account's own key; prices far
from the mid, unoffered methods and other accounts' data refused; withdrawals only to the miner, only when signed."""

import json
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from fastapi.testclient import TestClient

from kokoah_subnet.agent import harness as harness_module
from kokoah_subnet.agent import hlsign
from kokoah_subnet.agent.attest import DevelopmentAttestor, StaticKeys, binding_digest, verify_token
from kokoah_subnet.agent.harness import Harness, HarnessConfig, agent_app, public_app, stop_message, withdraw_message
from kokoah_subnet.agent.hlsign import USD_CLASS_TRANSFER_TYPES, recover_l1_signer
from kokoah_subnet.binding import binding_message, recover_signer

BUILDER = "0x43ccc0618e4358494f87a4b8699230203dc4ab67"
IMAGE = "sha256:" + "ab" * 32
CODE = "11" * 32


class FakeExchange:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.info_requests: list[dict[str, Any]] = []
        self.spot: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/info":
            self.info_requests.append(body)
            t = body["type"]
            if t == "meta":
                return httpx.Response(200, json={"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH"}]})
            if t == "allMids":
                return httpx.Response(200, json={"BTC": "100000", "ETH": "4000"})
            if t == "openOrders":
                return httpx.Response(200, json=[{"coin": "BTC", "oid": 7}])
            if t == "spotClearinghouseState":
                return httpx.Response(200, json={"balances": self.spot})
            return httpx.Response(200, json={"echo": body})
        self.posted.append(body)
        return httpx.Response(200, json={"status": "ok", "response": {"type": "default"}})


@pytest.fixture()
def setup() -> tuple[Harness, FakeExchange, Any, Any]:
    fake = FakeExchange()
    master = Account.create()
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cfg = HarnessConfig(netuid=2, hotkey="5Hk", master=master.address, code_sha256=CODE, builder=BUILDER)
    h = Harness(
        cfg,
        Account.create().key,
        DevelopmentAttestor(rsa_key, IMAGE),
        http=httpx.Client(transport=httpx.MockTransport(fake.handler), base_url="https://x"),
    )
    h.builder_approved = True  # the approval itself is tested separately
    return h, fake, master, rsa_key


def test_orders_carry_the_builder_code_and_marker_and_are_signed_by_the_account(setup: Any) -> None:
    h, fake, _, _ = setup
    out = h.call("order", {"orders": [{"coin": "BTC", "is_buy": True, "sz": 0.01, "limit_px": 100500}]})
    sent = fake.posted[-1]
    action = sent["action"]
    assert action["builder"] == {"b": BUILDER, "f": 25}
    wire = action["orders"][0]
    assert wire["a"] == 0 and wire["p"] == "100500" and wire["s"] == "0.01" and wire["t"] == {"limit": {"tif": "Gtc"}}
    assert wire["c"].startswith("0x6b6f0603" + CODE[:16]) and len(wire["c"]) == 34
    assert out["cloids"] == [wire["c"]]
    assert recover_l1_signer(action, sent["signature"], None, sent["nonce"], None, True) == h.address


def test_off_market_prices_and_unoffered_methods_are_refused(setup: Any) -> None:
    h, fake, _, _ = setup
    with pytest.raises(ValueError, match="from the mid"):
        h.call("order", {"orders": [{"coin": "BTC", "is_buy": True, "sz": 1, "limit_px": 120000}]})
    for method in ("withdraw", "modify", "twap_order", "approve_agent", "__class__", "_l1"):
        with pytest.raises(ValueError, match="not offered"):
            h.call(method, {})
    with pytest.raises(ValueError, match="unknown perpetual"):
        h.call("order", {"orders": [{"coin": "PURR/USDC", "is_buy": True, "sz": 1, "limit_px": 1}]})
    assert not fake.posted


def test_account_queries_answer_for_this_account_only(setup: Any) -> None:
    h, fake, _, _ = setup
    h.agent_info({"type": "clearinghouseState", "user": "0xsomeoneelse"})
    assert fake.info_requests[-1]["user"] == h.address
    with pytest.raises(ValueError, match="not offered"):
        h.agent_info({"type": "subAccounts", "user": h.address})


def test_withdrawals_go_only_to_the_miner_and_need_its_signature(setup: Any) -> None:
    h, fake, master, _ = setup
    t = int(time.time() * 1000)
    sig = "0x" + bytes(master.sign_message(encode_defunct(text=withdraw_message(h.address, "10", t))).signature).hex()
    h.withdraw("10", t, sig)
    action = fake.posted[-1]["action"]
    assert action["type"] == "withdraw3" and action["destination"] == master.address.lower()
    with pytest.raises(PermissionError, match="replayed"):
        h.withdraw("10", t, sig)
    stranger = Account.create()
    t2 = t + 1
    bad = (
        "0x" + bytes(stranger.sign_message(encode_defunct(text=withdraw_message(h.address, "10", t2))).signature).hex()
    )
    with pytest.raises(PermissionError, match="miner"):
        h.withdraw("10", t2, bad)
    old = t - 10 * 60_000
    stale = (
        "0x" + bytes(master.sign_message(encode_defunct(text=withdraw_message(h.address, "1", old))).signature).hex()
    )
    with pytest.raises(PermissionError, match="stale"):
        h.withdraw("1", old, stale)


def test_attestation_and_binding_speak_for_this_account(setup: Any) -> None:
    h, _, _, rsa_key = setup
    challenge = "cd" * 32
    att = h.attestation(challenge)
    bind = binding_digest(2, "5Hk", h.address, CODE)
    verify_token(att["token"], StaticKeys(rsa_key.public_key()), [IMAGE], [challenge, bind])
    b = h.binding()
    assert b["message"] == binding_message(2, "5Hk", h.address)
    assert recover_signer(2, "5Hk", h.address, b["signature"]) == h.address
    with pytest.raises(ValueError):
        h.attestation("not hex!")


def test_the_two_interfaces(setup: Any) -> None:
    h, fake, master, _ = setup
    agent = TestClient(agent_app(h))
    assert agent.get("/address").json() == {"address": h.address}
    assert agent.post("/call", json={"method": "withdraw", "params": {}}).json()["ok"] is False
    ok = agent.post("/call", json={"method": "cancel", "params": {"coin": "ETH", "oid": 5}}).json()
    assert ok["ok"] and fake.posted[-1]["action"] == {"type": "cancel", "cancels": [{"a": 1, "o": 5}]}
    public = TestClient(public_app(h))
    assert public.get("/status").json()["code_sha256"] == CODE
    assert public.get("/attestation", params={"challenge": "zz"}).status_code == 400
    t = int(time.time() * 1000)
    sig = "0x" + bytes(master.sign_message(encode_defunct(text=stop_message(h.address, t))).signature).hex()
    assert public.post("/stop", json={"time_ms": t, "signature": sig}).json() == {
        "ok": True,
        "result": {"cancelled": 1},
    }
    assert public.post("/stop", json={"time_ms": t + 1, "signature": sig}).status_code == 403


def test_the_harness_binding_line_is_the_subnet_one() -> None:
    assert harness_module.binding_message(7, "5Abc", "0xAB") == binding_message(7, "5Abc", "0xAB")


def test_an_order_first_approves_the_builder_fee(setup: Any) -> None:
    h, fake, _, _ = setup
    h.builder_approved = False
    h.call("order", {"orders": [{"coin": "BTC", "is_buy": True, "sz": 0.01, "limit_px": 100000}]})
    approval, order = fake.posted[-2]["action"], fake.posted[-1]["action"]
    assert approval["type"] == "approveBuilderFee" and approval["builder"] == BUILDER
    assert approval["maxFeeRate"] == "0.025%" and order["type"] == "order"


def test_spot_usdc_is_moved_to_perpetuals_within_the_account(setup: Any) -> None:
    h, fake, _, _ = setup
    fake.spot = [{"coin": "USDC", "total": "250.456", "hold": "0.4"}]
    h.fund_perps()
    sent = fake.posted[-1]
    action, sig = sent["action"], sent["signature"]
    assert action["type"] == "usdClassTransfer" and action["toPerp"] is True and action["amount"] == "250.05"
    payload = hlsign._user_payload("HyperliquidTransaction:UsdClassTransfer", USD_CLASS_TRANSFER_TYPES, action)
    vrs = (sig["v"], int(sig["r"], 16), int(sig["s"], 16))
    assert str(Account.recover_message(encode_typed_data(full_message=payload), vrs=vrs)).lower() == h.address
    fake.spot = [{"coin": "USDC", "total": "0.5", "hold": "0"}]
    count = len(fake.posted)
    assert h.fund_perps() is None and len(fake.posted) == count
    with pytest.raises(ValueError, match="not offered"):
        h.call("fund_perps", {})
