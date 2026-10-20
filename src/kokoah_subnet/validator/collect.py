"""Collect one day's inputs from the public sources and score it (whitepaper appendix B).

Everything fetched is written to the store first, so a day can be re-scored from the store and so the per-address
series (equity, positions, activity) accumulate for the windows the mechanism needs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kokoah_subnet.binding import read_bindings, resolve_bindings
from kokoah_subnet.mechanism import Params, by_user, match_total_fees, parse_builder_csv, round_trips
from kokoah_subnet.mechanism.day import DayInputs, DayResult, MinerInputs, score_day

from .builder_feed import BASE_URL, decompress, download, sha256
from .chain import mean_price_tao, own_stake_min, sample_blocks, snapshot
from .clusters import clusters, coldkey_edges
from .hyperliquid import (
    MAINNET_INFO,
    HyperliquidInfo,
    day_bounds_ms,
    positions_notional,
    sub_accounts,
    transfer_partners,
)
from .run import result_json
from .store import Store

log = logging.getLogger("kokoah_subnet.validator")


@dataclass(frozen=True)
class ValidatorConfig:
    network: str
    netuid: int
    builder: str  # the builder address whose daily file is scored
    data_dir: Path
    wallet_name: str = "validator"
    wallet_hotkey: str = "default"
    wallet_path: str | None = None
    start_day: str = "2026-10-20"  # day index 0
    hl_info_url: str = MAINNET_INFO
    builder_base_url: str = BASE_URL
    tao_coin: str = "TAO"  # the perp whose mark price gives TAO/USD
    tao_usd_fallback: float = 0.0  # used when the perp is not listed; 0 means refuse to score
    poll_s: int = 60
    weights_every_blocks: int = 360  # one tempo
    retry_missing_s: int = 3600
    fills_cache: Path | None = None  # where downloaded builder files are kept (default: data_dir/fills)
    extra: dict[str, str] = field(default_factory=dict)


def day_index(cfg: ValidatorConfig, day: str) -> int:
    d0 = datetime.strptime(cfg.start_day, "%Y-%m-%d").replace(tzinfo=UTC)
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return (d - d0).days


def day_strings(end_day: str, count: int) -> list[str]:
    end = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=UTC)
    return [(end - timedelta(days=count - 1 - i)).strftime("%Y-%m-%d") for i in range(count)]


async def tao_usd_mark(hl: HyperliquidInfo, coin: str, fallback: float) -> float:
    marks = await hl.mark_prices()
    if coin in marks and marks[coin] > 0:
        return marks[coin]
    if fallback > 0:
        return fallback
    raise RuntimeError(f"no TAO/USD mark: {coin} is not listed and no fallback is configured")


async def fetch_builder_file(cfg: ValidatorConfig, day: str) -> bytes:
    """The day's builder file, from the local cache or downloaded (and cached)."""
    cache = cfg.fills_cache or (cfg.data_dir / "fills")
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{day.replace('-', '')}.csv.lz4"
    if path.exists():
        return path.read_bytes()
    data = await download(cfg.builder, day, cfg.builder_base_url)
    path.write_bytes(data)
    return data


async def collect_and_score(
    cfg: ValidatorConfig, day: str, store: Store, hl: HyperliquidInfo, client: Any, params: Params
) -> DayResult:
    """Fetch everything for `day` (a completed UTC day), store it, score it, store the result."""
    data = await fetch_builder_file(cfg, day)
    fills = parse_builder_csv(decompress(data))
    digest = sha256(data)
    store.put_file(day, digest, len(fills))

    snap = await snapshot(client, cfg.netuid)
    published = await read_bindings(client, cfg.netuid)
    resolution = resolve_bindings(store.bindings(), published, snap.block)
    store.put_bindings(resolution.accepted)
    uid_of = snap.uid_of()
    coldkey_of = {n.hotkey: n.coldkey for n in snap.neurons}
    collateral_min = {n.hotkey: n.collateral_min_alpha for n in snap.neurons}

    blocks = sample_blocks(snap.block, snap.tempo, params.price_samples)
    price_tao = await mean_price_tao(client, cfg.netuid, blocks)
    tao_usd = await tao_usd_mark(hl, cfg.tao_coin, cfg.tao_usd_fallback)
    price_usd = price_tao * tao_usd

    start_ms, end_ms = day_bounds_ms(day)
    fills_by_address = by_user(fills)
    address_of_hotkey = {hk: a.address for hk, a in resolution.accepted.items() if hk in uid_of}
    edges = coldkey_edges(address_of_hotkey, coldkey_of)
    miners: list[MinerInputs] = []
    errors: dict[str, str] = {}
    for hk, address in address_of_hotkey.items():
        try:
            public = await hl.fills_by_time(address, start_ms, end_ms)
            own = match_total_fees(fills_by_address.get(address, []), public)
            fills_by_address[address] = own
            equity = await hl.account_value(address)
            flows = await hl.net_flows(address, start_ms, end_ms)
            store.put_equity(address, day, equity, flows)
            store.put_positions(address, day, await positions_notional(hl, address))
            rates = await hl.fee_rates_bps(address)
            store.put_activity(address, day, round_trips(own), len(own), sum(f.notional for f in own))
            rt_window, active_window, _ = store.activity_window(address, params.gate_days)
            window_start = start_ms - params.gate_days * 86_400_000
            partners = set(await sub_accounts(hl, address)) | await transfer_partners(hl, address, window_start, end_ms)
            edges.extend((address, other) for other in partners)
            staked = await own_stake_min(client, cfg.netuid, coldkey_of[hk], hk, blocks)
        except Exception as exc:  # noqa: BLE001 - one address must not sink the day
            log.warning("skipping %s (%s) for %s: %s", hk, address, day, exc)
            errors[hk] = str(exc)[:200]
            continue
        miners.append(
            MinerInputs(
                hotkey=hk,
                address=address,
                returns=store.returns(address, params.confidence_days),
                mean_equity_usd=store.mean_equity(address, params.gate_days),
                staked_min_alpha=staked,
                collateral_min_alpha=collateral_min.get(hk, 0.0),
                exchange_rates_bps=rates,
                round_trips_window=rt_window,
                active_days_window=active_window,
            )
        )
    addresses = list(address_of_hotkey.values())
    inputs = DayInputs(
        day_index=day_index(cfg, day),
        price_usd=price_usd,
        fills=[f for lst in fills_by_address.values() for f in lst],
        miners=miners,
        states=store.states(),
        cluster_of=clusters(addresses, edges),
        positions=store.position_series(addresses, day_strings(day, params.quality_days)),
    )
    result = score_day(inputs, params)
    store.put_states(result.states)
    published_json = json.loads(result_json(result, snap.owner_hotkey))
    published_json.update(
        {
            "day": day,
            "builder_file_sha256": digest,
            "builder_fills": len(fills),
            "price_tao": price_tao,
            "tao_usd": tao_usd,
            "block": snap.block,
            "bindings_refused": resolution.refused,
            "errors": errors,
        }
    )
    store.put_day(day, inputs.day_index, json.dumps(published_json, indent=1, sort_keys=True))
    log.info(
        "scored %s: %d miners, paid %.2f alpha, burned %.2f",
        day,
        len(miners),
        result.allocation.paid_alpha,
        result.allocation.burn_alpha,
    )
    return result
