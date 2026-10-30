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
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from kokoah_subnet.agent.images import APPROVED_HARNESS_IMAGES
from kokoah_subnet.binding import read_bindings, read_file_commitment, resolve_bindings
from kokoah_subnet.mechanism import (
    SPEC_VERSION,
    Fill,
    Params,
    by_user,
    is_perp,
    parse_builder_csv,
    round_trips,
)
from kokoah_subnet.mechanism.day import DayInputs, DayResult, MinerInputs, score_day
from kokoah_subnet.mechanism.eligibility import daily_average_equity, day_change, fresh_identity
from kokoah_subnet.mechanism.exclusions import bound_counterparty_notional
from kokoah_subnet.mechanism.fills import (
    BUILDER_CSV_HEADER,
    assign_counterparties,
    builder_orders,
    confirm_builder,
    merge_sources,
)

from .agents import check_agents, fetch_unlinks
from .builder_feed import BASE_URL, NotPublished, decompress, download, file_url, sha256
from .chain import day_sample_blocks, mean_price_tao, price_samples, snapshot, stake_samples
from .clusters import clusters, coldkey_edges, penalty_reach
from .hyperliquid import (
    MAINNET_INFO,
    HyperliquidInfo,
    daily_equity_from_portfolio,
    day_bounds_ms,
    ledger_amount,
    linked_reads,
    mark_by_minute,
    mean_close,
    off_market_transfer,
    portfolio_origin,
    portfolio_pnl_points,
    portfolio_points,
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
    burn_hotkey: str = ""  # registered hotkey (own coldkey) that takes a day on which no miner has any weight
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
    fills_cache: Path | None = None  # where downloaded builder files are kept, per builder (default: data_dir/fills)
    db_url: str = ""  # the record; empty = SQLite file in data_dir
    agent_images: tuple[str, ...] = APPROVED_HARNESS_IMAGES  # approved sealed-machine harness images
    # failed reads are waited for until the file deadline, and never less than this long after this validator's first
    # attempt at the day, so a validator that comes back after an outage still retries before it scores without them
    retry_grace_hours: float = 3.0
    agent_service: str = ""  # the owner's agent service (its released list); empty: no account counts as an agent
    extra: dict[str, str] = field(default_factory=dict)

    def store_target(self) -> str | Path:
        return self.db_url or (self.data_dir / "validator.sqlite")


def day_index(cfg: ValidatorConfig, day: str, params: Params | None = None) -> int:
    """Days since day zero. Day zero is the mechanism's (`Params.day_zero`), the same for every validator; a
    validator's `start_day` only says which days it scores."""
    zero = params.day_zero if params is not None else cfg.start_day
    d0 = datetime.strptime(zero, "%Y-%m-%d").replace(tzinfo=UTC)
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return (d - d0).days


def day_strings(end_day: str, count: int) -> list[str]:
    end = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=UTC)
    return [(end - timedelta(days=count - 1 - i)).strftime("%Y-%m-%d") for i in range(count)]


def block_time(now_block: int, block: int, now: datetime | None = None) -> datetime:
    """Estimated time of a past block at 12 s a block."""
    base = now or datetime.now(tz=UTC)
    return base - timedelta(seconds=(now_block - block) * BLOCK_SECONDS)


async def tao_usd_mark(
    hl: HyperliquidInfo, coin: str, fallback: float, start_ms: int | None = None, end_ms: int | None = None
) -> tuple[float, str]:
    """The day's mean TAO/USD from hourly candles of the exchange's TAO perpetual (one instant could be moved; a
    day's mean cannot be moved cheaply); the current mark, then the configured value, as fallbacks."""
    if start_ms is not None and end_ms is not None:
        try:
            return mean_close(await hl.candles(coin, "1h", start_ms, end_ms)), f"hyperliquid:{coin}:1h-mean"
        except Exception as exc:  # noqa: BLE001 - fall through to the spot mark
            log.warning("no TAO candles for the day: %s", exc)
    marks = await hl.mark_prices()
    if coin in marks and marks[coin] > 0:
        return marks[coin], f"hyperliquid:{coin}:mark"
    if fallback > 0:
        return fallback, "fallback"
    raise RuntimeError(f"no TAO/USD mark: {coin} is not listed and no fallback is configured")


def _readable(data: bytes) -> bool:
    """Whether bytes are a whole builder file: they decompress and start with the published header."""
    try:
        first = decompress(data).split("\n", 1)[0]
    except Exception:  # noqa: BLE001 - a truncated or corrupt frame
        return False
    return ",".join(h.strip() for h in first.split(",")) == BUILDER_CSV_HEADER


