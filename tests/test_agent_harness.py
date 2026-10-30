"""The trusted harness: builder code and agent marker on every order, signed by the account's own key; prices far
from the mid, unoffered methods and other accounts' data refused; withdrawals only to the miner, only when signed."""

import json
import time
from collections.abc import Callable
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
    for wrong in ("not hex!", "cd" * 31, "cd" * 33, "cd" * 8):  # a validator's challenge is 32 random bytes
        with pytest.raises(ValueError, match="64 hex"):
            h.attestation(wrong)


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


def test_numbers_that_are_not_finite_are_refused_before_signing(setup: Any) -> None:
    h, fake, _, _ = setup
    for x in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            hlsign.float_to_wire(x)
        with pytest.raises(ValueError, match="finite"):
            hlsign.float_to_int(x)
    btc = {"coin": "BTC", "is_buy": True, "sz": 0.01, "limit_px": 100000}
    trigger = {"trigger": {"triggerPx": "NaN", "isMarket": True, "tpsl": "sl"}}
    for order in (
        {**btc, "limit_px": "NaN"},
        {**btc, "sz": float("nan")},
        {**btc, "sz": "inf"},
        {**btc, "order_type": trigger},
    ):
        with pytest.raises(ValueError):
            h.call("order", {"orders": [order]})
    with pytest.raises(ValueError, match="finite"):
        h.call("update_isolated_margin", {"coin": "BTC", "amount_usd": "inf"})
    agent = TestClient(agent_app(h))
    body = '{"method": "update_leverage", "params": {"coin": "BTC", "leverage": Infinity}}'  # JSON parsers accept it
    answer = agent.post("/call", content=body, headers={"content-type": "application/json"})
    assert answer.status_code == 200 and answer.json()["ok"] is False
    assert not fake.posted


