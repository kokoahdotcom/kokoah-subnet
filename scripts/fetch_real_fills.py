"""Fetch public Hyperliquid fills of real addresses for the replay (`scripts/replay.py --real DIR`).

Run: uv run python scripts/fetch_real_fills.py --out DIR [--days 7] [--top 5] [--address 0x... ...]

Without `--address`, the addresses come from the public leaderboard (the most active by 30-day volume). Only public
info-endpoint reads; nothing is signed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kokoah_subnet.validator.hyperliquid import HyperliquidInfo  # noqa: E402

LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


async def leaderboard_addresses(top: int) -> list[str]:
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.get(LEADERBOARD)
        r.raise_for_status()
        rows = r.json().get("leaderboardRows", [])
    scored = []
    for row in rows:
        vol = 0.0
        for window, perf in row.get("windowPerformances", []):
            if window == "month":
                vol = float(perf.get("vlm", 0.0) or 0.0)
        scored.append((vol, str(row["ethAddress"]).lower()))
    scored.sort(reverse=True)
    return [a for _, a in scored[:top]]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--address", nargs="*", default=[])
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    addresses = [a.lower() for a in args.address] or await leaderboard_addresses(args.top)
    end = int(time.time() * 1000)
    start = end - args.days * 86_400_000
    hl = HyperliquidInfo(rate_limit_s=0.5)
    try:
        for a in addresses:
            # page forward in 2,000-fill steps (the API keeps the most recent 10,000 per address)
            records: list[dict[str, object]] = []
            cursor = start
            while True:
                page = await hl.post({"type": "userFillsByTime", "user": a, "startTime": cursor, "endTime": end})
                records.extend(dict(r) for r in page)
                if len(page) < 2000:
                    break
                cursor = int(page[-1]["time"]) + 1
            (args.out / f"{a}.json").write_text(json.dumps(records))
            print(f"{a}: {len(records)} fills in the last {args.days} days")
    finally:
        await hl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
