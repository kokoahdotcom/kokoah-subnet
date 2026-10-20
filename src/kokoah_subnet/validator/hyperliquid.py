"""Read-only client for Hyperliquid's public info endpoint (what a validator needs, nothing more).

Never signs, never trades, never touches `/exchange`. Every call is a POST of a small JSON body to `/info`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from kokoah_subnet.mechanism.fills import Fill, parse_user_fills

MAINNET_INFO = "https://api.hyperliquid.xyz/info"
PAGE = 2000  # fills per response; the API keeps the most recent 10,000 per address

# request weights per info type (the exchange budgets 1,200 weight per minute per address; documented values,
# unknown types count 20)
WEIGHTS: dict[str, int] = {
    "metaAndAssetCtxs": 20,
    "clearinghouseState": 2,
    "userFees": 20,
    "userFillsByTime": 20,
    "userNonFundingLedgerUpdates": 20,
    "subAccounts": 2,
}


class RateLimiter:
    """A sliding one-minute budget of request weight, shared by concurrent requests."""

    def __init__(self, budget_per_minute: int = 1000) -> None:
        self._budget = budget_per_minute
        self._spent: list[tuple[float, int]] = []
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._spent = [(t, w) for t, w in self._spent if now - t < 60]
                used = sum(w for _, w in self._spent)
                if used + weight <= self._budget:
                    self._spent.append((now, weight))
                    return
                wait = 60 - (now - self._spent[0][0]) + 0.05 if self._spent else 1.0
            await asyncio.sleep(max(0.05, wait))


class HyperliquidInfo:
    """Async client with retries, a weight-aware rate limiter and bounded concurrency, so a validator with hundreds
    of bound traders stays inside the public limits and still finishes quickly."""

    def __init__(
        self,
        url: str = MAINNET_INFO,
        timeout_s: float = 30.0,
        rate_limit_s: float = 0.0,
        budget_per_minute: int = 1000,
        concurrency: int = 6,
    ) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._gap = rate_limit_s
        self._limiter = RateLimiter(budget_per_minute)
        self._sem = asyncio.Semaphore(concurrency)

    async def close(self) -> None:
        await self._client.aclose()

    async def post(self, body: Mapping[str, Any], attempts: int = 4) -> Any:
        last: Exception | None = None
        weight = WEIGHTS.get(str(body.get("type")), 20)
        for attempt in range(attempts):
            await self._limiter.acquire(weight)
            async with self._sem:
                try:
                    r = await self._client.post(self._url, json=dict(body))
                    if r.status_code == 429 or r.status_code >= 500:
                        raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
                    r.raise_for_status()
                    if self._gap:
                        await asyncio.sleep(self._gap)
                    return r.json()
                except (httpx.HTTPError, ValueError) as exc:
                    last = exc
            await asyncio.sleep(max(self._gap, 0.5) * (2**attempt))
        raise RuntimeError(f"info request failed after {attempts} attempts: {last}")

    async def fills_by_time(self, user: str, start_ms: int, end_ms: int) -> list[Fill]:
        """All fills of `user` in [start_ms, end_ms), paging forward (2,000 per page)."""
        out: list[Fill] = []
        cursor = start_ms
        while True:
            page = await self.post(
                {
                    "type": "userFillsByTime",
                    "user": user,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "aggregateByTime": False,
                }
            )
            records = [r for r in page if int(r["time"]) < end_ms]
            out.extend(parse_user_fills(user, records))
            if len(page) < PAGE:
                break
            cursor = int(page[-1]["time"]) + 1
        return out

    async def account_value(self, user: str) -> float:
        state = await self.post({"type": "clearinghouseState", "user": user})
        return float(state["marginSummary"]["accountValue"])

    async def ledger_updates(self, user: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """The raw non-funding ledger records (deposits, withdrawals, transfers) in the window."""
        updates = await self.post(
            {"type": "userNonFundingLedgerUpdates", "user": user, "startTime": start_ms, "endTime": end_ms}
        )
        return [dict(u) for u in updates]

    async def net_flows(self, user: str, start_ms: int, end_ms: int) -> float:
        """Deposits minus withdrawals (and account transfers in minus out) in the window, USD."""
        return flows_from_updates(user, await self.ledger_updates(user, start_ms, end_ms))

    async def fee_rates_raw(self, user: str) -> dict[str, Any]:
        """The address's fee schedule as the exchange reports it."""
        return dict(await self.post({"type": "userFees", "user": user}))

    async def account_state(self, user: str) -> dict[str, Any]:
        """The raw clearinghouse state (margin summary and open positions)."""
        return dict(await self.post({"type": "clearinghouseState", "user": user}))

    async def fee_rates_bps(self, user: str) -> tuple[float, float]:
        """The address's current (taker, maker) perps rates in bps, after volume tiers and discounts."""
        fees = await self.post({"type": "userFees", "user": user})
        return float(fees["userCrossRate"]) * 1e4, float(fees["userAddRate"]) * 1e4

    async def mark_prices(self) -> dict[str, float]:
        """Mark price per perp coin, from `metaAndAssetCtxs`."""
        meta, ctxs = await self.post({"type": "metaAndAssetCtxs"})
        return {asset["name"]: float(ctx["markPx"]) for asset, ctx in zip(meta["universe"], ctxs, strict=True)}