async def fetch_builder_file(cfg: ValidatorConfig, day: str) -> tuple[bytes, bool]:
    """The day's builder file, from the local cache or downloaded (and cached); the flag says it was downloaded.
    The cache is kept per builder and written whole (a temporary file renamed into place); a cached file that does
    not read as a builder file is downloaded again."""
    cache = (cfg.fills_cache or (cfg.data_dir / "fills")) / cfg.builder.lower()
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{day.replace('-', '')}.csv.lz4"
    if path.exists():
        data = path.read_bytes()
        if _readable(data):
            return data, False
        log.warning("cached builder file %s is unreadable: downloading it again", path)
        path.unlink(missing_ok=True)  # another validator sharing the cache may have replaced it already
    data = await download(cfg.builder, day, cfg.builder_base_url)
    fd, partial = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=cache)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(partial, path)
    return data, True


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
        "source": f.source,
        "tid": f.tid,
        "cloid": f.cloid,
    }


MAX_TX_LOOKUPS = 200  # per address and day: taker fills beyond it are not confirmed one by one


async def confirmed_builder_orders(
    hl: HyperliquidInfo, user: str, builder: str, taker_hashes: set[str], limit: int | None = None
) -> tuple[set[str], set[str], int]:
    """The client order ids and transaction hashes of `user`'s orders that carried `builder`: from the address's
    own transactions, and for taker fills not found there, from the transaction that placed and filled them, at most
    `limit` (default MAX_TX_LOOKUPS) of those a day. Also returns how many were left unlooked-up."""
    details = await hl.explorer({"type": "userDetails", "user": user})
    cloids, hashes = builder_orders(details.get("txs", []) if isinstance(details, dict) else [], builder)
    pending = sorted(taker_hashes - hashes)
    cap = MAX_TX_LOOKUPS if limit is None else limit
    for h in pending[:cap]:
        tx = (await hl.explorer({"type": "txDetails", "hash": h})).get("tx") or {}
        if str(tx.get("user", "")).lower() == user.lower():
            more_cloids, more_hashes = builder_orders([tx], builder)
            cloids |= more_cloids
            hashes |= more_hashes
    return cloids, hashes, max(0, len(pending) - cap)


async def builder_rewards(hl: HyperliquidInfo, builder: str) -> dict[str, float]:
    """What the exchange says the builder has earned (`referral`: builderRewards, claimed, unclaimed), USD."""
    r = await hl.post({"type": "referral", "user": builder})
    return {k: float(r.get(k) or 0.0) for k in ("builderRewards", "claimedRewards", "unclaimedRewards")}


