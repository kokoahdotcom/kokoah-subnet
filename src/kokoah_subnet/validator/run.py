"""Assemble one day's inputs from files and run the mechanism. The network fetches live in `hyperliquid.py`,
`builder_feed.py` and `chain.py`; this module only wires what they produced into `score_day`."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kokoah_subnet.mechanism import Fill, MinerState, Params
from kokoah_subnet.mechanism.day import DayInputs, DayResult, MinerInputs, score_day


def load_states(path: Path | None) -> dict[str, MinerState]:
    if path is None or not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {hk: MinerState(**{**v, "paid": tuple(v.get("paid", ()))}) for hk, v in raw.items()}


def dump_states(states: dict[str, MinerState]) -> str:
    return json.dumps({hk: asdict(s) for hk, s in states.items()}, indent=1, sort_keys=True)


def load_miners(path: Path, fills_window: dict[str, list[Fill]] | None = None) -> list[MinerInputs]:
    """Miner inputs from a JSON list: hotkey, address, returns, mean_equity_usd, staked_min_alpha,
    collateral_min_alpha, start_positions, exchange_rates_bps."""
    raw: list[dict[str, Any]] = json.loads(path.read_text())
    out = []
    for m in raw:
        address = str(m["address"]).lower()
        rates = m.get("exchange_rates_bps")
        out.append(
            MinerInputs(
                hotkey=str(m["hotkey"]),
                address=address,
                returns=[float(r) for r in m.get("returns", [])],
                mean_equity_usd=float(m.get("mean_equity_usd", 0.0)),
                staked_min_alpha=float(m.get("staked_min_alpha", 0.0)),
                collateral_min_alpha=float(m.get("collateral_min_alpha", 0.0)),
                window_fills=(fills_window or {}).get(address, []),
                start_positions={str(k): float(v) for k, v in m.get("start_positions", {}).items()},
                exchange_rates_bps=(float(rates[0]), float(rates[1])) if rates else None,
            )
        )
    return out


def run_day(
    day_index: int,
    price_usd: float,
    fills: list[Fill],
    miners: list[MinerInputs],
    states: dict[str, MinerState],
    params: Params,
    cluster_of: dict[str, str] | None = None,
    positions: dict[str, dict[str, list[float]]] | None = None,
) -> DayResult:
    inputs = DayInputs(
        day_index=day_index,
        price_usd=price_usd,
        fills=fills,
        miners=miners,
        states=states,
        cluster_of=cluster_of or {},
        positions=positions or {},
    )
    return score_day(inputs, params)


def result_json(result: DayResult, owner_hotkey: str) -> str:
    alloc = result.allocation
    return json.dumps(
        {
            "day_index": result.day_index,
            "price_usd": alloc.price_usd,
            "pool_alpha": alloc.pool_alpha,
            "paid_alpha": alloc.paid_alpha,
            "burn_alpha": alloc.burn_alpha,
            "weights": result.weights(owner_hotkey),
            "miners": [asdict(r) for r in result.records],
        },
        indent=1,
        sort_keys=True,
    )
