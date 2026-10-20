"""Read-only HTTP view of the validator's store: the published daily artifacts, the bindings, the leaderboard."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException

from .store import Store


def create_app(store: Store) -> FastAPI:
    app = FastAPI(title="kokoah-subnet validator", docs_url=None, redoc_url=None)

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
        """Miners of the latest day sorted by skill, with the published metrics (no effect on pay)."""
        latest = store.latest_day()
        if latest is None:
            return []
        miners = json.loads(latest[2])["miners"]
        rows = []
        for m in miners:
            rows.append(
                {
                    "hotkey": m["hotkey"],
                    "address": m["address"],
                    "skill": m["skill"],
                    "skill_raw": m["skill_raw"],
                    "holding": m["holding"],
                    "multiplier": m["multiplier"],
                    "volume_usd": m["fees"]["notional"],
                    "builder_fee_usd": m["fees"]["builder_fee"],
                    "mean_equity_usd": store.mean_equity(m["address"], 30),
                    "pay_alpha": m["pay_alpha"],
                    "mirrored_share": m["mirrored_share"],
                    "reported": m["reported"],
                }
            )
        rows.sort(key=lambda r: (r["skill"], r["volume_usd"]), reverse=True)
        return rows

    return app
