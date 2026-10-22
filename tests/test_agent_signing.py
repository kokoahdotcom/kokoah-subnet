"""Hyperliquid signing matches the official SDK's published test vectors (hyperliquid-python-sdk,
tests/signing_test.py, read 2026-10-22)."""

from eth_account import Account

from kokoah_subnet.agent.hlsign import (
    WITHDRAW_TYPES,
    action_hash,
    float_to_int,
    float_to_wire,
    recover_l1_signer,
    sign_l1_action,
    sign_user_action,
    withdraw_action,
)

KEY = "0x0123456789012345678901234567890123456789012345678901234567890123"


def order(asset: int, sz: float, px: float, tif: str, cloid: str | None = None) -> dict:
    wire = {
        "a": asset,
        "b": True,
        "p": float_to_wire(px),
        "s": float_to_wire(sz),
        "r": False,
        "t": {"limit": {"tif": tif}},
    }
    if cloid:
        wire["c"] = cloid
    return {"type": "order", "orders": [wire], "grouping": "na"}


def test_phantom_agent_hash_matches_production() -> None:
    h = action_hash(order(4, 0.0147, 1670.1, "Ioc"), None, 1677777606040, None)
    assert "0x" + h.hex() == "0x0fcbeda5ae3c4950a548021552a4fea2226858c4453571bf3f24ba017eac2908"


def test_l1_signing_matches() -> None:
    action = {"type": "dummy", "num": float_to_int(1000)}
    main = sign_l1_action(KEY, action, None, 0, None, True)
    assert main == {
        "r": "0x53749d5b30552aeb2fca34b530185976545bb22d0b3ce6f62e31be961a59298",
        "s": "0x755c40ba9bf05223521753995abb2f73ab3229be8ec921f350cb447e384d8ed8",
        "v": 27,
    }
    test = sign_l1_action(KEY, action, None, 0, None, False)
    assert test["r"] == "0x542af61ef1f429707e3c76c5293c80d01f74ef853e34b76efffcb57e574f9510" and test["v"] == 28


def test_order_signing_matches_with_and_without_cloid() -> None:
    main = sign_l1_action(KEY, order(1, 100, 100, "Gtc"), None, 0, None, True)
    assert main["r"] == "0xd65369825a9df5d80099e513cce430311d7d26ddf477f5b3a33d2806b100d78e" and main["v"] == 28
    with_cloid = sign_l1_action(
        KEY, order(1, 100, 100, "Gtc", "0x00000000000000000000000000000001"), None, 0, None, True
    )
    assert with_cloid["r"] == "0x41ae18e8239a56cacbc5dad94d45d0b747e5da11ad564077fcac71277a946e3"
    assert with_cloid["s"] == "0x3c61f667e747404fe7eea8f90ab0e76cc12ce60270438b2058324681a00116da"


def test_vault_and_schedule_cancel_match() -> None:
    action = {"type": "dummy", "num": float_to_int(1000)}
    vault = sign_l1_action(KEY, action, "0x1719884eb866cb12b2287399b15f7db5e7d775ea", 0, None, True)
    assert vault["r"] == "0x3c548db75e479f8012acf3000ca3a6b05606bc2ec0c29c50c515066a326239" and vault["v"] == 28
    sc = sign_l1_action(KEY, {"type": "scheduleCancel"}, None, 0, None, True)
    assert sc["r"] == "0x6cdfb286702f5917e76cd9b3b8bf678fcc49aec194c02a73e6d4f16891195df9"


def test_withdraw_signing_matches() -> None:
    action = withdraw_action("0x5e9ee1089755c3435139848e47e6635505d5a13a", "1", 1687816341423)
    sig = sign_user_action(KEY, action, WITHDRAW_TYPES, "HyperliquidTransaction:Withdraw", False)
    assert sig == {
        "r": "0x8363524c799e90ce9bc41022f7c39b4e9bdba786e5f9c72b20e43e1462c37cf9",
        "s": "0x58b1411a775938b83e29182e8ef74975f9054c8e97ebf5ec2dc8d51bfc893881",
        "v": 28,
    }


def test_the_signer_of_an_order_can_be_recovered() -> None:
    action = order(1, 100, 100, "Gtc")
    sig = sign_l1_action(KEY, action, None, 42, None, True)
    assert recover_l1_signer(action, sig, None, 42, None, True) == Account.from_key(KEY).address.lower()
    other = Account.create()
    sig2 = sign_l1_action(other.key, action, None, 42, None, True)
    assert recover_l1_signer(action, sig2, None, 42, None, True) == other.address.lower()
