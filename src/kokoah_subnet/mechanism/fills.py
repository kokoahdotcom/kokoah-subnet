"""Fills: the unit of everything the mechanism counts.

Two public sources feed it. Hyperliquid's per-builder daily file (`builder_fills/{builder}/{YYYYMMDD}.csv.lz4`) is
the canonical list of fills that carry the builder code; the address's own public fill history (`userFillsByTime`)
supplies the fill's total fee field. `match_total_fees` joins the two.
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
        return (self.user, self.coin, self.time_ms, self.px, self.sz, self.side)


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
                side=row[3].strip(),
                px=float(row[4]),
                sz=float(row[5]),
                crossed=_parse_bool(row[6]),
                builder_fee=float(row[13]),
                counterparty=counterparty,
                closed_pnl=float(row[11] or 0.0),
                twap_id=int(twap) if twap else None,
            )
        )
    return fills


def parse_user_fills(user: str, records: Iterable[Mapping[str, Any]]) -> list[Fill]:
    """Parse fills from the public `userFills` / `userFillsByTime` endpoints (fields documented 2026-10-19)."""
    out: list[Fill] = []
    for r in records:
        builder = float(r.get("builderFee") or 0.0)
        out.append(
            Fill(
                time_ms=int(r["time"]),
                user=user.lower(),
                coin=str(r["coin"]),
                side=str(r["side"]),
                px=float(r["px"]),
                sz=float(r["sz"]),
                crossed=bool(r.get("crossed", False)),
                builder_fee=builder,
                closed_pnl=float(r.get("closedPnl") or 0.0),
                total_fee=float(r["fee"]),
            )
        )
    return out


def match_total_fees(builder_fills: Sequence[Fill], user_fills: Sequence[Fill]) -> list[Fill]:
    """Copy the total fee of each matching public fill onto the builder fill (same user, coin, time, price, size,
    side). Builder fills without a match keep `total_fee = None`; the credit code then counts the builder fee only,
    which can only lower the cap."""
    by_key = {f.match_key(): f.total_fee for f in user_fills if f.total_fee is not None}
    return [replace(f, total_fee=by_key.get(f.match_key(), f.total_fee)) for f in builder_fills]


def by_user(fills: Iterable[Fill]) -> dict[str, list[Fill]]:
    out: dict[str, list[Fill]] = {}
    for f in fills:
        out.setdefault(f.user, []).append(f)
    return out