async def collect_and_score(
    cfg: ValidatorConfig,
    day: str,
    store: Store,
    hl: HyperliquidInfo,
    client: Any,
    params: Params,
    now_ts: float | None = None,
    agent_status: dict[str, bool] | None = None,
    agent_keys: Any = None,
    agent_http: Any = None,
) -> DayResult:
    """Fetch everything for `day` (a completed UTC day), store it, score it, store the result."""
    start_ms, end_ms = day_bounds_ms(day)
    clock = now_ts if now_ts is not None else datetime.now(tz=UTC).timestamp()
    first_attempt = store.get_meta(f"first_attempt:{day}")
    if first_attempt is None:
        store.set_meta(f"first_attempt:{day}", repr(clock))
    first_ts = float(first_attempt) if first_attempt is not None else clock
    deadline_ts = max(end_ms / 1000 + params.file_deadline_hours * 3600, first_ts + cfg.retry_grace_hours * 3600)
    past_deadline = clock >= deadline_ts
    try:
        data, downloaded = await fetch_builder_file(cfg, day)
    except NotPublished:
        # the exchange writes a day's file about an hour after the day ends, and writes none when its part of the
        # day had no fills; past the deadline the day is scored from the bound addresses' own histories
        if clock * 1000 < end_ms + params.file_deadline_hours * 3_600_000:
            raise
        data, downloaded = b"", False
    fills = parse_builder_csv(decompress(data)) if data else []
    digest = sha256(data) if data else ""
    if not data:
        log.info("no builder file for %s past the deadline: scoring from the addresses' histories", day)
        store.put_error(day, None, None, "builder file", "not published by the deadline; scored from histories")
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
    # the whole pool goes to miners; only a day with no weight at all goes to the burn hotkey, or to the owner
    # hotkey when no burn hotkey is registered
    fallback_hotkey = cfg.burn_hotkey if cfg.burn_hotkey and cfg.burn_hotkey in uid_of else snap.owner_hotkey
    if fallback_hotkey != cfg.burn_hotkey:
        log.warning("burn hotkey not registered: a day with no miner weight goes to the owner hotkey")
    previous = {n["uid"]: n for n in store.latest_neurons(before_day=day)}
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
    # hotkey swaps: the same UID on the same coldkey and the same registration with a new hotkey keeps its pay
    # history, skill and penalties, never its fee credit or qualification (re-binding required; the swap is recorded
    # and applied when scoring). Compared with the metagraph of the previous scored day; a UID registered again
    # (a new registration block) is a new registration, not a swap
    swaps: list[tuple[str, str]] = []
    store.clear_swaps(day)  # a retried day records only the swaps it sees now
    for n in snap.neurons:
        prev_n = previous.get(n.uid)
        same_registration = (
            not prev_n
            or not prev_n["block_at_registration"]
            or not n.block_at_registration
            or prev_n["block_at_registration"] == n.block_at_registration
        )
        if prev_n and prev_n["hotkey"] != n.hotkey and prev_n["coldkey"] == n.coldkey and same_registration:
            store.put_swap(day, n.uid, prev_n["hotkey"], n.hotkey, n.coldkey)
            swaps.append((prev_n["hotkey"], n.hotkey))
            log.info("hotkey swap on uid %d: %s -> %s", n.uid, prev_n["hotkey"], n.hotkey)
    # unlinks: signed by the linked addresses themselves and published by the subnet's service; each signature is
    # checked here, and every one seen is kept, so a later outage of the service changes nothing already applied
    if cfg.agent_service:
        own_unlink_http = agent_http is None
        unlink_http = agent_http or httpx.AsyncClient(timeout=20)
        try:
            store.put_unlinks(day, await fetch_unlinks(unlink_http, cfg.agent_service, cfg.netuid))
        except Exception as exc:
            store.put_error(day, None, None, "unlinks", str(exc))
            if not past_deadline:
                raise RuntimeError(f"unlinks unavailable: {exc}") from exc
        finally:
            if own_unlink_http:
                await unlink_http.aclose()
    # the accepted bindings and their events are written once the day is scored, so a retried day resolves them
    # from the same bindings again
    before = store.bindings()
    resolution = resolve_bindings(
        before, published, snap.block, registered=uid_of.keys(), unlinks=store.unlinks(), day=day
    )
    events: list[dict[str, Any]] = []
    for hk, acc in resolution.accepted.items():
        old = before.get(hk)
        if old is None:
            events.append({"hotkey": hk, "event": "accepted", "address": acc.address, "reason": None})
        elif old.address != acc.address:
            events.append({"hotkey": hk, "event": "migrated", "address": acc.address, "reason": f"from {old.address}"})
    for hk in before:
        if hk not in uid_of:
            events.append(
                {"hotkey": hk, "event": "left", "address": before[hk].address, "reason": "hotkey no longer registered"}
            )
    for hk, address in resolution.unlinked.items():
        events.append({"hotkey": hk, "event": "unlinked", "address": address, "reason": "the address ended the link"})
    for old_hk, new_hk in swaps:
        events.append(
            {"hotkey": new_hk, "event": "swapped", "address": None, "reason": f"history (not credit) from {old_hk}"}
        )
    for hk, reason in resolution.refused.items():
        events.append(
            {
                "hotkey": hk,
                "event": "refused",
                "address": binding_by_hotkey[hk].binding.address if hk in binding_by_hotkey else None,
                "reason": reason,
            }
        )

    coldkey_of = {n.hotkey: n.coldkey for n in snap.neurons}
    collateral_min = {n.hotkey: n.collateral_min_alpha for n in snap.neurons}
    collateral_locked = {n.hotkey: n.collateral_locked_alpha for n in snap.neurons}
    registered_block = {n.hotkey: n.block_at_registration or None for n in snap.neurons}
    blocks = day_sample_blocks(snap.block, now.timestamp(), start_ms / 1000, snap.tempo, params.price_samples)
    samples = await price_samples(client, cfg.netuid, blocks)
    store.put_price_samples(day, samples)
    price_tao = (
        sum(p for _, p in samples) / len(samples) if samples else await mean_price_tao(client, cfg.netuid, blocks)
    )
    tao_usd, mark_source = await tao_usd_mark(hl, cfg.tao_coin, cfg.tao_usd_fallback, start_ms, end_ms)
    store.put_tao_mark(day, mark_source, tao_usd)
    price_usd = price_tao * tao_usd

    fills_by_address = by_user(fills)
    address_of_hotkey = {hk: a.address for hk, a in resolution.accepted.items() if hk in uid_of}
    # agent accounts: challenge every sealed machine and verify its attestation (the skill bonus)
    if agent_status is None:
        agent_bindings = {
            hk: binding_by_hotkey[hk].binding
            for hk, a in address_of_hotkey.items()
            if hk in binding_by_hotkey and binding_by_hotkey[hk].binding.address == a
        }

        async def extra_agents(address: str) -> Any:
            return await hl.post({"type": "extraAgents", "user": address})

        own_http = agent_http is None
        http_client = agent_http or httpx.AsyncClient(timeout=20)
        try:
            checks = await check_agents(
                agent_bindings,
                cfg.netuid,
                agent_keys,
                cfg.agent_images,
                http_client,
                extra_agents,
                service=cfg.agent_service,
            )
        except Exception as exc:  # noqa: BLE001 - no bonus today rather than no scoring
            log.warning("agent checks failed: %s", exc)
            store.put_error(day, None, None, "agent checks", str(exc))
            checks = []
        finally:
            if own_http:
                await http_client.aclose()
        store.put_agent_checks(day, [c.as_row() for c in checks])
        agent_status = {c.hotkey: c.ok for c in checks}
    hotkey_of_address = {a: hk for hk, a in address_of_hotkey.items()}
    edges = [(a, b, "coldkey") for a, b in coldkey_edges(address_of_hotkey, coldkey_of)]
    miners: list[MinerInputs] = []
    errors: dict[str, str] = {}
    rates_of: dict[str, tuple[float, float]] = {}
    edge_lists: dict[str, list[tuple[str, str, str]]] = {}
    avg_equity: dict[str, tuple[float | None, int]] = {}
    tid_owners: dict[int, set[str]] = {}  # trade id -> addresses with a fill in that trade (bound and linked)
    # addresses whose linked-account reads failed after the deadline: "clusters" (its sub-accounts or transfer
    # partners are unknown) or "linked fills" (a linked account's trades are unknown)
    unchecked: dict[str, str] = {}
    uncounted: list[tuple[Fill, str]] = []  # builder fills that needed a failed read, with the reason recorded
    builder = cfg.builder.lower()

    def record_activity(address: str, fills: list[Fill]) -> tuple[int, int]:
        """Store the day's round trips, fills and notional of the address's perpetual fills; the gate window's
        round trips and active days through the day."""
        perps = [f for f in fills if is_perp(f.coin)]
        store.put_activity(address, day, round_trips(perps), len(perps), sum(f.notional for f in perps))
        rt_window, active_window, _ = store.activity_window(address, params.gate_days, day)
        return rt_window, active_window

    marks_cache: dict[str, dict[int, float] | None] = {}
    marks_locks: dict[str, asyncio.Lock] = {}

    async def marks_for(coin: str) -> dict[int, float] | None:
        """Minute marks for a perp coin over the day: one candle request per coin, shared by every address (the
        first address to ask reads them, the others wait for that answer), so every address is clipped against the
        same marks. The clip is an anti-wash rule, so a read that may succeed later (rate limit, server error, no
        answer) is recorded and fails the day until the deadline; after it, or at once when the exchange refuses the
        coin outright (so no trader can hold everyone's day with a coin that has no candles), None: the coin's
        builder fills go uncounted and the day's return of an address that traded the coin is unknown."""
        async with marks_locks.setdefault(coin, asyncio.Lock()):
            if coin not in marks_cache:
                try:
                    marks_cache[coin] = mark_by_minute(await hl.candles(coin, "1m", start_ms, end_ms))
                except Exception as exc:  # noqa: BLE001
                    store.put_error(day, None, None, "marks", f"minute candles of {coin}: {exc}")
                    if not past_deadline and not getattr(exc, "lasting", False):
                        raise RuntimeError(f"minute candles of {coin} unavailable: {exc}") from exc
                    marks_cache[coin] = None
        return marks_cache[coin]

    async def one(hk: str, address: str) -> None:
        """Everything for one bound address. A failed read of the account's recorded history fails the day (it is
        retried within the hour). A failed read of the fill history, the explorer, the minute candles or a linked
        account fails the day until the deadline; after it, only what needed the failed read goes uncounted. The
        other reads (account state, ledger, fee rates, stake) fall back. Every failure is recorded."""
        own = fills_by_address.get(address, [])
        public: list[Fill] = []
        # the account's recorded history: eligibility needs it every day. A failed read fails the day (retried
        # hourly) until the deadline, and the average is never estimated from a snapshot; after it, this address's
        # average and return are unknown for the day (it is not eligible) and every other address is scored as usual
        try:
            windows = await hl.portfolio(address)
        except Exception as exc:
            store.put_error(day, hk, address, "portfolio", str(exc))
            if not past_deadline:
                raise RuntimeError(f"portfolio history for {address} unavailable: {exc}") from exc
            windows = []
        store.put_equity_points(address, params.equity_window, portfolio_points(windows, params.equity_window), day)
        store.put_equity_points(
            address, params.equity_window + ":pnl", portfolio_pnl_points(windows, params.equity_window), day
        )
        origin = portfolio_origin(windows, params.equity_origin_window)
        avg = daily_average_equity(
            store.equity_points(address, params.equity_window, start_ms - 2 * 86_400_000, end_ms + 86_400_000),
            start_ms,
            end_ms,
            origin,
            params.equity_min_points,
        )
        store.put_equity_daily(address, day, avg.value, avg.points, origin, params.equity_window, avg.note)
        avg_equity[hk] = (avg.value, avg.points)
        if avg.value is None:
            store.put_error(day, hk, address, "average equity", avg.note)
        if not store.equity_series(address, 1):
            # first time this address is scored: the exchange's own history gives the returns from day one
            history = daily_equity_from_portfolio(windows)
            earlier = [(h_day, value, flows) for h_day, value, flows in history if h_day < day]
            for h_day, value, flows in earlier:
                store.put_equity(address, h_day, value, flows, raw={"source": "portfolio"})
            log.info("backfilled %d days of account value for %s", len(earlier), address)
        # the address's own fill history is the primary source of its builder fills (the daily file holds only part
        # of the day). A failed read fails the day (retried hourly) until the deadline; after it, only this address's
        # fills beyond the file go unconfirmed, and every other address is scored as usual
        gaps: list[int] = []
        history_read = True
        try:
            public = await hl.fills_by_time(address, start_ms, end_ms, gaps)
        except Exception as exc:
            store.put_error(day, hk, address, "fill history", str(exc))
            if not past_deadline:
                raise RuntimeError(f"fill history for {address} unavailable: {exc}") from exc
            public = []
            history_read = False  # off-market fills cannot be checked, so the day's return is unknown
        if gaps:
            store.put_note(
                day,
                hk,
                address,
                "fill history",
                f"{len(gaps)} milliseconds with more fills than a page: the rest unread",
            )
        merged = merge_sources(own, public, params.builder_fee_rate)
        candidates = [f for f in merged if f.source == "history"]
        if candidates:
            # the history does not name the builder: each candidate's order must be one the address signed with
            # Kokoah's builder (its own transactions on the explorer)
            takers = {f.hash for f in candidates if f.crossed and f.hash}
            try:
                cloids, hashes, skipped = await confirmed_builder_orders(hl, address, builder, takers)
            except Exception as exc:
                store.put_error(day, hk, address, "builder", f"orders unavailable: {exc}")
                if not past_deadline:
                    raise RuntimeError(f"orders of {address} unavailable: {exc}") from exc
                cloids, hashes, skipped = set(), set(), 0
            merged, refused = confirm_builder(merged, cloids, hashes)
            if refused:
                store.put_note(
                    day, hk, address, "builder", f"{len(refused)} history fills not confirmed as Kokoah orders"
                )
            if skipped:
                store.put_note(day, hk, address, "builder", f"{skipped} taker fills beyond the daily lookup cap")
        history_only = [f for f in merged if f.source == "history"]
        file_only = [f for f in merged if f.source == "file"]
        if file_only:
            store.put_note(
                day, hk, address, "fill history", f"{len(file_only)} file fills not in the history (beyond its reach)"
            )
        log.info(
            "%s: %d builder fills (%d from the history only, %d from the file only)",
            address,
            len(merged),
            len(history_only),
            len(file_only),
        )
        own = merged
        for f in public:
            if f.tid is not None:
                tid_owners.setdefault(f.tid, set()).add(address)
        # marks: the 0.5% clip for credit, and money moved in through off-market fills (removed from the return)
        offmarket = 0.0
        marked: list[Fill] = []
        coins = {f.coin for f in own if is_perp(f.coin)} | {f.coin for f in public if is_perp(f.coin)}
        marks_by_coin = {coin: await marks_for(coin) for coin in coins}
        unmarked = {coin for coin, marks in marks_by_coin.items() if marks is None}
        for f in own:
            if f.coin in unmarked:
                uncounted.append((f, "no_mark"))
                continue
            mark = (marks_by_coin.get(f.coin) or {}).get(f.time_ms // 60_000 * 60_000)
            marked.append(replace(f, mark_px=mark) if mark else f)
        for f in public:
            if not is_perp(f.coin) or f.coin in unmarked:
                continue
            mark = (marks_by_coin.get(f.coin) or {}).get(f.time_ms // 60_000 * 60_000)
            if mark:
                offmarket += off_market_transfer(f.px, mark, f.sz, f.side, params.mark_clip)
        # without a coin's marks the money moved in through its off-market fills is unknown, so is the day's return
        return_known = history_read and not any(f.coin in unmarked for f in public)
        if unmarked:
            store.put_note(
                day,
                hk,
                address,
                "marks",
                f"{len(own) - len(marked)} fills uncounted"
                + ("" if return_known else " and the day's return unknown")
                + f": no minute candles for {', '.join(sorted(unmarked))}",
            )
        fills_by_address[address] = marked
        rates: tuple[float, float] | None = None
        # the day's return from the exchange's own record, the same for every validator whenever it scores; the end
        # of the day is recorded within hours, so until the file deadline a missing end point is waited for
        change = day_change(
            store.equity_points(address, params.equity_window, start_ms - 2 * 86_400_000, end_ms + 86_400_000),
            store.equity_points(address, params.equity_window + ":pnl", start_ms - 2 * 86_400_000, end_ms + 86_400_000),
            start_ms,
            end_ms,
        )
        if change.value_start is not None and change.value_end is None:
            if not past_deadline:
                raise RuntimeError(f"the recorded history of {address} does not reach the end of {day} yet")
        try:
            state = await hl.account_state(address)
            live = float(state["marginSummary"]["accountValue"])
            try:
                updates = await hl.ledger_updates(address, start_ms, end_ms)
            except Exception as exc:  # noqa: BLE001 - the ledger is kept for the record; the return does not use it
                updates = []
                errors[hk] = f"ledger: {str(exc)[:160]}"
                store.put_error(day, hk, address, "ledger", str(exc))
            if change.known and change.value_end is not None and change.profit is not None:
                # the return is the exchange's recorded profit; any other change in value is money moved
                previous = store.equity_value(address, store.day_before(day))
                base = previous if previous is not None else change.value_start or 0.0
                flows = (change.value_end - base) - change.profit
                store.put_equity(
                    address,
                    day,
                    change.value_end,
                    flows,
                    raw=state.get("marginSummary"),
                    offmarket_pnl=offmarket,
                    known=return_known,
                )
            else:
                store.put_equity(
                    address, day, live, 0.0, raw=state.get("marginSummary"), offmarket_pnl=offmarket, known=False
                )
                store.put_error(day, hk, address, "daily return", change.note)
            store.put_ledger_updates(
                address,
                [
                    {
                        "time_ms": int(u.get("time", 0)),
                        "hash": u.get("hash"),
                        "type": (u.get("delta") or {}).get("type"),
                        "usdc": ledger_amount(address, u.get("delta") or {}),
                        "raw": u,
                    }
                    for u in updates
                ],
            )
            store.put_positions(address, day, positions_from_state(state))
        except Exception as exc:  # noqa: BLE001 - no account value today: the return series has a gap, nothing else
            errors[hk] = f"account state: {str(exc)[:160]}"
            store.put_error(day, hk, address, "account state", str(exc))
        try:
            fee_raw = await hl.fee_rates_raw(address)
            rates = (float(fee_raw["userCrossRate"]) * 1e4, float(fee_raw["userAddRate"]) * 1e4)
            rates_of[address] = rates
            store.put_fee_rates(address, day, rates[0], rates[1], raw=fee_raw)
        except Exception as exc:  # noqa: BLE001
            store.put_error(day, hk, address, "fee rates", str(exc))
        rt_window, active_window = record_activity(address, marked)
        # linked accounts (clusters, and the counterparties of history fills): like the history itself, a failed read
        # fails the day until the deadline; after it, the failure is recorded, the other reads go on and the fills
        # that needed the failed read go uncounted (below, once every address is read)
        subs: set[str] = set()
        partners: dict[str, float] = {}
        try:
            subs = set(await sub_accounts(hl, address))
        except Exception as exc:  # noqa: BLE001
            store.put_error(day, hk, address, "clusters", f"sub-accounts: {exc}")
            if not past_deadline:
                raise RuntimeError(f"sub-accounts of {address} unavailable: {exc}") from exc
            unchecked[address] = "clusters"
        try:
            partners = await transfer_partners(hl, address, start_ms - params.gate_days * 86_400_000, end_ms)
        except Exception as exc:  # noqa: BLE001
            store.put_error(day, hk, address, "clusters", f"transfer partners: {exc}")
            if not past_deadline:
                raise RuntimeError(f"transfer partners of {address} unavailable: {exc}") from exc
            unchecked[address] = "clusters"
        edge_lists[address] = [(address, o, "subaccount") for o in sorted(subs)] + [
            (address, o, "transfer") for o in sorted(set(partners) - subs)
        ]
        # the linked accounts' trades of the day name the counterparty of history fills by trade id
        linked, left_out = linked_reads(subs, partners, params.max_linked_accounts)
        if left_out:
            store.put_note(day, hk, address, "clusters", f"{left_out} linked accounts beyond the cap not read")
        for other in linked:
            try:
                other_fills = await hl.fills_by_time(other, start_ms, end_ms)
            except Exception as exc:  # noqa: BLE001
                store.put_error(day, hk, address, "clusters", f"fills of {other}: {exc}")
                if not past_deadline:
                    raise RuntimeError(f"fills of {other}, linked to {address}, unavailable: {exc}") from exc
                unchecked.setdefault(address, "linked fills")
                continue
            for f in other_fills:
                if f.tid is not None:
                    tid_owners.setdefault(f.tid, set()).add(other)
        staked = 0.0
        try:
            stakes = await stake_samples(client, cfg.netuid, coldkey_of[hk], hk, blocks)
            store.put_stake_samples(day, hk, stakes)
            staked = min((v for _, v in stakes), default=0.0)
        except Exception as exc:  # noqa: BLE001 - no stake read: recorded as 0 (stake does not affect pay)
            errors[hk] = f"stake: {str(exc)[:160]}"
            store.put_error(day, hk, address, "stake", str(exc))
        miners.append(
            MinerInputs(
                hotkey=hk,
                address=address,
                returns=store.returns(address, max(params.confidence_days, params.entry_days), day),
                mean_equity_usd=store.mean_equity(address, params.gate_days, day),
                staked_min_alpha=staked,
                collateral_min_alpha=collateral_min.get(hk, 0.0),
                collateral_locked_alpha=collateral_locked.get(hk, 0.0),
                exchange_rates_bps=rates,
                round_trips_window=rt_window,
                active_days_window=active_window,
                avg_equity_usd=avg.value,
                registered_block=registered_block.get(hk),
                credited_notional_prior_usd=store.credited_notional(hk, day, params.skill_volume_days - 1),
                dated_returns=store.dated_returns(address, max(params.confidence_days, params.entry_days), day),
                agent_verified=bool(agent_status.get(hk, False)),
                # the copy rule's longest record: where the account's history on the exchange begins, which every
                # validator reads the same (not the days this validator happens to have scored)
                record_start=(
                    datetime.fromtimestamp(origin / 1000, tz=UTC).strftime("%Y-%m-%d") if origin is not None else day
                ),
            )
        )

    # one address that cannot be read fails the day: the other reads are cancelled with it, so nothing more is
    # fetched or written for a day that is retried anyway
    try:
        async with asyncio.TaskGroup() as group:
            for hk, a in address_of_hotkey.items():
                group.create_task(one(hk, a))
    except ExceptionGroup as failed:
        raise failed.exceptions[0] from None
    miners.sort(key=lambda m: m.hotkey)
    for a in address_of_hotkey.values():
        fills_by_address[a] = assign_counterparties(fills_by_address.get(a, []), tid_owners)
    addresses = list(address_of_hotkey.values())
    bound = set(addresses)
    # a linked read that failed after the deadline: the fills that needed it go uncounted (recorded as such). Without
    # the sub-accounts or transfer partners, no counterparty outside the bound addresses can be told apart from the
    # address's own cluster; without a linked account's trades, a history fill whose counterparty is still unnamed
    # may have traded with that account
    for i, m in enumerate(miners):
        unread = unchecked.get(m.address)
        if unread is None:
            continue
        keep: list[Fill] = []
        for f in fills_by_address.get(m.address, []):
            needs = (
                f.counterparty not in bound if unread == "clusters" else f.source == "history" and not f.counterparty
            )
            if needs and is_perp(f.coin):
                uncounted.append((f, "unchecked_counterparty"))
            else:
                keep.append(f)
        dropped = len(fills_by_address.get(m.address, [])) - len(keep)
        if dropped:
            fills_by_address[m.address] = keep
            store.put_note(
                day,
                m.hotkey,
                m.address,
                "clusters",
                f"{dropped} fills uncounted: their counterparty could not be checked",
            )
            rt_window, active_window = record_activity(m.address, keep)
            miners[i] = replace(m, round_trips_window=rt_window, active_days_window=active_window)
    # what the day's builder fills actually charged (recorded)
    taker_notional = sum(f.notional for lst in fills_by_address.values() for f in lst if f.crossed and is_perp(f.coin))
    taker_fees = sum(f.builder_fee for lst in fills_by_address.values() for f in lst if f.crossed and is_perp(f.coin))
    effective_bps = taker_fees / taker_notional * 1e4 if taker_notional > 0 else None

    # what the exchange says the builder has earned: the fees credited to traders can never exceed it
    try:
        rewards = await builder_rewards(hl, builder)
        store.put_builder_rewards(builder, rewards)
    except Exception as exc:  # noqa: BLE001 - a record and a check, not an input
        store.put_error(day, None, None, "builder rewards", str(exc))
    for lst in edge_lists.values():
        edges.extend(lst)
    cluster_of = clusters(addresses, [(a, b) for a, b, _ in edges])
    store.put_clusters(day, cluster_of, edges)
    all_fills = [f for lst in fills_by_address.values() for f in lst]
    states = store.states_before(day)
    for _, old_hk, new_hk in store.swaps_through(day):
        if old_hk in states and new_hk not in states:
            states[new_hk] = fresh_identity(states.pop(old_hk))
    inputs = DayInputs(
        day_index=day_index(cfg, day, params),
        price_usd=price_usd,
        fills=all_fills,
        miners=miners,
        states=states,
        cluster_of=cluster_of,
        positions=store.position_series(addresses, day_strings(day, params.quality_days)),
        penalized_until=store.penalties(day_index(cfg, day, params), before_day=day),
    )
    result = score_day(inputs, params)
    reports: list[tuple[str, int, str, str]] = []
    for r in result.records:
        if r.reported:
            until = inputs.day_index + params.mirror_zero_days
            reports.append((r.address, until, r.hotkey, f"mirrored {r.mirrored_share:.0%}"))
            for other in sorted(penalty_reach(r.address, edges) - {r.address.lower()}):
                reports.append((other, until, r.hotkey, f"tied to {r.address}"))
    store.put_penalties(day, reports)

    # every fill, with why it did or did not count
    against = bound_counterparty_notional({a: f for a, f in fills_by_address.items() if a in bound})
    rows = []
    tagged = [(f, "") for f in all_fills] + uncounted  # "" = scored, else the reason it went uncounted
    for f, why in sorted(tagged, key=lambda x: (x[0].time_ms, x[0].user, x[0].coin, x[0].side, x[0].px, x[0].sz)):
        fill_hk = hotkey_of_address.get(f.user)
        excluded: str | None
        if why:
            excluded = why
        elif not is_perp(f.coin):
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
                "staked_min_alpha": next((m.staked_min_alpha for m in miners if m.hotkey == r.hotkey), 0.0),
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
                "pay_alpha": r.pay_alpha,
                "volume_pay_alpha": r.volume_pay_alpha,
                "skill_pay_alpha": r.skill_pay_alpha,
                "skill_weight": r.skill_weight,
                "skill_adjusted": r.skill_adjusted,
                "volume_window_usd": r.volume_window_usd,
                "volume_factor": r.volume_factor,
                "entry_psr": r.entry_psr,
                "entry_pass": r.entry_pass,
                "agent_verified": r.agent_verified,
                "copy_group": ",".join(r.copy_group) or None,
                "skill_pool_reason": r.skill_pool_reason,
                "pay_usd": r.pay_alpha * price_usd,
                "exchange_taker_bps": rates_of.get(r.address, (None, None))[0],
                "exchange_maker_bps": rates_of.get(r.address, (None, None))[1],
                "eligible": r.eligible,
                "ineligible_reason": r.ineligible_reason,
                "avg_equity_usd": r.avg_equity_usd,
                "equity_points": avg_equity.get(r.hotkey, (None, 0))[1],
                "qualified": r.qualified,
                "qual_trades": r.qual_trades,
                "qual_notional": r.qual_notional,
                "qualified_at_ms": r.qualified_at_ms,
                "credited_credit": r.credited.credit,
                "credited_notional": r.credited.notional,
                "credited_fills": r.credited.fills,
                "identity_reset": r.identity_reset,
                "registered_block": registered_block.get(r.hotkey),
            }
            for r in result.records
        ],
    )
    store.put_states(result.states, day=day)
    store.put_bindings(resolution.accepted, day=day)
    store.put_binding_events(day, events)

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

    published_json = json.loads(result_json(result, fallback_hotkey))
    published_json.update(
        {
            "day": day,
            "spec_version": SPEC_VERSION,
            "builder_file_sha256": digest,
            "builder_file_hash_matches_operator": hash_matches,
            "builder_fills": len(fills),
            "effective_builder_fee_bps": effective_bps,
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
        burn_hotkey=fallback_hotkey,
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
