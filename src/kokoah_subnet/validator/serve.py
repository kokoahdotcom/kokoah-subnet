"""Read-only HTTP view of the validator's store: the published daily artifacts, the bindings, the leaderboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .store import Store

PAGE = Path(__file__).with_name("leaderboard.html")


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="kokoah-subnet validator", docs_url=None, redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return PAGE.read_text(encoding="utf-8")

    @app.get("/summary")
    def summary() -> dict[str, Any]:
        latest = store.latest_day()
        return {"latest_day": latest[0] if latest else None, **store.subnet_totals()}

    @app.get("/health")
    def health() -> dict[str, Any]:
        latest = store.latest_day()
        return {"ok": True, "latest_day": latest[0] if latest else None}

    @app.get("/days")
    def days() -> list[str]:
        return store.days_list()

    @app.get("/days/latest")
    def latest() -> Any:
        latest = store.latest_day()
        if latest is None:
            raise HTTPException(404, "no day scored yet")
        return json.loads(latest[2])

    @app.get("/days/{day}")
    def day(day: str) -> Any:
        text = store.day(day)
        if text is None:
            raise HTTPException(404, f"{day} not scored")
        return json.loads(text)

    @app.get("/bindings")
    def bindings() -> list[dict[str, Any]]:
        return [
            {"hotkey": b.hotkey, "address": b.address, "accepted_block": b.accepted_block, "first_block": b.first_block}
            for b in store.bindings().values()
        ]

    @app.get("/leaderboard")
    def leaderboard() -> list[dict[str, Any]]:
        """Miners of the latest day sorted by skill, with the published metrics (no effect on pay) and their
        totals over every stored day."""
        latest = store.latest_day()
        if latest is None:
            return []
        totals = store.miner_totals()
        rows = []
        for m in json.loads(latest[2])["miners"]:
            t = totals.get(m["hotkey"], {})
            rows.append(
                {
                    "hotkey": m["hotkey"],
                    "address": m["address"],
                    "skill": m["skill"],
                    "skill_raw": m["skill_raw"],
                    "holding": m["holding"],
                    "multiplier": m["multiplier"],
                    "gates_pass": m["gates_pass"],
                    "fills": m["fees"]["fills"],
                    "volume_usd": m["fees"]["notional"],
                    "builder_fee_usd": m["fees"]["builder_fee"],
                    "mean_equity_usd": store.mean_equity(m["address"], 30),
                    "share_alpha": m["share_alpha"],
                    "cap_alpha": m["cap_alpha"],
                    "pay_alpha": m["pay_alpha"],
                    "cap_binds": m["pay_alpha"] > 0 and m["pay_alpha"] < m["share_alpha"] - 1e-9,
                    "mirrored_share": m["mirrored_share"],
                    "reported": m["reported"],
                    "days": int(t.get("days", 0)),
                    "active_days": int(t.get("active_days", 0)),
                    "total_pay_alpha": t.get("pay_alpha", 0.0),
                    "total_builder_fee_usd": t.get("builder_fee_usd", 0.0),
                    "total_volume_usd": t.get("volume_usd", 0.0),
                    "profit_usd": t.get("profit_usd", 0.0),
                }
            )
        rows.sort(key=lambda r: (r["skill"], r["volume_usd"]), reverse=True)
        return rows

    return app