def day_bounds_ms(day: str) -> tuple[int, int]:
    """[start, end) of a UTC day given as YYYY-MM-DD."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    s = int(start.timestamp() * 1000)
    return s, s + 86_400_000


async def sub_accounts(client: HyperliquidInfo, user: str) -> list[str]:
    """Sub-account addresses of a master address (empty for an address without any)."""
    result = await client.post({"type": "subAccounts", "user": user})
    if not result:
        return []
    return [str(s["subAccountUser"]).lower() for s in result if "subAccountUser" in s]


async def transfer_partners(client: HyperliquidInfo, user: str, start_ms: int, end_ms: int) -> set[str]:
    """Addresses this address sent funds to or received funds from in the window (ledger transfers), lowercase."""
    updates = await client.post(
        {"type": "userNonFundingLedgerUpdates", "user": user, "startTime": start_ms, "endTime": end_ms}
    )
    me = user.lower()
    partners: set[str] = set()
    for u in updates:
        delta = u.get("delta", {})
        if delta.get("type") in ("internalTransfer", "subAccountTransfer", "send", "receive"):
            for key in ("destination", "user", "fromUser", "toUser"):
                other = str(delta.get(key, "")).lower()
                if other.startswith("0x") and other != me:
                    partners.add(other)
    return partners


async def positions_notional(client: HyperliquidInfo, user: str) -> dict[str, float]:
    """Signed position notional per perp coin from the clearinghouse state (positive long, negative short)."""
    state = await client.post({"type": "clearinghouseState", "user": user})
    out: dict[str, float] = {}
    for ap in state.get("assetPositions", []):
        pos = ap.get("position", {})
        szi = float(pos.get("szi", 0.0) or 0.0)
        value = float(pos.get("positionValue", 0.0) or 0.0)
        if szi != 0 and value != 0:
            out[str(pos["coin"])] = value if szi > 0 else -value
    return out


def flows_from_updates(user: str, updates: list[dict[str, Any]]) -> float:
    """Deposits minus withdrawals (and account transfers in minus out), USD, from raw ledger records."""
    total = 0.0
    for u in updates:
        delta = u.get("delta", {})
        kind = delta.get("type")
        if kind == "deposit":
            total += float(delta.get("usdc", 0.0))
        elif kind == "withdraw":
            total -= float(delta.get("usdc", 0.0))
        elif kind in ("accountClassTransfer", "internalTransfer", "subAccountTransfer"):
            amount = float(delta.get("usdc", 0.0))
            total += (
                amount
                if delta.get("toPerp", True) and delta.get("destination", user).lower() == user.lower()
                else -amount
            )
    return total


def positions_from_state(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Per perp coin: signed notional (positive long) and the position's details, from a clearinghouse state."""
    out: dict[str, dict[str, Any]] = {}
    for ap in state.get("assetPositions", []):
        pos = ap.get("position", {})
        szi = float(pos.get("szi", 0.0) or 0.0)
        value = float(pos.get("positionValue", 0.0) or 0.0)
        if szi == 0 or value == 0:
            continue
        lev = pos.get("leverage", {})
        out[str(pos["coin"])] = {
            "notional": value if szi > 0 else -value,
            "szi": szi,
            "entry_px": float(pos["entryPx"]) if pos.get("entryPx") else None,
            "unrealized_pnl": float(pos["unrealizedPnl"]) if pos.get("unrealizedPnl") else None,
            "leverage": float(lev.get("value", 0.0)) if isinstance(lev, dict) and lev.get("value") else None,
            "raw": dict(pos),
        }
    return out
