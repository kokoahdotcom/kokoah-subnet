"""Read-only HTTP view of the validator's store: the published daily artifacts, the bindings, the leaderboard and each
account's record."""

from __future__ import annotations

import json
import re
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

    def record_fields(address: str) -> dict[str, Any]:
        found = store.account_record(address, with_days=False)
        r = found["record"] if found else {}
        keys = ("record_days", "return_90d", "worst_drop_90d", "losing_days_90d", "return_days_90d", "agent_days")
        return {k: r.get(k, 0 if k in ("record_days", "agent_days") else None) for k in keys}

    @app.get("/accounts/{address}")
    def account(address: str) -> dict[str, Any]:
        """One address's whole record: every stored day with its return, the growth curve and the scored figures."""
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            raise HTTPException(status_code=400, detail="bad address")
        found = store.account_record(address)
        if found is None:
            raise HTTPException(status_code=404, detail="this address was never scored")
        return found

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
                    "skill_adjusted": m.get("skill_adjusted", 0.0),
                    "skill_weight": m.get("skill_weight", 0.0),
                    "entry_pass": m.get("entry_pass", False),
                    "agent_verified": m.get("agent_verified", False),
                    "skill_pool_reason": m.get("skill_pool_reason"),
                    "gates_pass": m["gates_pass"],
                    "fills": m["fees"]["fills"],
                    "volume_usd": m["fees"]["notional"],
                    "builder_fee_usd": m["fees"]["builder_fee"],
                    "mean_equity_usd": store.mean_equity(m["address"], 30),
                    "volume_pay_alpha": m.get("volume_pay_alpha", 0.0),
                    "skill_pay_alpha": m.get("skill_pay_alpha", 0.0),
                    "pay_alpha": m["pay_alpha"],
                    "mirrored_share": m["mirrored_share"],
                    "reported": m["reported"],
                    "eligible": m.get("eligible", True),
                    "ineligible_reason": m.get("ineligible_reason"),
                    "avg_equity_usd": m.get("avg_equity_usd"),
                    "qualified": m.get("qualified", True),
                    "qual_trades": m.get("qual_trades", 0),
                    "days": int(t.get("days", 0)),
                    "active_days": int(t.get("active_days", 0)),
                    "total_pay_alpha": t.get("pay_alpha", 0.0),
                    "total_builder_fee_usd": t.get("builder_fee_usd", 0.0),
                    "total_volume_usd": t.get("volume_usd", 0.0),
                    "profit_usd": t.get("profit_usd", 0.0),
                    **record_fields(m["address"]),
                }
            )
        rows.sort(key=lambda r: (r["skill"], r["volume_usd"]), reverse=True)
        return rows

    return app
