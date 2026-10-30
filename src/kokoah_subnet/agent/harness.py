"""The trusted harness inside the sealed machine (specification section 16).

It holds the account's only key, which it creates inside the machine and never exports; nobody else can sign for
the account, so every action on it comes from here. It offers the miner's agent a narrow trading interface over a
Unix socket (the agent has no other way out, see `sandbox.py`) and the outside world a small public interface.

To the agent (Unix socket, `agent_app`):
  POST /call {method, params}: `order` (one or more limit or trigger orders), `cancel`, `cancel_by_cloid`,
      `update_leverage`, `update_isolated_margin`, `schedule_cancel`. Kokoah's builder code is attached to every
      order; the order id carries Kokoah's agent marker. An order that executes now (a limit order, or a trigger
      already past its trigger price) is refused when priced more than `price_band` from the mid; a trigger that has
      not fired yet rests wherever it is placed, and its limit price must be within `price_band` of its trigger.
      Not offered: modify (its builder handling is unverified), TWAP and trailing stops (no builder field),
      withdrawals, transfers and approvals.
  POST /info {request}: read-only exchange data. Account queries are answered for this account only.
  GET /address.

To the world (TCP, `public_app`):
  GET /status, GET /binding (the subnet binding line signed by the account), GET /attestation?challenge=<64 hex>
  (a fresh token with the challenge and the binding digest as nonces, the validators' 32 random bytes), POST
  /withdraw and POST /stop (both signed by the miner's own address; withdrawals go only to that address).
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .attest import Attestor, binding_digest
from .hlsign import (
    APPROVE_BUILDER_FEE_TYPES,
    USD_CLASS_TRANSFER_TYPES,
    WITHDRAW_TYPES,
    approve_builder_fee_action,
    float_to_wire,
    sign_l1_action,
    sign_user_action,
    usd_class_transfer_action,
    withdraw_action,
)

HARNESS_VERSION = "1.2.0"
MAINNET_API = "https://api.hyperliquid.xyz"
TESTNET_API = "https://api.hyperliquid-testnet.xyz"
AGENT_CLOID_PREFIX = "6b6f0603"  # "ko", origin 0x06 (agent), layout 0x03 (kokoah-shared actions/cloid.ts)
PUBLIC_INFO = frozenset(
    {"meta", "metaAndAssetCtxs", "allMids", "l2Book", "candleSnapshot", "fundingHistory", "predictedFundings"}
)
ACCOUNT_INFO = frozenset(
    {
        "clearinghouseState",
        "openOrders",
        "frontendOpenOrders",
        "userFills",
        "userFillsByTime",
        "userFunding",
        "orderStatus",
        "historicalOrders",
        "userFees",
        "userRateLimit",
        "portfolio",
    }
)
AGENT_METHODS = frozenset(
    {"order", "cancel", "cancel_by_cloid", "update_leverage", "update_isolated_margin", "schedule_cancel"}
)
SIGNED_REQUEST_WINDOW_MS = 300_000


def binding_message(netuid: int, hotkey: str, address: str) -> str:
    """The subnet binding line (identical to `kokoah_subnet.binding.binding_message`, kept here so the sealed
    machine's image does not need the chain SDK; a test keeps the two the same)."""
    return f"kokoah-bind-1 netuid={netuid} hotkey={hotkey} address={address.lower()}"


@dataclass(frozen=True)
class HarnessConfig:
    netuid: int
    hotkey: str  # the subnet hotkey this account is bound to
    master: str  # the miner's own address: the only withdrawal destination, and the signer of /withdraw and /stop
    code_sha256: str  # the fingerprint of the agent code this machine runs
    builder: str  # Kokoah's builder address
    builder_fee_tenths_bps: int = 25  # 0.025%
    mainnet: bool = True
    price_band: float = 0.05  # how far an order may execute from the market (see `order`)

    @property
    def api(self) -> str:
        return MAINNET_API if self.mainnet else TESTNET_API


class Harness:
    def __init__(
        self,
        config: HarnessConfig,
        key: bytes,
        attestor: Attestor,
        http: httpx.Client | None = None,
        on_stop: Callable[[], None] | None = None,
        allow_writes: bool = True,
    ) -> None:
        self.config = config
        self._account = Account.from_key(key)  # the key lives only in this object's memory
        self.address = self._account.address.lower()
        self.attestor = attestor
        self.http = http or httpx.Client(timeout=20)
        self.on_stop = on_stop
        self.started_at = int(time.time())
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0
        self._signed_lock = threading.Lock()
        self._signed: dict[str, int] = {}  # every master-signed message accepted within the window, with its time
        self._assets: dict[str, tuple[int, int]] = {}  # coin -> (asset id, size decimals)
        self.builder_approved = False
        self.allow_writes = allow_writes  # False only in development runs: nothing is sent to the exchange

    # ----- exchange plumbing -------------------------------------------------------------------------------

    def _nonce(self) -> int:
        with self._nonce_lock:
            n = max(int(time.time() * 1000), self._last_nonce + 1)
            self._last_nonce = n
            return n

    def info(self, request: dict[str, Any]) -> Any:
        r = self.http.post(f"{self.config.api}/info", json=request)
        r.raise_for_status()
        return r.json()

    def _post_exchange(self, action: dict[str, Any], signature: dict[str, Any], nonce: int) -> Any:
        if not self.allow_writes:
            raise ValueError("exchange writes are off in this development run")
        body = {"action": action, "nonce": nonce, "signature": signature, "vaultAddress": None}
        r = self.http.post(f"{self.config.api}/exchange", json=body)
        r.raise_for_status()
        return r.json()

    def _l1(self, action: dict[str, Any]) -> Any:
        nonce = self._nonce()
        sig = sign_l1_action(self._account.key, action, None, nonce, None, self.config.mainnet)
        return self._post_exchange(action, sig, nonce)

    def _user(self, action: dict[str, Any], types: list[dict[str, str]], primary: str, nonce: int) -> Any:
        sig = sign_user_action(self._account.key, action, types, primary, self.config.mainnet)
        return self._post_exchange(action, sig, nonce)

    def asset(self, coin: str) -> tuple[int, int]:
        if coin not in self._assets:
            meta = self.info({"type": "meta"})
            self._assets = {a["name"]: (i, int(a.get("szDecimals", 0))) for i, a in enumerate(meta["universe"])}
        if coin not in self._assets:
            raise ValueError(f"unknown perpetual: {coin} (the subnet counts the main perpetuals only)")
        return self._assets[coin]

    def approve_builder(self) -> Any:
        """Approve Kokoah's builder fee for this account (needs a deposit first; retried until it succeeds)."""
        rate = f"{self.config.builder_fee_tenths_bps / 1000:g}%"
        nonce = self._nonce()
        action = approve_builder_fee_action(self.config.builder, rate, nonce)
        result = self._user(action, APPROVE_BUILDER_FEE_TYPES, "HyperliquidTransaction:ApproveBuilderFee", nonce)
        if isinstance(result, dict) and result.get("status") == "ok":
            self.builder_approved = True
        return result

    def fund_perps(self) -> Any:
        """Move the account's spot USDC to its perpetuals balance, where the agent can trade with it (USDC sent with
        Hyperliquid's "send" lands in spot). The money stays in the account. None when there is under $1 to move."""
        spot = self.info({"type": "spotClearinghouseState", "user": self.address})
        usdc = next((b for b in spot.get("balances", []) if b.get("coin") == "USDC"), None)
        free = float(usdc["total"]) - float(usdc.get("hold", 0) or 0) if usdc else 0.0
        if free < 1:
            return None
        nonce = self._nonce()
        action = usd_class_transfer_action(float_to_wire(math.floor(free * 100) / 100), True, nonce)
        return self._user(action, USD_CLASS_TRANSFER_TYPES, "HyperliquidTransaction:UsdClassTransfer", nonce)

    # ----- what the agent may do -----------------------------------------------------------------------------

    def new_cloid(self) -> str:
        return "0x" + AGENT_CLOID_PREFIX + self.config.code_sha256[:16] + os.urandom(4).hex()

    def _check_price(self, px: float, reference: float, what: str) -> None:
        if abs(px / reference - 1) > self.config.price_band:
            raise ValueError(f"price {px} is more than {self.config.price_band:.0%} from {what} {reference}")

    def order(self, orders: list[dict[str, Any]], grouping: str = "na") -> Any:
        if not orders:
            raise ValueError("no orders")
        if not self.builder_approved:  # the exchange refuses builder orders until the account has approved the fee
            try:
                answer: Any = self.approve_builder()
            except (httpx.HTTPError, ValueError) as exc:
                answer = exc
            if not self.builder_approved:
                said = answer.get("response", answer) if isinstance(answer, dict) else answer
                raise ValueError(
                    f"Kokoah's builder fee is not approved yet (an account needs a deposit first): {str(said)[:200]}"
                )
        mids = self.info({"type": "allMids"})
        wires = []
        cloids = []
        for o in orders:
            coin = str(o["coin"])
            asset, _ = self.asset(coin)
            px = float(o["limit_px"])
            is_buy = bool(o["is_buy"])
            mid = float(mids.get(coin, 0) or 0)
            if mid <= 0:
                raise ValueError(f"no mid price for {coin}")
            order_type = o.get("order_type") or {"limit": {"tif": "Gtc"}}
            if "limit" in order_type:
                tif = order_type["limit"].get("tif", "Gtc")
                if tif not in ("Gtc", "Ioc", "Alo"):
                    raise ValueError(f"unknown time in force {tif}")
                self._check_price(px, mid, "the mid")
                t: dict[str, Any] = {"limit": {"tif": tif}}
            elif "trigger" in order_type:
                trig = order_type["trigger"]
                trigger_px = float(trig["triggerPx"])
                if not trigger_px > 0:
                    raise ValueError(f"bad trigger price {trig['triggerPx']}")
                tpsl = trig.get("tpsl")
                if tpsl not in ("tp", "sl"):  # it picks the trigger's direction: never guessed
                    raise ValueError(f"tpsl must be tp or sl, not {tpsl!r}")
                if trigger_fired(is_buy, tpsl, trigger_px, mid):  # it executes now: banded like a limit order
                    self._check_price(px, mid, "the mid")
                else:  # it rests until the market reaches its trigger, then executes at its limit price
                    self._check_price(px, trigger_px, "its trigger")
                t = {
                    "trigger": {
                        "isMarket": bool(trig.get("isMarket", False)),
                        "triggerPx": float_to_wire(trigger_px),
                        "tpsl": tpsl,
                    }
                }
            else:
                raise ValueError("order_type must be limit or trigger")
            cloid = self.new_cloid()
            cloids.append(cloid)
            wires.append(
                {
                    "a": asset,
                    "b": is_buy,
                    "p": float_to_wire(px),
                    "s": float_to_wire(float(o["sz"])),
                    "r": bool(o.get("reduce_only", False)),
                    "t": t,
                    "c": cloid,
                }
            )
        if grouping not in ("na", "normalTpsl", "positionTpsl"):
            raise ValueError("unknown grouping")
        action = {
            "type": "order",
            "orders": wires,
            "grouping": grouping,
            "builder": {"b": self.config.builder.lower(), "f": self.config.builder_fee_tenths_bps},
        }
        return {"cloids": cloids, "response": self._l1(action)}

    def cancel(self, coin: str, oid: int) -> Any:
        return self._l1({"type": "cancel", "cancels": [{"a": self.asset(coin)[0], "o": int(oid)}]})

    def cancel_by_cloid(self, coin: str, cloid: str) -> Any:
        return self._l1({"type": "cancelByCloid", "cancels": [{"asset": self.asset(coin)[0], "cloid": cloid}]})

    def update_leverage(self, coin: str, leverage: int, is_cross: bool = True) -> Any:
        action = {"type": "updateLeverage", "asset": self.asset(coin)[0], "isCross": bool(is_cross)}
        action["leverage"] = int(leverage)
        return self._l1(action)

    def update_isolated_margin(self, coin: str, amount_usd: float, is_buy: bool = True) -> Any:
        if not math.isfinite(float(amount_usd)):
            raise ValueError(f"not a finite amount: {amount_usd}")
        ntli = round(float(amount_usd) * 1e6)
        action = {"type": "updateIsolatedMargin", "asset": self.asset(coin)[0], "isBuy": bool(is_buy), "ntli": ntli}
        return self._l1(action)

    def schedule_cancel(self, time_ms: int | None = None) -> Any:
        action: dict[str, Any] = {"type": "scheduleCancel"}
        if time_ms is not None:
            action["time"] = int(time_ms)
        return self._l1(action)

    def call(self, method: str, params: dict[str, Any]) -> Any:
        if method not in AGENT_METHODS:  # checked before anything else: no attribute is ever looked up by name
            raise ValueError(f"method not offered: {method}")
        handlers: dict[str, Callable[..., Any]] = {
            "order": self.order,
            "cancel": self.cancel,
            "cancel_by_cloid": self.cancel_by_cloid,
            "update_leverage": self.update_leverage,
            "update_isolated_margin": self.update_isolated_margin,
            "schedule_cancel": self.schedule_cancel,
        }
        return handlers[method](**params)

    def agent_info(self, request: dict[str, Any]) -> Any:
        kind = request.get("type")
        if kind in PUBLIC_INFO:
            return self.info(request)
        if kind in ACCOUNT_INFO:
            return self.info({**request, "user": self.address})  # this account only
        raise ValueError(f"info request not offered: {kind}")

    # ----- what the world may ask ----------------------------------------------------------------------------

    def binding(self) -> dict[str, str]:
        message = binding_message(self.config.netuid, self.config.hotkey, self.address)
        signed = self._account.sign_message(encode_defunct(text=message))
        return {"address": self.address, "message": message, "signature": "0x" + bytes(signed.signature).hex()}

    def attestation(self, challenge: str) -> dict[str, Any]:
        if len(challenge) != 64 or any(c not in "0123456789abcdef" for c in challenge.lower()):
            raise ValueError("the challenge must be 64 hex characters")
        bind = binding_digest(self.config.netuid, self.config.hotkey, self.address, self.config.code_sha256)
        return {
            "token": self.attestor.token([challenge.lower(), bind]),
            "address": self.address,
            "netuid": self.config.netuid,
            "hotkey": self.config.hotkey,
            "code_sha256": self.config.code_sha256,
            "harness_version": HARNESS_VERSION,
        }

    def _check_master(self, message: str, signature: str, time_ms: int) -> None:
        """A request signed by the miner's own address within the window, accepted once (each signed message is its
        own request: a stop and a withdrawal signed in the same millisecond are two)."""
        now = int(time.time() * 1000)
        if abs(now - time_ms) > SIGNED_REQUEST_WINDOW_MS:
            raise PermissionError("stale request: sign a new message")
        try:
            raw = bytes.fromhex(signature.removeprefix("0x"))
            signer = Account.recover_message(encode_defunct(text=message), signature=raw)
        except Exception as exc:  # noqa: BLE001 - a signature that cannot be read is not the miner's
            raise PermissionError("not signed by the miner's address") from exc
        if str(signer).lower() != self.config.master.lower():
            raise PermissionError("not signed by the miner's address")
        with self._signed_lock:  # checked and recorded together: the same request sent twice at once acts once
            for seen, t in list(self._signed.items()):
                if now - t > SIGNED_REQUEST_WINDOW_MS:
                    del self._signed[seen]
            if message in self._signed:
                raise PermissionError("replayed request")
            self._signed[message] = time_ms

    def withdraw(self, amount_usd: str, time_ms: int, signature: str) -> Any:
        message = withdraw_message(self.address, amount_usd, time_ms)
        self._check_master(message, signature, time_ms)
        nonce = self._nonce()
        action = withdraw_action(self.config.master, amount_usd, nonce)
        return self._user(action, WITHDRAW_TYPES, "HyperliquidTransaction:Withdraw", nonce)

    def stop(self, time_ms: int, signature: str) -> Any:
        self._check_master(stop_message(self.address, time_ms), signature, time_ms)
        open_orders = self.info({"type": "openOrders", "user": self.address})
        results = [self.cancel(o["coin"], int(o["oid"])) for o in open_orders]
        if self.on_stop:
            self.on_stop()
        return {"cancelled": len(results)}

    def status(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "netuid": self.config.netuid,
            "hotkey": self.config.hotkey,
            "master": self.config.master.lower(),
            "code_sha256": self.config.code_sha256,
            "harness_version": HARNESS_VERSION,
            "builder_approved": self.builder_approved,
            "network": "mainnet" if self.config.mainnet else "testnet",
            "started_at": self.started_at,
        }


def trigger_fired(is_buy: bool, tpsl: str, trigger_px: float, price: float) -> bool:
    """Whether a trigger order would fire at this price, by the exchange's rule: a stop buys at or above its trigger
    and sells at or below it; a take-profit buys at or below and sells at or above. The harness asks with the mid;
    the exchange triggers on the mark price, which near the trigger can differ by a fraction of a percent."""
    if tpsl == "sl":
        return price >= trigger_px if is_buy else price <= trigger_px
    return price <= trigger_px if is_buy else price >= trigger_px


def withdraw_message(account: str, amount_usd: str, time_ms: int) -> str:
    return f"kokoah-agent-withdraw-1 account={account.lower()} amount={amount_usd} time={time_ms}"


def stop_message(account: str, time_ms: int) -> str:
    return f"kokoah-agent-stop-1 account={account.lower()} time={time_ms}"


class Call(BaseModel):
    method: str
    params: dict[str, Any] = {}


class InfoRequest(BaseModel):
    request: dict[str, Any]


class Signed(BaseModel):
    time_ms: int
    signature: str
    amount_usd: str | None = None


def _guard(fn: Callable[[], Any]) -> Any:
    try:
        return {"ok": True, "result": fn()}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return {"ok": False, "error": str(exc)}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"exchange: {exc}"}


