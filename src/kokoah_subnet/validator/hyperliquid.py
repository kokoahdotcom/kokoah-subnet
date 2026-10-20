"""Read-only client for Hyperliquid's public info endpoint (what a validator needs, nothing more).

Never signs, never trades, never touches `/exchange`. Every call is a POST of a small JSON body to `/info`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from kokoah_subnet.mechanism.fills import Fill, parse_user_fills

MAINNET_INFO = "https://api.hyperliquid.xyz/info"
PAGE = 2000  # fills per response; the API keeps the most recent 10,000 per address


class HyperliquidInfo:
    """Thin async client with retries. `rate_limit_s` spaces requests so a validator stays well inside the
    public limits."""

    def __init__(self, url: str = MAINNET_INFO, timeout_s: float = 30.0, rate_limit_s: float = 0.25) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._gap = rate_limit_s
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def post(self, body: Mapping[str, Any], attempts: int = 4) -> Any:
        last: Exception | None = None
        for attempt in range(attempts):
            async with self._lock:
                try:
                    r = await self._client.post(self._url, json=dict(body))
                    if r.status_code == 429 or r.status_code >= 500:
                        raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
                    r.raise_for_status()
                    await asyncio.sleep(self._gap)
                    return r.json()
                except (httpx.HTTPError, ValueError) as exc:
                    last = exc
                    await asyncio.sleep(self._gap * (2**attempt))
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

    async def net_flows(self, user: str, start_ms: int, end_ms: int) -> float:
        """Deposits minus withdrawals (and account transfers in minus out) in the window, USD."""
        updates = await self.post(
            {"type": "userNonFundingLedgerUpdates", "user": user, "startTime": start_ms, "endTime": end_ms}
        )
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
