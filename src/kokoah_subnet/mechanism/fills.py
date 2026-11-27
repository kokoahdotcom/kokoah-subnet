"""Fills: the unit of everything the mechanism counts.

Two public sources feed it (specification section 3): the address's own fill history (`userFillsByTime`), the
primary source, and Hyperliquid's per-builder daily file (`builder_fills/{builder}/{YYYYMMDD}.csv.lz4`), which names
the builder and each fill's counterparty but may not cover the whole day. `merge_sources` joins the two.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

BUILDER_CSV_HEADER = (
    "time,user,coin,side,px,sz,crossed,special_trade_type,tif,is_trigger,counterparty,closed_pnl,twap_id,builder_fee"
)


@dataclass(frozen=True)
class Fill:
    """One fill. Addresses are lowercase; money is USD; `side` is "B" (buy) or "A" (sell)."""

    time_ms: int
    user: str
    coin: str
    side: str
    px: float
    sz: float
    crossed: bool  # taker
    builder_fee: float  # USD paid to the builder on this fill
    counterparty: str = ""  # lowercase address, "" when unknown
    closed_pnl: float = 0.0
    total_fee: float | None = None  # the fill's total fee field: exchange fee (rebate negative) plus builder fee
    mark_px: float | None = None  # mark price at the fill, when known (for the 0.5% clip)
    twap_id: int | None = None
    tid: int | None = None  # the exchange's trade id, shared by both sides of a trade (history fills only)
    cloid: str | None = None  # the client order id (history fills only)
    source: str = "file"  # "file" (builder file only), "history" (the address's own history only), "both"
    hash: str | None = None  # the transaction that produced the fill (history fills only)
    start_position: float | None = None  # the coin's position size just before the fill (history fills only)

    @property
    def notional(self) -> float:
        return self.px * self.sz

    @property
    def signed_size(self) -> float:
        return self.sz if self.side == "B" else -self.sz

    @property
    def day(self) -> str:
        """UTC day of the fill, YYYY-MM-DD."""
        return datetime.fromtimestamp(self.time_ms / 1000, tz=UTC).strftime("%Y-%m-%d")

    def match_key(self) -> tuple[str, str, int, float, float, str]:
        """The builder file gives whole seconds and the history milliseconds, so fills are matched on the second."""
        return (self.user, self.coin, self.time_ms // 1000, self.px, self.sz, self.side)


def is_perp(coin: str) -> bool:
    """Perpetual markets are plain tickers ("BTC", "kPEPE", a builder-deployed exchange's "xyz:NVDA"); spot pairs are
    "PURR/USDC" or index names like "@107"."""
    return "/" not in coin and not coin.startswith("@")


def counts(coin: str, spot: bool) -> bool:
    """Whether a fill in `coin` counts for the volume pool: a perpetual always; a spot pair once spot counts
    (`Params.spot_counts`: the app's builder code rides on spot sells, where the exchange charges it). The skill pool's
    volume and activity gates read perpetual fills only."""
    return spot or is_perp(coin)


def peak_position_usd(fills: Iterable[Fill]) -> float:
    """The largest total value of the open perpetual positions over a day's own fills (the address's history): each
    coin's position from the fill's start position (else its fills so far), valued at the coin's latest fill price,
    just before and just after every fill. What the day's trades needed in the account (over the leverage), whatever
    it held at the exchange's recorded points; spot holdings are paid in full, so they never count."""
    position: dict[str, float] = {}
    price: dict[str, float] = {}
    peak = 0.0
    for f in sorted(fills, key=lambda x: (x.time_ms, x.tid or 0, x.coin)):
        if not is_perp(f.coin):
            continue
        before = f.start_position if f.start_position is not None else position.get(f.coin, 0.0)
        price[f.coin] = f.px
        for size in (before, before + f.signed_size):
            position[f.coin] = size
            peak = max(peak, sum(abs(p) * price[c] for c, p in position.items()))
    return peak


def position_peaks(fills: Iterable[Fill], start_ms: int, hours: int = 24) -> list[float]:
    """The largest total value of the open perpetual positions in each hour of the day that starts at `start_ms`, over
    the day's own fills (the address's history), as `peak_position_usd` walks them: a position carried into an hour
    counts in it at its coin's latest fill price. A position held from before the day without a fill that day is not
    seen (its margin is in the account at the exchange's recorded points anyway)."""
    peaks = [0.0] * hours
    position: dict[str, float] = {}
    price: dict[str, float] = {}

    def gross() -> float:
        return sum(abs(p) * price[c] for c, p in position.items())

    carried, last = 0.0, 0
    for f in sorted(fills, key=lambda x: (x.time_ms, x.tid or 0, x.coin)):
        if not is_perp(f.coin):
            continue
        h = min(hours - 1, max(0, (f.time_ms - start_ms) // 3_600_000))
        for k in range(last, h + 1):  # the hours since the last fill hold what it left open
            peaks[k] = max(peaks[k], carried)
        before = f.start_position if f.start_position is not None else position.get(f.coin, 0.0)
        price[f.coin] = f.px
        for size in (before, before + f.signed_size):
            position[f.coin] = size
            peaks[h] = max(peaks[h], gross())
        carried, last = gross(), h
    for k in range(last, hours):
        peaks[k] = max(peaks[k], carried)
    return peaks


def _parse_time_ms(text: str) -> int:
    t = text.strip()
    if t.isdigit():
        return int(t)
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _parse_bool(text: str) -> bool:
    return text.strip().lower() in ("true", "1", "t", "yes")


def normalize_side(text: str) -> str:
    """The exchange writes "B"/"A" in the API and "Bid"/"Ask" in the builder files; the mechanism uses "B"/"A"."""
    t = text.strip().lower()
    if t in ("b", "bid", "buy", "long"):
        return "B"
    if t in ("a", "ask", "sell", "s", "short"):
        return "A"
    raise ValueError(f"unknown side {text!r}")


def parse_builder_csv(text: str) -> list[Fill]:
    """Parse a per-builder daily file. The header must match the published format exactly."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError("empty builder fills file") from exc
    if ",".join(h.strip() for h in header) != BUILDER_CSV_HEADER:
        raise ValueError("unexpected builder fills header")
    fills: list[Fill] = []
    for row in reader:
        if len(row) < 14 or not row[1].strip():
            continue
        user = row[1].strip().lower()
        counterparty = row[10].strip().lower()
        twap = row[12].strip()
        fills.append(
            Fill(
                time_ms=_parse_time_ms(row[0]),
                user=user,
                coin=row[2].strip(),
                side=normalize_side(row[3]),
                px=float(row[4]),
                sz=float(row[5]),
                crossed=_parse_bool(row[6]),
                builder_fee=float(row[13]),
                counterparty=counterparty,
                closed_pnl=float(row[11] or 0.0),
                twap_id=int(twap) if twap and int(twap) != 0 else None,
            )
        )
    return fills


def parse_user_fills(user: str, records: Iterable[Mapping[str, Any]]) -> list[Fill]:
    """Parse fills from the public `userFills` / `userFillsByTime` endpoints (fields documented 2026-10-19)."""
    out: list[Fill] = []
    for r in records:
        builder = float(r.get("builderFee") or 0.0)
        tid = r.get("tid")
        cloid = r.get("cloid")
        tx = r.get("hash")
        out.append(
            Fill(
                time_ms=int(r["time"]),
                user=user.lower(),
                coin=str(r["coin"]),
                side=normalize_side(str(r["side"])),
                px=float(r["px"]),
                sz=float(r["sz"]),
                crossed=bool(r.get("crossed", False)),
                builder_fee=builder,
                closed_pnl=float(r.get("closedPnl") or 0.0),
                total_fee=float(r["fee"]),
                twap_id=int(r["twapId"]) if r.get("twapId") else None,
                tid=int(tid) if tid is not None else None,
                cloid=str(cloid).lower() if cloid else None,
                source="history",
                hash=str(tx).lower() if tx else None,
                start_position=float(r["startPosition"]) if r.get("startPosition") not in (None, "") else None,
            )
        )
    return out


def match_total_fees(builder_fills: Sequence[Fill], user_fills: Sequence[Fill]) -> list[Fill]:
    """Copy the total fee of each matching public fill onto the builder fill (same user, coin, second, price, size,
    side; identical rows are matched one to one). Builder fills without a match keep `total_fee = None`; the credit
    code then counts the builder fee only, which can only lower the recorded total fees."""
    pool: dict[tuple[str, str, int, float, float, str], list[float]] = {}
    for f in user_fills:
        if f.total_fee is not None:
            pool.setdefault(f.match_key(), []).append(f.total_fee)
    out = []
    for f in builder_fills:
        fees = pool.get(f.match_key())
        out.append(replace(f, total_fee=fees.pop(0)) if fees else f)
    return out


def history_candidate(f: Fill, rate: float) -> bool:
    """A fill seen only in the address's own history may be a builder fill when it paid a builder fee of at most the
    configured rate. The history does not name the builder, so a candidate counts only once its order is confirmed
    to carry the scored builder (`confirm_builder`)."""
    return f.builder_fee > 0 and f.builder_fee <= f.notional * rate * (1 + 1e-4) + 2e-6


def confirm_builder(
    fills: Sequence[Fill], builder_cloids: set[str], builder_hashes: set[str]
) -> tuple[list[Fill], list[Fill]]:
    """(confirmed, refused): history-only fills whose order the address signed with the scored builder, by client
    order id (from the address's own transactions) or by the transaction that placed and filled it (a taker fill).
    Fills from the file are confirmed by the file itself."""
    keep: list[Fill] = []
    refused: list[Fill] = []
    for f in fills:
        if f.source != "history":
            keep.append(f)
        elif (f.cloid and f.cloid in builder_cloids) or (f.hash and f.hash in builder_hashes):
            keep.append(f)
        else:
            refused.append(f)
    return keep, refused


def builder_orders(transactions: Iterable[Mapping[str, Any]], builder: str) -> tuple[set[str], set[str]]:
    """From an address's own transactions (the explorer's `userDetails`): the client order ids of orders it placed
    with `builder`, and the hashes of those order transactions. A client order id that was later modified is left
    out (a modify carries no builder field), and so is one that any order without `builder` also used: a reused id
    proves nothing about which order a fill came from."""
    b = builder.lower()
    cloids: set[str] = set()
    hashes: set[str] = set()
    modified: set[str] = set()
    elsewhere: set[str] = set()
    for tx in transactions:
        action = tx.get("action") or {}
        kind = action.get("type")
        if tx.get("error"):
            continue
        if kind == "order" and str((action.get("builder") or {}).get("b", "")).lower() == b:
            hashes.add(str(tx.get("hash", "")).lower())
            cloids.update(str(o["c"]).lower() for o in action.get("orders", []) if o.get("c"))
        elif kind == "order":
            elsewhere.update(str(o["c"]).lower() for o in action.get("orders", []) if o.get("c"))
        elif kind in ("modify", "batchModify"):
            mods = action.get("modifies") or [action]
            for m in mods:
                order = m.get("order") or {}
                if order.get("c"):
                    modified.add(str(order["c"]).lower())
                if isinstance(m.get("oid"), str):
                    modified.add(str(m["oid"]).lower())
    return cloids - modified - elsewhere, hashes


def merge_sources(file_fills: Sequence[Fill], history: Sequence[Fill], rate: float, strict: bool = False) -> list[Fill]:
    """One address's builder fills for a day from both sources (specification section 3).

    The exchange's per-builder daily file may not cover the whole day, so the address's own fill history is the
    primary source. A fill in both is taken from the history (exact time, total fee, trade id) with the file's
    counterparty and builder fee; a fill only in the file is kept as it is (beyond the history's 10,000-fill reach); a
    fill only in the history is kept as a candidate when it paid a builder fee within the rate (`history_candidate`),
    to be confirmed by `confirm_builder`.

    `strict` (the days spot fills count, spec 6.9): where the history holds more fills of one match key than the file
    names, which of them carried the builder cannot be told by the key (a twin placed without the builder, or with
    another builder, would take the builder fill's row while the builder fill counts again as a candidate), so the
    file's rows are kept as they are and the history's of that key are dropped: such a fill counts once. And a spot
    pair the file writes otherwise than the history ("@107" or "PURR/USDC": a name the other source never uses) is
    matched without its coin, under the same rule."""
    hist = sorted(history, key=lambda x: (x.time_ms, x.tid or 0))
    files: dict[tuple[str, str, int, float, float, str], list[Fill]] = {}
    for f in file_fills:
        files.setdefault(f.match_key(), []).append(f)
    out: list[Fill] = []
    if not strict:  # the days before: each history fill takes the next file row of its key
        for h in hist:
            matches = files.get(h.match_key())
            if matches:
                fl = matches.pop(0)
                out.append(replace(h, counterparty=fl.counterparty, builder_fee=fl.builder_fee, source="both"))
            elif history_candidate(h, rate):
                out.append(h)
        out.extend(f for fs in files.values() for f in fs)
        return sorted(out, key=lambda x: (x.time_ms, x.coin, x.side, x.px, x.sz))
    used: set[int] = set()  # the file rows taken (by identity)
    left: list[Fill] = []  # history fills without a file row of their own key
    keyed: dict[tuple[str, str, int, float, float, str], list[Fill]] = {}
    for h in hist:
        keyed.setdefault(h.match_key(), []).append(h)
    for key, hs in keyed.items():
        rows = files.get(key, [])
        if not rows:
            left.extend(hs)
        elif len(hs) <= len(rows):
            for h, fl in zip(hs, rows, strict=False):
                used.add(id(fl))
                out.append(replace(h, counterparty=fl.counterparty, builder_fee=fl.builder_fee, source="both"))
        # more in the history than in the file: the file's rows stay, the history's are dropped
    history_coins = {h.coin for h in hist}
    file_coins = {f.coin for f in file_fills}
    loose_rows: dict[tuple[str, int, float, float, str], list[Fill]] = {}
    for f in file_fills:
        if id(f) not in used and not is_perp(f.coin) and f.coin not in history_coins:
            loose_rows.setdefault(_coinless(f), []).append(f)
    loose: dict[tuple[str, int, float, float, str], list[Fill]] = {}
    for h in left:
        if not is_perp(h.coin) and h.coin not in file_coins:
            loose.setdefault(_coinless(h), []).append(h)
    taken: set[int] = set()  # history fills matched or dropped without their coin
    for loose_key, hs in loose.items():
        rows = loose_rows.get(loose_key, [])
        if not rows:
            continue
        taken.update(id(h) for h in hs)
        if len(hs) <= len(rows):
            for h, fl in zip(hs, rows, strict=False):
                used.add(id(fl))
                out.append(replace(h, counterparty=fl.counterparty, builder_fee=fl.builder_fee, source="both"))
    left = [h for h in left if id(h) not in taken]
    out.extend(h for h in left if history_candidate(h, rate))
    out.extend(f for f in file_fills if id(f) not in used)
    return sorted(out, key=lambda x: (x.time_ms, x.coin, x.side, x.px, x.sz))


def _coinless(f: Fill) -> tuple[str, int, float, float, str]:
    """A fill's match key without its coin (for spot pairs, whose names the two sources may write differently)."""
    user, _, second, px, sz, side = f.match_key()
    return user, second, px, sz, side


def assign_counterparties(fills: Sequence[Fill], tid_owners: Mapping[int, set[str]]) -> list[Fill]:
    """Name the counterparty of history fills from the trade id: both sides of a trade carry the same id, so a
    fill whose id also appears among another known address's fills (a bound address or a linked account) traded
    with that address."""
    out = []
    for f in fills:
        if not f.counterparty and f.tid is not None:
            others = sorted(a for a in tid_owners.get(f.tid, set()) if a != f.user)
            if others:
                f = replace(f, counterparty=others[0])
        out.append(f)
    return out


def by_user(fills: Iterable[Fill]) -> dict[str, list[Fill]]:
    out: dict[str, list[Fill]] = {}
    for f in fills:
        out.setdefault(f.user, []).append(f)
    return out