def agent_app(h: Harness) -> FastAPI:
    app = FastAPI(title="Kokoah agent harness (agent side)", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/address")
    def address() -> dict[str, str]:
        return {"address": h.address}

    @app.post("/call")
    def call(body: Call) -> Any:
        return _guard(lambda: h.call(body.method, body.params))

    @app.post("/info")
    def info(body: InfoRequest) -> Any:
        return _guard(lambda: h.agent_info(body.request))

    return app


def public_app(h: Harness) -> FastAPI:
    app = FastAPI(title="Kokoah agent harness", docs_url=None, redoc_url=None)

    @app.get("/status")
    def status() -> dict[str, Any]:
        return h.status()

    @app.get("/binding")
    def binding() -> dict[str, str]:
        return h.binding()

    @app.get("/attestation")
    def attestation(challenge: str) -> Any:
        try:
            return h.attestation(challenge)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/withdraw")
    def withdraw(body: Signed) -> Any:
        if not body.amount_usd:
            raise HTTPException(status_code=400, detail="amount_usd is required")
        amount = body.amount_usd
        return _guard(lambda: h.withdraw(amount, body.time_ms, body.signature))

    @app.post("/stop")
    def stop(body: Signed) -> Any:
        return _guard(lambda: h.stop(body.time_ms, body.signature))

    return app