def test_triggers_are_banded_where_they_execute(setup: Any) -> None:
    """The mid is 100000. A stop or take-profit that has not fired rests wherever it is placed, and its limit must be
    near its own trigger; one already past its trigger executes now, so its limit must be near the mid."""
    h, fake, _, _ = setup

    def order(is_buy: bool, tpsl: str, trigger: float, limit: float) -> Any:
        o = {"coin": "BTC", "is_buy": is_buy, "sz": 0.01, "limit_px": limit, "reduce_only": True}
        o["order_type"] = {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": tpsl}}
        return h.call("order", {"orders": [o]})

    order(False, "sl", 80000, 79000)  # a long's stop-loss 20% below the market
    order(True, "sl", 130000, 131000)  # a short's stop-loss 30% above
    order(False, "tp", 125000, 124000)  # a long's take-profit 25% above
    order(True, "tp", 85000, 85500)  # a short's take-profit 15% below
    assert len(fake.posted) == 4 and fake.posted[0]["action"]["orders"][0]["t"]["trigger"]["triggerPx"] == "80000"
    with pytest.raises(ValueError, match="from its trigger"):
        order(False, "sl", 80000, 70000)  # it would execute 12.5% below its trigger
    with pytest.raises(ValueError, match="from the mid"):
        order(False, "tp", 85000, 85000)  # a take-profit below the market fires at once, 15% below the mid
    with pytest.raises(ValueError, match="from the mid"):
        order(True, "sl", 85000, 85000)  # a stop-buy below the market fires at once too
    with pytest.raises(ValueError, match="trigger price"):
        order(False, "sl", 0, 0)
    for unknown in ("TP", "take_profit", "", None):  # the direction is never guessed
        with pytest.raises(ValueError, match="tpsl must be tp or sl"):
            order(False, unknown, 125000, 124000)  # type: ignore[arg-type]
    assert len(fake.posted) == 4
    fired = harness_module.trigger_fired
    assert fired(True, "sl", 100000, 100000) and fired(False, "tp", 100000, 100000)  # at the trigger: it fires


def test_signed_requests_read_the_signature_with_or_without_0x(setup: Any) -> None:
    h, fake, master, _ = setup
    public = TestClient(public_app(h))
    t = int(time.time() * 1000)
    bare = bytes(master.sign_message(encode_defunct(text=withdraw_message(h.address, "5", t))).signature).hex()
    answer = public.post("/withdraw", json={"time_ms": t, "signature": bare, "amount_usd": "5"})
    assert answer.status_code == 200 and answer.json()["ok"] and fake.posted[-1]["action"]["type"] == "withdraw3"
    for unreadable in ("0x" + "00" * 64, "0x" + "00" * 65, "zz"):
        refused = public.post("/stop", json={"time_ms": t + 1, "signature": unreadable})
        assert refused.status_code == 403 and "miner" in refused.json()["detail"]


def test_each_signed_request_acts_once(setup: Any) -> None:
    import threading

    h, fake, master, _ = setup
    t = int(time.time() * 1000)

    def signed(text: str) -> str:
        return "0x" + bytes(master.sign_message(encode_defunct(text=text)).signature).hex()

    stop_sig, withdraw_sig = signed(stop_message(h.address, t)), signed(withdraw_message(h.address, "3", t))
    h.stop(t, stop_sig)  # a stop and a withdrawal signed in the same millisecond are two requests
    h.withdraw("3", t, withdraw_sig)
    h.withdraw("4", t - 1000, signed(withdraw_message(h.address, "4", t - 1000)))  # signed earlier, sent later
    with pytest.raises(PermissionError, match="replayed"):
        h.stop(t, stop_sig)
    with pytest.raises(PermissionError, match="replayed"):
        h.withdraw("3", t, withdraw_sig)
    count = len(fake.posted)
    twice = signed(withdraw_message(h.address, "7", t + 1))
    results: list[str] = []

    def send() -> None:
        try:
            h.withdraw("7", t + 1, twice)
            results.append("sent")
        except PermissionError:
            results.append("refused")

    threads = [threading.Thread(target=send) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == ["refused"] * 7 + ["sent"] and len(fake.posted) == count + 1


def test_an_order_tells_why_the_builder_fee_is_not_approved(setup: Any) -> None:
    h, fake, _, _ = setup
    h.builder_approved = False
    real: Callable[[httpx.Request], httpx.Response] = fake.handler

    def refusing(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/exchange" and json.loads(request.content)["action"]["type"] == "approveBuilderFee":
            return httpx.Response(200, json={"status": "err", "response": "Must deposit before performing actions."})
        return real(request)

    h.http = httpx.Client(transport=httpx.MockTransport(refusing), base_url="https://x")
    with pytest.raises(ValueError, match="Must deposit before performing actions"):
        h.call("order", {"orders": [{"coin": "BTC", "is_buy": True, "sz": 0.01, "limit_px": 100000}]})
    h.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(502)), base_url="https://x")
    with pytest.raises(ValueError, match="502"):
        h.call("order", {"orders": [{"coin": "BTC", "is_buy": True, "sz": 0.01, "limit_px": 100000}]})
    assert not h.builder_approved


def test_the_agent_guide_lists_exactly_the_info_requests_answered() -> None:
    import re
    from pathlib import Path

    guide = (Path(__file__).resolve().parents[1] / "docs" / "agents.md").read_text()
    row = next(line for line in guide.splitlines() if line.startswith("| `POST /info"))
    listed = set(re.findall(r"`([A-Za-z0-9]+)`", row))
    assert listed == harness_module.PUBLIC_INFO | harness_module.ACCOUNT_INFO


def test_the_machine_talks_only_to_the_key_service_its_image_names() -> None:
    """Whoever starts a machine cannot point it at a relay: a production image uses its own https service whatever
    the environment says, and refuses to start without one; only a development image in development mode takes a
    local service."""
    from kokoah_subnet.agent.supervisor import ImageSettings, service_url

    image = ImageSettings("0x" + "43" * 20, 25, service="https://api.kokoah.com/miners")
    relay = {"KOKOAH_AGENT_SERVICE": "https://relay.example"}
    assert service_url(image, False, relay) == "https://api.kokoah.com/miners"
    assert service_url(image, True, relay) == "https://api.kokoah.com/miners"  # a release image, even told "dev"
    for bare in (ImageSettings("0x" + "43" * 20, 25), ImageSettings("0x" + "43" * 20, 25, service="http://svc")):
        with pytest.raises(SystemExit, match="https key service"):
            service_url(bare, False, {})
    dev = ImageSettings("0x" + "43" * 20, 25, development=True)
    assert service_url(dev, True, {"KOKOAH_AGENT_SERVICE": "http://127.0.0.1:48131"}) == "http://127.0.0.1:48131"
    with pytest.raises(SystemExit, match="set KOKOAH_AGENT_SERVICE"):
        service_url(dev, True, {})


def test_the_release_image_names_the_key_service_and_the_miner_cannot_override_it() -> None:
    """The launch policy lets exactly these start settings in, and no command, mount or capability override: so no
    key service, proxy, certificate or development setting can reach a release machine."""
    import re
    from pathlib import Path

    from kokoah_subnet.agent.supervisor import ImageSettings, development_mode

    root = Path(__file__).resolve().parents[1]
    image = ImageSettings.load(root / "deploy" / "agent" / "image.json")
    assert image.service == "https://api.kokoah.com/miners" and image.development is False
    assert not development_mode(image, {"KOKOAH_AGENT_DEV": "1"})
    dockerfile = (root / "deploy" / "agent" / "Dockerfile").read_text()
    labels = dict(re.findall(r'^LABEL "tee\.launch_policy\.([a-z_]+)"="([^"]*)"$', dockerfile, flags=re.M))
    assert set(labels) == {"allow_env_override", "log_redirect", "monitoring_memory_allow"}
    assert set(labels["allow_env_override"].split(",")) == {
        f"KOKOAH_AGENT_{name}"
        for name in ("SUBMISSION", "BUNDLE_KEY", "NETUID", "HOTKEY", "MASTER", "SECRET", "PERMIT", "RESTORE", "NETWORK")
    }
    assert labels["log_redirect"] == "debugonly"
    dev = ImageSettings.load(root / "deploy" / "agent" / "image.dev.json")
    assert dev.development and development_mode(dev, {"KOKOAH_AGENT_DEV": "1"}) and not development_mode(dev, {})
