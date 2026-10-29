"""Hyperliquid action signing, as the official SDK does it (hyperliquid-python-sdk, utils/signing.py; checked
against its published test vectors in tests/test_agent_signing.py).

Two kinds of signatures:
- L1 actions (orders, cancels, leverage, schedule cancel): keccak of the msgpack'd action, the nonce, the vault flag
  and an optional expiry, signed as an EIP-712 "Agent" message on the "Exchange" domain (chain id 1337).
- User-signed actions (withdraw, builder fee approval): EIP-712 on the "HyperliquidSignTransaction" domain.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

import msgpack
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils.conversions import to_hex
from eth_utils.crypto import keccak

SIGNATURE_CHAIN_ID = "0x66eee"
_DOMAIN_FIELDS = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]
WITHDRAW_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "destination", "type": "string"},
    {"name": "amount", "type": "string"},
    {"name": "time", "type": "uint64"},
]
APPROVE_BUILDER_FEE_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "maxFeeRate", "type": "string"},
    {"name": "builder", "type": "address"},
    {"name": "nonce", "type": "uint64"},
]
USD_CLASS_TRANSFER_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "amount", "type": "string"},
    {"name": "toPerp", "type": "bool"},
    {"name": "nonce", "type": "uint64"},
]


def float_to_wire(x: float) -> str:
    if not math.isfinite(x):  # "NaN" and "Infinity" would otherwise be signed into the action
        raise ValueError(f"not a finite number: {x}")
    rounded = f"{x:.8f}"
    if abs(float(rounded) - x) >= 1e-12:
        raise ValueError(f"float_to_wire causes rounding: {x}")
    if rounded == "-0":
        rounded = "0"
    return f"{Decimal(rounded).normalize():f}"


def float_to_int(x: float, power: int = 8) -> int:
    if not math.isfinite(x):
        raise ValueError(f"not a finite number: {x}")
    scaled = x * 10**power
    if abs(round(scaled) - scaled) >= 1e-3:
        raise ValueError(f"float_to_int causes rounding: {x}")
    return int(round(scaled))


def action_hash(action: Any, vault_address: str | None, nonce: int, expires_after: int | None) -> bytes:
    data = msgpack.packb(action)
    data += nonce.to_bytes(8, "big")
    if vault_address is None:
        data += b"\x00"
    else:
        data += b"\x01" + bytes.fromhex(vault_address.removeprefix("0x"))
    if expires_after is not None:
        data += b"\x00" + expires_after.to_bytes(8, "big")
    return bytes(keccak(data))


def _l1_payload(connection_id: bytes, mainnet: bool) -> dict[str, Any]:
    return {
        "domain": {
            "chainId": 1337,
            "name": "Exchange",
            "verifyingContract": "0x0000000000000000000000000000000000000000",
            "version": "1",
        },
        "types": {
            "Agent": [{"name": "source", "type": "string"}, {"name": "connectionId", "type": "bytes32"}],
            "EIP712Domain": _DOMAIN_FIELDS,
        },
        "primaryType": "Agent",
        "message": {"source": "a" if mainnet else "b", "connectionId": connection_id},
    }


def _user_payload(primary_type: str, types: list[dict[str, str]], action: dict[str, Any]) -> dict[str, Any]:
    return {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": int(action["signatureChainId"], 16),
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "types": {primary_type: types, "EIP712Domain": _DOMAIN_FIELDS},
        "primaryType": primary_type,
        "message": action,
    }


def _sign(key: Any, payload: dict[str, Any]) -> dict[str, Any]:
    signed = Account.sign_message(encode_typed_data(full_message=payload), private_key=key)
    return {"r": to_hex(signed.r), "s": to_hex(signed.s), "v": signed.v}


def sign_l1_action(
    key: Any, action: Any, vault_address: str | None, nonce: int, expires_after: int | None, mainnet: bool
) -> dict[str, Any]:
    return _sign(key, _l1_payload(action_hash(action, vault_address, nonce, expires_after), mainnet))


def recover_l1_signer(
    action: Any,
    signature: dict[str, Any],
    vault_address: str | None,
    nonce: int,
    expires_after: int | None,
    mainnet: bool,
) -> str:
    """The lowercase address that signed an L1 action (how a validator tells which key placed an order)."""
    payload = _l1_payload(action_hash(action, vault_address, nonce, expires_after), mainnet)
    vrs = (signature["v"], int(signature["r"], 16), int(signature["s"], 16))
    return str(Account.recover_message(encode_typed_data(full_message=payload), vrs=vrs)).lower()


def sign_user_action(
    key: Any, action: dict[str, Any], types: list[dict[str, str]], primary_type: str, mainnet: bool
) -> dict[str, Any]:
    action["signatureChainId"] = SIGNATURE_CHAIN_ID
    action["hyperliquidChain"] = "Mainnet" if mainnet else "Testnet"
    return _sign(key, _user_payload(primary_type, types, action))


def withdraw_action(destination: str, amount_usd: str, time_ms: int) -> dict[str, Any]:
    return {"type": "withdraw3", "destination": destination.lower(), "amount": amount_usd, "time": time_ms}


def approve_builder_fee_action(builder: str, max_fee_rate: str, nonce: int) -> dict[str, Any]:
    return {"type": "approveBuilderFee", "maxFeeRate": max_fee_rate, "builder": builder.lower(), "nonce": nonce}


def usd_class_transfer_action(amount_usd: str, to_perp: bool, nonce: int) -> dict[str, Any]:
    """Moves USDC between the account's own spot and perpetuals balances."""
    return {"type": "usdClassTransfer", "amount": amount_usd, "toPerp": to_perp, "nonce": nonce}
