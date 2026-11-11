"""Read-only client for Hyperliquid's public info endpoint (what a validator needs, nothing more).

Never signs, never trades, never touches `/exchange`. Every call is a POST of a small JSON body to `/info`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from kokoah_subnet.mechanism.fills import Fill, parse_user_fills

MAINNET_INFO = "https://api.hyperliquid.xyz/info"
MAINNET_EXPLORER = "https://rpc.hyperliquid.xyz/explorer"
PAGE = 2000  # fills per response
FILLS_KEPT = 10_000  # the API keeps the most recent 10,000 fills per address
CANDLES_KEPT = 5000  # candles per coin and interval the API keeps (minute candles: about three and a half days)

# request weights per info type (the exchange budgets 1,200 weight per minute per address; documented values,
# unknown types count 20)
WEIGHTS: dict[str, int] = {
    "metaAndAssetCtxs": 20,
    "clearinghouseState": 2,
    "userFees": 20,
    "userFillsByTime": 20,
    "userNonFundingLedgerUpdates": 20,
    "subAccounts": 2,
    "candleSnapshot": 20,
    "portfolio": 20,
}
# and on top, the exchange charges one more weight per this many items a response returns
ITEMS_PER_WEIGHT: dict[str, int] = {"userFillsByTime": 20, "userFills": 20, "candleSnapshot": 60}


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

    async def charge(self, weight: int) -> None:
        """Count weight the exchange charged after the fact (for the items a response returned), without waiting:
        the requests after it wait for it instead."""
        async with self._lock:
            self._spent.append((time.monotonic(), min(max(0, weight), self._budget)))


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
        explorer_url: str = MAINNET_EXPLORER,
        page_size: int = PAGE,
        backoff_s: float = 0.5,
    ) -> None:
        self._url = url
        self._page = page_size
        self._backoff = backoff_s  # the first retry's wait, doubled on every later one
        self._explorer = explorer_url
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._gap = rate_limit_s
        self._limiter = RateLimiter(budget_per_minute)
        self._sem = asyncio.Semaphore(concurrency)

    async def close(self) -> None:
        await self._client.aclose()

    async def explorer(self, body: Mapping[str, Any], attempts: int = 4) -> Any:
        """The explorer endpoint (`userDetails`: an address's own transactions; `txDetails`: one transaction)."""
        return await self.post(body, attempts, url=self._explorer)

    async def post(self, body: Mapping[str, Any], attempts: int = 4, url: str | None = None) -> Any:
        last: Exception | None = None
        weight = WEIGHTS.get(str(body.get("type")), 20)
        for attempt in range(attempts):
            await self._limiter.acquire(weight)
            async with self._sem:
                try:
                    r = await self._client.post(url or self._url, json=dict(body))
                    if r.status_code == 429 or r.status_code >= 500:
                        raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
                    r.raise_for_status()
                    if self._gap:
                        await asyncio.sleep(self._gap)
                    data = r.json()
                    per = ITEMS_PER_WEIGHT.get(str(body.get("type")))
                    if per and isinstance(data, list) and len(data) >= per:
                        await self._limiter.charge(len(data) // per)
                    return data
                except (httpx.HTTPError, ValueError) as exc:
                    last = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(max(self._gap, self._backoff) * (2**attempt))
        raise RuntimeError(f"info request failed after {attempts} attempts: {last}")

    async def fills_by_time(self, user: str, start_ms: int, end_ms: int, gaps: list[int] | None = None) -> list[Fill]:
        """All fills of `user` in [start_ms, end_ms), paging forward (2,000 per page). A full page can end inside a
        group of fills at one millisecond (one order filling many resting orders), so the next page starts at that
        millisecond again and the fills already read there are skipped. A whole page at one millisecond leaves the
        rest of that millisecond unread: its time is added to `gaps` when given."""
        out: list[Fill] = []
        cursor = start_ms
        seen: set[str] = set()  # the fills already read at the cursor's millisecond
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
            fresh = [r for r in page if int(r["time"]) < end_ms and _fill_key(r) not in seen]
            out.extend(parse_user_fills(user, fresh))
            if len(page) < self._page:
                break
            last = int(page[-1]["time"])
            if last == cursor:
                # a whole page at one millisecond: nothing more can be read there
                if gaps is not None:
                    gaps.append(last)
                cursor, seen = last + 1, set()
                continue
            cursor = last
            seen = {_fill_key(r) for r in page if int(r["time"]) == last}
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

    async def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """Candles for a perp coin (`candleSnapshot`): fields t (open time), T, o, h, l, c, v, n."""
        body = {
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        }
        answer = await self.post(body)
        if not isinstance(answer, list):
            raise RuntimeError(f"no candles for {coin}: {str(answer)[:80]}")
        return [dict(c) for c in answer]

    async def perp_names(self, dex: str = "") -> set[str]:
        """The names of the exchange's perpetuals, delisted ones included (`meta`; a builder-deployed exchange's own
        with `dex`, its names written `dex:COIN`)."""
        body: dict[str, Any] = {"type": "meta", "dex": dex} if dex else {"type": "meta"}
        return {str(asset["name"]) for asset in (await self.post(body))["universe"]}

    async def portfolio(self, user: str) -> list[Any]:
        """The exchange's account-value and PnL history windows for an address (`portfolio`)."""
        return list(await self.post({"type": "portfolio", "user": user}))

    async def mark_prices(self) -> dict[str, float]:
        """Mark price per perp coin, from `metaAndAssetCtxs`."""
        meta, ctxs = await self.post({"type": "metaAndAssetCtxs"})
        return {asset["name"]: float(ctx["markPx"]) for asset, ctx in zip(meta["universe"], ctxs, strict=True)}


def _fill_key(record: Mapping[str, Any]) -> str:
    """One fill record as it was read (two fills always differ in their trade or order ids)."""
    return json.dumps(record, sort_keys=True, default=str)


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


def transfer_usd(delta: Mapping[str, Any]) -> float:
    """The USD value a ledger transfer moved: `usdc` for account transfers, `usdcValue` for a token send, and a
    send's token amount only when the token is USDC (a count of some other token is no USD figure)."""

    def num(key: str) -> float:
        try:
            return abs(float(delta.get(key) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    if delta.get("type") in ("internalTransfer", "subAccountTransfer"):
        return num("usdc")
    if delta.get("usdcValue") not in (None, ""):
        return num("usdcValue")
    return num("amount") if str(delta.get("token", "")).upper().startswith("USDC") else 0.0


async def transfer_partners(client: HyperliquidInfo, user: str, start_ms: int, end_ms: int) -> dict[str, float]:
    """Addresses this address sent funds to or received funds from in the window (ledger transfers), lowercase,
    with the USD moved between them both ways (`transfer_usd`)."""
    updates = await client.post(
        {"type": "userNonFundingLedgerUpdates", "user": user, "startTime": start_ms, "endTime": end_ms}
    )
    me = user.lower()
    partners: dict[str, float] = {}
    for u in updates:
        delta = u.get("delta", {})
        if delta.get("type") in ("internalTransfer", "subAccountTransfer", "send", "receive"):
            moved = transfer_usd(delta)
            for key in ("destination", "user", "fromUser", "toUser"):
                other = str(delta.get(key, "")).lower()
                if other.startswith("0x") and other != me:
                    partners[other] = partners.get(other, 0.0) + moved
    return partners


def linked_reads(subs: set[str], partners: Mapping[str, float], cap: int) -> tuple[list[str], int]:
    """The linked accounts whose trades are read, at most `cap`: the sub-accounts first, then the transfer partners
    by the amount moved, largest first (anyone can send a small transfer, so small ones come last). Also returns how
    many were left out."""
    ordered = sorted(subs) + sorted((a for a in partners if a not in subs), key=lambda a: (-partners[a], a))
    return ordered[:cap], max(0, len(ordered) - cap)


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


def ledger_amount(user: str, delta: Mapping[str, Any]) -> float:
    """The USD a ledger record moves into (+) or out of (-) the address's perps account; 0 for records that do not
    touch it (spot-only moves, staking). Vault withdrawals name the amount `netWithdrawnUsd`."""
    me = user.lower()
    kind = delta.get("type")

    def num(key: str) -> float:
        return float(delta.get(key, 0.0) or 0.0)

    if kind == "deposit":
        return num("usdc")
    if kind == "withdraw":
        return -num("usdc")
    if kind in ("accountClassTransfer", "internalTransfer", "subAccountTransfer"):
        inbound = delta.get("toPerp", True) and str(delta.get("destination", me)).lower() == me
        return num("usdc") if inbound else -num("usdc")
    if kind in ("vaultDeposit", "vaultCreate"):
        return -num("usdc")
    if kind == "vaultWithdraw":
        return num("netWithdrawnUsd")
    if kind == "vaultDistribution":
        return num("usdc")
    if kind == "send" and str(delta.get("token", "")).upper().startswith("USDC"):
        amount = num("usdcValue") or num("amount")
        received = str(delta.get("destination", "")).lower() == me and delta.get("destinationDex", "") == ""
        sent = str(delta.get("user", "")).lower() == me and delta.get("sourceDex", "") == ""
        return (amount if received else 0.0) - (amount if sent else 0.0)
    return 0.0


def flows_from_updates(user: str, updates: list[dict[str, Any]]) -> float:
    """Money moved into minus out of the perps account, USD, from raw ledger records (for the record; the daily
    return uses the exchange's own recorded profit, which needs no ledger reading)."""
    return sum(ledger_amount(user, u.get("delta", {})) for u in updates)


def daily_equity_from_portfolio(
    windows: list[Any], prefer: tuple[str, ...] = ("perpMonth", "month")
) -> list[tuple[str, float, float]]:
    """(day, end-of-day account value, net flows) per UTC day from the portfolio history: the last point of each day
    gives the value; flows are the change in value not explained by the day's PnL. Perps-only windows are preferred."""
    chosen: Mapping[str, Any] | None = None
    for name in prefer:
        for w in windows:
            if isinstance(w, list | tuple) and len(w) == 2 and w[0] == name:
                chosen = w[1]
                break
        if chosen is not None:
            break
    if not chosen:
        return []
    values = {}
    for ts, v in chosen.get("accountValueHistory", []):
        day = datetime.fromtimestamp(int(ts) / 1000, tz=UTC).strftime("%Y-%m-%d")
        values[day] = float(v)
    pnls = {}
    for ts, v in chosen.get("pnlHistory", []):
        day = datetime.fromtimestamp(int(ts) / 1000, tz=UTC).strftime("%Y-%m-%d")
        pnls[day] = float(v)
    out: list[tuple[str, float, float]] = []
    prev_value: float | None = None
    prev_pnl: float | None = None
    for day in sorted(values):
        value = values[day]
        pnl = pnls.get(day)
        flows = 0.0
        if prev_value is not None and pnl is not None and prev_pnl is not None:
            flows = (value - prev_value) - (pnl - prev_pnl)
        out.append((day, value, flows))
        prev_value = value
        if pnl is not None:
            prev_pnl = pnl
    return out


def mark_by_minute(candles: list[dict[str, Any]]) -> dict[int, float]:
    """Minute (ms, floored) -> a mark proxy: the candle's close."""
    out: dict[int, float] = {}
    for c in candles:
        try:
            out[int(c["t"]) // 60_000 * 60_000] = float(c["c"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def day_volume_usd(candles: list[dict[str, Any]]) -> float | None:
    """A coin's traded notional over the candles, USD (each candle's volume at its close); None when the candles
    carry no volume."""
    total, seen = 0.0, False
    for c in candles:
        try:
            total += float(c["v"]) * float(c["c"])
            seen = True
        except (KeyError, TypeError, ValueError):
            continue
    return total if seen else None


def end_of_day_positions(
    state: Mapping[str, Any], later: Iterable[Fill], price_of: Mapping[str, float | None]
) -> dict[str, dict[str, Any]]:
    """Per main perpetual: the position at the day's end, the same whenever a validator reads it. A coin traded since
    the day ended takes the size the exchange records just before its first fill since (`later`, oldest first, up to
    the state's time), or else the state's size less what those fills moved; any other coin keeps the state's size.
    Valued at the coin's last mark of the day (`price_of`; a coin without one is left out). Positive is long.
    Builder-deployed markets (`dex:COIN`) are not in the main state, so they are left out."""
    sizes: dict[str, float] = {}
    for ap in state.get("assetPositions", []):
        pos = ap.get("position", {})
        try:
            sizes[str(pos["coin"])] = float(pos.get("szi", 0.0) or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
    moved: dict[str, float] = {}
    first: dict[str, float | None] = {}
    for f in sorted(later, key=lambda f: f.time_ms):
        if ":" in f.coin:
            continue
        first.setdefault(f.coin, f.start_position)
        moved[f.coin] = moved.get(f.coin, 0.0) + (f.sz if f.side == "B" else -f.sz)
    for coin, delta in moved.items():
        start = first.get(coin)
        sizes[coin] = start if start is not None else sizes.get(coin, 0.0) - delta
    out: dict[str, dict[str, Any]] = {}
    for coin, szi in sorted(sizes.items()):
        px = price_of.get(coin)
        if ":" in coin or abs(szi) < 1e-9 or not px:
            continue
        out[coin] = {"notional": szi * px, "szi": szi, "raw": {"day_end_px": px}}
    return out


def minutes_lost(marks: dict[int, float], start_ms: int, now_ms: int) -> bool:
    """Whether the exchange no longer holds a day's minute candles from its start: it keeps only its most recent
    `CANDLES_KEPT`, so for a day that began before them the answer starts later than the day, or is empty. A day
    within reach is never called lost (a quiet coin can start the day without a candle)."""
    if start_ms >= now_ms - CANDLES_KEPT * 60_000:
        return False
    return not marks or min(marks) > start_ms


def mean_close(candles: list[dict[str, Any]]) -> float:
    closes = [float(c["c"]) for c in candles if "c" in c]
    if not closes:
        raise ValueError("no candles")
    return sum(closes) / len(closes)


def off_market_transfer(px: float, mark: float, sz: float, side: str, clip: float) -> float:
    """Money moved into the account through an off-market price (positive = gain), zero within the clip band.
    Buying below the mark or selling above it is a gift from the counterparty."""
    if mark <= 0 or abs(px / mark - 1) <= clip:
        return 0.0
    return (mark - px) * sz if side == "B" else (px - mark) * sz


def _window(windows: list[Any], name: str) -> Mapping[str, Any] | None:
    for w in windows:
        if isinstance(w, list | tuple) and len(w) == 2 and w[0] == name and isinstance(w[1], Mapping):
            return w[1]
    return None


def portfolio_pnl_points(windows: list[Any], name: str, edge_days: float = 6.0) -> list[tuple[int, float]]:
    """The recorded (time ms, cumulative profit) points of one portfolio window, with the same edge rules as the
    value points: the exchange's own profit, which leaves out deposits, withdrawals, transfers and vault moves."""
    return portfolio_points(windows, name, edge_days, key="pnlHistory")


def portfolio_points(
    windows: list[Any], name: str, edge_days: float = 6.0, key: str = "accountValueHistory"
) -> list[tuple[int, float]]:
    """The recorded (time ms, account value) points of one portfolio window. The window's last point is the live
    value at the moment of the request, and a point at the window's far edge can be made up for the window's
    start; both differ from one request to the next, so both are dropped and only recorded history is returned
    (the same for every validator that asks while the day is inside the window). A young account's first point is
    a real one and is kept."""
    w = _window(windows, name)
    if not w:
        return []
    pts = [(int(t), float(v)) for t, v in w.get(key, [])][:-1]
    if len(pts) > 1 and pts[-1][0] - pts[0][0] > edge_days * 86_400_000:
        pts = pts[1:]
    return pts


def portfolio_origin(windows: list[Any], name: str) -> int | None:
    """When the account's recorded history begins (the first point of the all-time window)."""
    w = _window(windows, name)
    if not w:
        return None
    pts = w.get("accountValueHistory", [])
    return int(pts[0][0]) if pts else None


def portfolio_made_up(windows: list[Any], name: str) -> bool:
    """Whether one portfolio window's history is made up: every point but the live one shows a zero account value
    and a zero profit. The exchange keeps no history for an account that has held nothing and makes one up for each
    request (evenly spaced zero points that end at the moment of the request); until it records an account just
    funded, the window still looks so, with only the live point showing the money. Such points are timed by the
    request and differ from one validator to the next, so none of them is ever stored."""
    w = _window(windows, name)
    if not w:
        return False
    try:
        series = [[(int(t), float(v)) for t, v in w.get(key) or []] for key in ("accountValueHistory", "pnlHistory")]
    except (TypeError, ValueError):
        return False
    return all(len(pts) >= 2 and all(v == 0.0 for _, v in pts[:-1]) for pts in series)


def portfolio_live_holds(windows: list[Any], name: str) -> bool:
    """Whether one portfolio window's live point (its last) shows money: a non-zero account value or profit."""
    w = _window(windows, name)
    if not w:
        return False
    try:
        live = [float(pts[-1][1]) for pts in (w.get("accountValueHistory") or [], w.get("pnlHistory") or []) if pts]
    except (TypeError, ValueError, IndexError):
        return False
    return any(v != 0.0 for v in live)


def portfolio_empty_until(windows: list[Any], name: str, edge_days: float = 6.0) -> int | None:
    """When one portfolio window shows the account held nothing from its far edge until its first recorded point:
    that point's time. The point at the far edge is made up for the window's start but carries the account's value
    there, and the exchange records nothing while an account holds nothing, so a zero far edge followed by recorded
    points means nothing was held before the first of them (an account funded after an empty spell, however old its
    history). None when the window has no far edge (a young account's first point is a real one), shows something
    held at it, or records nothing."""
    w = _window(windows, name)
    if not w or portfolio_made_up(windows, name):
        return None
    try:
        values = [(int(t), float(v)) for t, v in w.get("accountValueHistory") or []]
    except (TypeError, ValueError):
        return None
    recorded = portfolio_points(windows, name, edge_days)
    if len(values) < 3 or not recorded or recorded[0][0] == values[0][0] or values[0][1] != 0.0:
        return None
    return recorded[0][0]


def portfolio_held_nothing(windows: list[Any], name: str, start_ms: int, end_ms: int) -> bool:
    """Whether one portfolio window reaches from before `start_ms` to after `end_ms` and every point of it, the live
    one included, shows a zero account value and a zero profit: the perps account held nothing over the whole window.
    The exchange keeps no history for such an account and makes one up for each request (evenly spaced zero points
    that end at the moment of the request), so its points reach past a day's end only many hours later."""
    w = _window(windows, name)
    if not w:
        return False
    try:
        series = [[(int(t), float(v)) for t, v in w.get(key) or []] for key in ("accountValueHistory", "pnlHistory")]
    except (TypeError, ValueError):
        return False
    return all(
        bool(pts) and pts[0][0] <= start_ms and pts[-1][0] >= end_ms and all(v == 0.0 for _, v in pts) for pts in series
    )
