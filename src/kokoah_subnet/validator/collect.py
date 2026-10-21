"""Collect one day's inputs from the public sources, score it, and record everything (whitepaper appendix B).

Everything fetched is written to the store first, so a day can be re-scored from the store and so the per-address
series (equity, positions, activity) accumulate for the windows the mechanism needs. The record is deliberately
complete: every fill with why it was or was not counted, every per-miner figure with the parts behind it, every
chain sample, every commitment seen, every refused binding and every error.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kokoah_subnet.binding import read_bindings, read_file_commitment, resolve_bindings
from kokoah_subnet.mechanism import (
    SPEC_VERSION,
    Fill,
    Params,
    by_user,
    is_perp,
    match_total_fees,
    parse_builder_csv,
    round_trips,
)
from kokoah_subnet.mechanism.day import DayInputs, DayResult, MinerInputs, score_day
from kokoah_subnet.mechanism.exclusions import bound_counterparty_notional

from .builder_feed import BASE_URL, decompress, download, file_url, sha256
from .chain import mean_price_tao, price_samples, sample_blocks, snapshot, stake_samples
from .clusters import clusters, coldkey_edges
from .hyperliquid import (
    MAINNET_INFO,
    HyperliquidInfo,
    day_bounds_ms,
    flows_from_updates,
    positions_from_state,
    sub_accounts,
    transfer_partners,
)
from .run import result_json
from .store import Store

log = logging.getLogger("kokoah_subnet.validator")
BLOCK_SECONDS = 12


@dataclass(frozen=True)
class ValidatorConfig:
    network: str
    netuid: int
    builder: str  # the builder address whose daily file is scored
    data_dir: Path
    burn_hotkey: str = ""  # registered hotkey (own coldkey, not the owner's) that receives the unawarded remainder
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
    db_url: str = ""  # the record; empty = SQLite file in data_dir
    extra: dict[str, str] = field(default_factory=dict)

    def store_target(self) -> str | Path:
        return self.db_url or (self.data_dir / "validator.sqlite")


def day_index(cfg: ValidatorConfig, day: str) -> int:
    d0 = datetime.strptime(cfg.start_day, "%Y-%m-%d").replace(tzinfo=UTC)
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return (d - d0).days


def day_strings(end_day: str, count: int) -> list[str]:
    end = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=UTC)
    return [(end - timedelta(days=count - 1 - i)).strftime("%Y-%m-%d") for i in range(count)]


def block_time(now_block: int, block: int, now: datetime | None = None) -> datetime:
    """Estimated time of a past block at 12 s a block."""
    base = now or datetime.now(tz=UTC)
    return base - timedelta(seconds=(now_block - block) * BLOCK_SECONDS)


async def tao_usd_mark(hl: HyperliquidInfo, coin: str, fallback: float) -> tuple[float, str]:
    marks = await hl.mark_prices()
    if coin in marks and marks[coin] > 0:
        return marks[coin], f"hyperliquid:{coin}"
    if fallback > 0:
        return fallback, "fallback"
    raise RuntimeError(f"no TAO/USD mark: {coin} is not listed and no fallback is configured")


async def fetch_builder_file(cfg: ValidatorConfig, day: str) -> tuple[bytes, bool]:
    """The day's builder file, from the local cache or downloaded (and cached); the flag says it was downloaded."""
    cache = cfg.fills_cache or (cfg.data_dir / "fills")
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{day.replace('-', '')}.csv.lz4"
    if path.exists():
        return path.read_bytes(), False
    data = await download(cfg.builder, day, cfg.builder_base_url)
    path.write_bytes(data)
    return data, True


def _paid_window(state: Any) -> float:
    return float(state.paid_window()) if state is not None else 0.0


def _fill_row(f: Fill, hotkey: str | None, excluded: str | None) -> dict[str, Any]:
    return {
        "address": f.user,
        "hotkey": hotkey,
        "time_ms": f.time_ms,
        "coin": f.coin,
        "side": f.side,
        "px": f.px,
        "sz": f.sz,
        "notional": f.notional,
        "crossed": f.crossed,
        "builder_fee": f.builder_fee,
        "total_fee": f.total_fee,
        "counterparty": f.counterparty or None,
        "closed_pnl": f.closed_pnl,
        "twap_id": f.twap_id,
        "mark_px": f.mark_px,
        "excluded": excluded,
    }


async def collect_and_score(
    cfg: ValidatorConfig, day: str, store: Store, hl: HyperliquidInfo, client: Any, params: Params
) -> DayResult:
    """Fetch everything for `day` (a completed UTC day), store it, score it, store the result."""
    data, downloaded = await fetch_builder_file(cfg, day)
    fills = parse_builder_csv(decompress(data))
    digest = sha256(data)
    store.put_file(
        day,
        digest,
        len(fills),
        bytes=len(data),
        users=len({f.user for f in fills}),
        url=file_url(cfg.builder, day, cfg.builder_base_url) if downloaded else None,
    )

    snap = await snapshot(client, cfg.netuid)
    now = datetime.now(tz=UTC)
    uid_of = snap.uid_of()
    if not cfg.burn_hotkey or cfg.burn_hotkey not in uid_of:
        raise RuntimeError("burn_hotkey is not set or not registered on the subnet; register it and configure it")
    store.put_neurons(day, snap.block, [n.as_row() for n in snap.neurons])
    committed = await read_file_commitment(client, cfg.netuid, snap.owner_hotkey)
    hash_matches: bool | None = None if committed is None or committed[0] != day else committed[1] == digest
    if hash_matches is False:
        log.warning("builder file hash for %s differs from the operator's commitment", day)

    # bindings: every commitment seen, what was accepted, what was refused and why
    published = await read_bindings(client, cfg.netuid)
    all_commitments = await client.subnets.commitments(cfg.netuid)
    binding_by_hotkey = {p.hotkey: p for p in published}
    store.put_commitments(
        day,
        [
            {
                "hotkey": hk,
                "block": int(getattr(c, "block", 0) or 0),
                "data": str(getattr(c, "data", ""))[:4000],
                "is_binding": hk in binding_by_hotkey,
                "valid": binding_by_hotkey[hk].valid if hk in binding_by_hotkey else None,
                "address": binding_by_hotkey[hk].binding.address if hk in binding_by_hotkey else None,
                "previous_address": binding_by_hotkey[hk].binding.previous_address if hk in binding_by_hotkey else None,
                "encrypted": bool(getattr(c, "encrypted", False)),
            }
            for hk, c in all_commitments.items()
        ],
    )
    before = store.bindings()
    resolution = resolve_bindings(before, published, snap.block)
    store.put_bindings(resolution.accepted, day=day)
    events: list[dict[str, Any]] = []
    for hk, acc in resolution.accepted.items():
        old = before.get(hk)
        if old is None:
            events.append({"hotkey": hk, "event": "accepted", "address": acc.address, "reason": None})
        elif old.address != acc.address:
            events.append({"hotkey": hk, "event": "migrated", "address": acc.address, "reason": f"from {old.address}"})
    for hk, reason in resolution.refused.items():
        events.append(
            {
                "hotkey": hk,
                "event": "refused",
                "address": binding_by_hotkey[hk].binding.address if hk in binding_by_hotkey else None,
                "reason": reason,
            }
        )
    store.put_binding_events(day, events)

    coldkey_of = {n.hotkey: n.coldkey for n in snap.neurons}
    collateral_min = {n.hotkey: n.collateral_min_alpha for n in snap.neurons}
    blocks = sample_blocks(snap.block, snap.tempo, params.price_samples)
    samples = await price_samples(client, cfg.netuid, blocks)
    store.put_price_samples(day, samples)
    price_tao = (
        sum(p for _, p in samples) / len(samples) if samples else await mean_price_tao(client, cfg.netuid, blocks)
    )
    tao_usd, mark_source = await tao_usd_mark(hl, cfg.tao_coin, cfg.tao_usd_fallback)
    store.put_tao_mark(day, mark_source, tao_usd)
    price_usd = price_tao * tao_usd

    start_ms, end_ms = day_bounds_ms(day)
    fills_by_address = by_user(fills)
    address_of_hotkey = {hk: a.address for hk, a in resolution.accepted.items() if hk in uid_of}
    hotkey_of_address = {a: hk for hk, a in address_of_hotkey.items()}
    edges = [(a, b, "coldkey") for a, b in coldkey_edges(address_of_hotkey, coldkey_of)]
    miners: list[MinerInputs] = []
    errors: dict[str, str] = {}
    rates_of: dict[str, tuple[float, float]] = {}
    edge_lists: dict[str, list[tuple[str, str, str]]] = {}

    async def one(hk: str, address: str) -> None:
        try:
            public = await hl.fills_by_time(address, start_ms, end_ms)
            own = match_total_fees(fills_by_address.get(address, []), public)
            fills_by_address[address] = own
            state = await hl.account_state(address)
            equity = float(state["marginSummary"]["accountValue"])
            updates = await hl.ledger_updates(address, start_ms, end_ms)
            flows = flows_from_updates(address, updates)
            store.put_equity(address, day, equity, flows, raw=state.get("marginSummary"))
            store.put_ledger_updates(
                address,
                [
                    {
                        "time_ms": int(u.get("time", 0)),
                        "hash": u.get("hash"),
                        "type": (u.get("delta") or {}).get("type"),
                        "usdc": float((u.get("delta") or {}).get("usdc", 0.0) or 0.0),
                        "raw": u,
                    }
                    for u in updates
                ],
            )
            store.put_positions(address, day, positions_from_state(state))
            fee_raw = await hl.fee_rates_raw(address)
            rates = (float(fee_raw["userCrossRate"]) * 1e4, float(fee_raw["userAddRate"]) * 1e4)
            rates_of[address] = rates
            store.put_fee_rates(address, day, rates[0], rates[1], raw=fee_raw)
            perps_own = [f for f in own if is_perp(f.coin)]
            store.put_activity(address, day, round_trips(perps_own), len(perps_own), sum(f.notional for f in perps_own))
            rt_window, active_window, _ = store.activity_window(address, params.gate_days)
            window_start = start_ms - params.gate_days * 86_400_000
            subs = set(await sub_accounts(hl, address))
            partners = await transfer_partners(hl, address, window_start, end_ms)
            edge_lists[address] = [(address, o, "subaccount") for o in subs] + [
                (address, o, "transfer") for o in partners - subs
            ]
            stakes = await stake_samples(client, cfg.netuid, coldkey_of[hk], hk, blocks)
            store.put_stake_samples(day, hk, stakes)
            staked = min((v for _, v in stakes), default=0.0)
        except Exception as exc:  # noqa: BLE001 - one address must not sink the day
            log.warning("skipping %s (%s) for %s: %s", hk, address, day, exc)
            errors[hk] = str(exc)[:200]
            store.put_error(day, hk, address, "collect", str(exc))
            return
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

    await asyncio.gather(*(one(hk, a) for hk, a in address_of_hotkey.items()))
    miners.sort(key=lambda m: m.hotkey)
    for lst in edge_lists.values():
        edges.extend(lst)
    addresses = list(address_of_hotkey.values())
    cluster_of = clusters(addresses, [(a, b) for a, b, _ in edges])
    store.put_clusters(day, cluster_of, edges)
    all_fills = [f for lst in fills_by_address.values() for f in lst]
    inputs = DayInputs(
        day_index=day_index(cfg, day),
        price_usd=price_usd,
        fills=all_fills,
        miners=miners,
        states=store.states(),
        cluster_of=cluster_of,
        positions=store.position_series(addresses, day_strings(day, params.quality_days)),
    )
    result = score_day(inputs, params)

    # every fill, with why it did or did not count
    bound = set(addresses)
    against = bound_counterparty_notional({a: f for a, f in fills_by_address.items() if a in bound})
    rows = []
    for f in all_fills:
        fill_hk = hotkey_of_address.get(f.user)
        excluded: str | None
        if not is_perp(f.coin):
            excluded = "spot"
        elif fill_hk is None:
            excluded = "unbound"
        elif f.counterparty and cluster_of.get(f.user, f.user) == cluster_of.get(f.counterparty, f.counterparty):
            excluded = "cluster"
        elif f.counterparty in bound and f.counterparty != f.user:
            excluded = "bound_counterparty"
        elif f.mark_px is not None and f.mark_px > 0 and abs(f.px / f.mark_px - 1) > params.mark_clip:
            excluded = "mark_clip"
        else:
            excluded = None
        rows.append(_fill_row(f, fill_hk, excluded))
    store.put_fills(day, rows)
    del against

    # every per-miner figure with its parts
    store.put_miner_days(
        day,
        [
            {
                "hotkey": r.hotkey,
                "address": r.address,
                "uid": uid_of.get(r.hotkey),
                "credit": r.fees.credit,
                "builder_fee": r.fees.builder_fee,
                "total_fee": r.fees.total_fee,
                "notional": r.fees.notional,
                "taker_notional": r.fees.taker_notional,
                "fills": r.fees.fills,
                "closed_pnl": r.fees.closed_pnl,
                "phi_hat": r.phi_hat,
                "fee_hat": r.fee_hat,
                "confidence": r.confidence,
                "quality": r.quality,
                "skill_raw": r.skill_raw,
                "g": r.g,
                "skill": r.skill,
                "holding": r.holding,
                "staked_min_alpha": next((m.staked_min_alpha for m in miners if m.hotkey == r.hotkey), 0.0),
                "paid_window_alpha": _paid_window(inputs.states.get(r.hotkey)),
                "collateral_min_alpha": collateral_min.get(r.hotkey, 0.0),
                "gates_pass": r.gates_pass,
                "round_trips_window": r.round_trips_window,
                "active_days_window": r.active_days_window,
                "mean_equity_usd": next((m.mean_equity_usd for m in miners if m.hotkey == r.hotkey), 0.0),
                "returns_days": r.returns_days,
                "mirrored_share": r.mirrored_share,
                "counterparty_share": r.counterparty_share,
                "position_share": r.position_share,
                "credit_factor": r.credit_factor,
                "reported": r.reported,
                "zeroed": r.zeroed,
                "multiplier": r.multiplier,
                "share_alpha": r.share_alpha,
                "cap_usd": r.cap_usd,
                "cap_alpha": r.cap_alpha,
                "pay_alpha": r.pay_alpha,
                "pay_usd": r.pay_alpha * price_usd,
                "exchange_taker_bps": rates_of.get(r.address, (None, None))[0],
                "exchange_maker_bps": rates_of.get(r.address, (None, None))[1],
            }
            for r in result.records
        ],
    )
    store.put_states(result.states, day=day)

    # the miners dimension: who is who, when registered, bound, paid
    emission = store.emission_totals()
    miner_rows = []
    for n in snap.neurons:
        role = (
            "owner"
            if n.hotkey == snap.owner_hotkey
            else "burn"
            if n.hotkey == cfg.burn_hotkey
            else ("trader" if n.hotkey in address_of_hotkey else "validator" if n.validator_permit else "unknown")
        )
        bound_to = resolution.accepted.get(n.hotkey)
        pay_total = sum(r.pay_alpha for r in result.records if r.hotkey == n.hotkey)
        total, first_paid, last_paid = emission.get(n.hotkey, (0.0, None, None))
        miner_rows.append(
            {
                "hotkey": n.hotkey,
                "coldkey": n.coldkey,
                "uid": n.uid,
                "block_at_registration": n.block_at_registration or None,
                "registered_at": block_time(snap.block, n.block_at_registration, now)
                if n.block_at_registration
                else None,
                "first_seen_at": now,
                "last_seen_at": now,
                "address": bound_to.address if bound_to else None,
                "bound_block": bound_to.first_block if bound_to else None,
                "bound_at": block_time(snap.block, bound_to.first_block, now) if bound_to else None,
                "first_paid_at": first_paid,
                "last_paid_at": last_paid,
                "total_emission_alpha": total,
                "total_pay_alpha": pay_total if pay_total else None,
                "role": role,
            }
        )
    store.upsert_miners(miner_rows)

    published_json = json.loads(result_json(result, cfg.burn_hotkey))
    published_json.update(
        {
            "day": day,
            "spec_version": SPEC_VERSION,
            "builder_file_sha256": digest,
            "builder_file_hash_matches_operator": hash_matches,
            "builder_fills": len(fills),
            "price_tao": price_tao,
            "tao_usd": tao_usd,
            "tao_usd_source": mark_source,
            "block": snap.block,
            "bindings_refused": resolution.refused,
            "errors": errors,
            "miners_detail": [asdict(r) for r in result.records],
        }
    )
    store.put_day(
        day,
        inputs.day_index,
        json.dumps(published_json, indent=1, sort_keys=True, default=str),
        block=snap.block,
        tempo=snap.tempo,
        price_tao=price_tao,
        tao_usd=tao_usd,
        price_usd=price_usd,
        pool_alpha=result.allocation.pool_alpha,
        paid_alpha=result.allocation.paid_alpha,
        burn_alpha=result.allocation.burn_alpha,
        burn_hotkey=cfg.burn_hotkey,
        miners=len(result.records),
        miners_with_fills=sum(1 for r in result.records if r.fees.fills > 0),
        volume_usd=sum(r.fees.notional for r in result.records),
        builder_fee_usd=sum(r.fees.builder_fee for r in result.records),
        builder_file_sha256=digest,
        builder_fills=len(fills),
        hash_matches_operator=hash_matches,
        spec_version=SPEC_VERSION,
    )
    log.info(
        "scored %s: %d miners, paid %.2f alpha, burned %.2f",
        day,
        len(miners),
        result.allocation.paid_alpha,
        result.allocation.burn_alpha,
    )
    return result
