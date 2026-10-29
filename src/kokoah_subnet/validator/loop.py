"""The validator daemon: score each completed UTC day in order (waiting for its builder file until the deadline), and
keep the weights set.

`step` is one pass and takes its clients as arguments, so it runs against fakes in tests; `run` wires the real ones.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.engine import make_url

from kokoah_subnet import __version__
from kokoah_subnet.mechanism import DEFAULT, SPEC_VERSION, Params

from .builder_feed import NotPublished, download, sha256
from .chain import set_weights, snapshot, subnet_counters, weights_by_uid, weights_rate_limit
from .collect import ValidatorConfig, collect_and_score
from .hyperliquid import HyperliquidInfo
from .store import Store

log = logging.getLogger("kokoah_subnet.validator")


def previous_utc_day(now: float | None = None) -> str:
    t = datetime.fromtimestamp(now if now is not None else time.time(), tz=UTC)
    return (t - timedelta(days=1)).strftime("%Y-%m-%d")


def pending_days(store: Store, start_day: str, last_day: str) -> list[str]:
    """Days from `start_day` to `last_day` not scored yet, oldest first, and never a day before the latest scored
    day. Scoring must stay in order (the smoothing carries from day to day), so the loop takes the first one and
    waits for its file rather than skipping ahead."""
    days = store.days_list()
    scored = set(days)
    if days:
        start_day = max(start_day, Store.day_after(days[-1]))
    d = datetime.strptime(start_day, "%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime.strptime(last_day, "%Y-%m-%d").replace(tzinfo=UTC)
    out = []
    while d <= end:
        s = d.strftime("%Y-%m-%d")
        if s not in scored:
            out.append(s)
        d += timedelta(days=1)
    return out


async def step(
    cfg: ValidatorConfig,
    store: Store,
    hl: HyperliquidInfo,
    client: Any,
    wallet: Any,
    params: Params = DEFAULT,
    now: float | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """One pass: score the oldest pending day if there is one (retrying missing files hourly), then submit the
    latest weights when they are due (`submit_weights`). Returns what happened, for logs and tests."""
    report: dict[str, Any] = {"scored": None, "weights": None}
    clock = now if now is not None else time.time()
    pending = pending_days(store, cfg.start_day, previous_utc_day(now))
    in_order = True
    if day is not None:
        target = day
        if store.day(day) is None and (not pending or day != pending[0]):
            # a day out of order would score against later rows and replace the states the next day starts from
            in_order = False
            oldest = f"the oldest pending day is {pending[0]}" if pending else "no completed day is pending"
            report["scored"] = {"day": day, "error": f"{day} is out of order: {oldest}"}
    else:
        target = pending[0] if pending else previous_utc_day(now)
        if len(pending) > 1:
            report["pending_days"] = pending
    if store.day(target) is None and in_order and target >= cfg.start_day:
        last_try = float(store.get_meta(f"attempt:{target}", "0") or 0)
        if clock - last_try >= cfg.retry_missing_s:
            store.set_meta(f"attempt:{target}", str(clock))
            try:
                result = await collect_and_score(cfg, target, store, hl, client, params, now_ts=clock)
                report["scored"] = {
                    "day": target,
                    "paid": result.allocation.paid_alpha,
                    "burn": result.allocation.burn_alpha,
                }
            except NotPublished as exc:
                log.info("%s", exc)
                report["scored"] = {"day": target, "missing": True}
            except Exception as exc:  # noqa: BLE001 - keep the loop alive, report the failure
                log.exception("scoring %s failed", target)
                report["scored"] = {"day": target, "error": str(exc)[:200]}
    snap = await snapshot(client, cfg.netuid)
    # every tempo: record the metagraph as the chain paid it (emission per UID), and who is who
    step_now = snap.block // max(1, snap.tempo)
    last_epoch = store.last_epoch_block()
    if last_epoch is None or snap.block // max(1, snap.tempo) != last_epoch // max(1, snap.tempo):
        if store.put_epoch(snap.block, step_now, snap.tempo, snap.price_tao, [n.as_row() for n in snap.neurons]):
            report["epoch"] = {"block": snap.block, "uids": len(snap.neurons)}
            try:
                store.put_subnet_epoch(snap.block, **(await subnet_counters(client, cfg.netuid)))
            except Exception as exc:  # noqa: BLE001 - the counters are a record, not an input
                log.warning("subnet counters not recorded: %s", exc)
            emission = store.emission_totals()
            store.upsert_miners(
                [
                    {
                        "hotkey": n.hotkey,
                        "coldkey": n.coldkey,
                        "uid": n.uid,
                        "block_at_registration": n.block_at_registration or None,
                        "last_seen_at": datetime.now(tz=UTC),
                        "first_seen_at": datetime.now(tz=UTC),
                        "first_paid_at": emission.get(n.hotkey, (0.0, None, None))[1],
                        "last_paid_at": emission.get(n.hotkey, (0.0, None, None))[2],
                        "total_emission_alpha": emission.get(n.hotkey, (0.0, None, None))[0],
                    }
                    for n in snap.neurons
                ]
            )
    # later in the day, fetch the builder file again: a file the exchange replaced would be visible as a changed hash,
    # and a file published after a day scored without one gets its hash recorded. Tried at most hourly.
    hour = datetime.fromtimestamp(clock, tz=UTC).hour
    last_recheck = float(store.get_meta(f"recheck:{target}", "0") or 0)
    if (
        hour >= 6
        and store.day(target) is not None
        and store.file_recheck(target) is None
        and clock - last_recheck >= cfg.retry_missing_s
    ):
        store.set_meta(f"recheck:{target}", str(clock))
        try:
            digest = sha256(await download(cfg.builder, target, cfg.builder_base_url))
            store.put_file_recheck(target, digest)
            scored = (store.file(target) or ("", 0))[0]
            if not scored:
                store.put_note(target, None, None, "file recheck", f"published after scoring: {digest}")
            elif scored != digest:
                log.warning("builder file for %s changed after scoring (%s -> %s)", target, scored[:12], digest[:12])
                store.put_error(target, None, None, "file recheck", f"hash changed {scored} -> {digest}")
            report["file_recheck"] = {"day": target, "changed": bool(scored and scored != digest)}
        except Exception as exc:  # noqa: BLE001 - the recheck is a record, not an input
            log.info("file recheck for %s: %s", target, exc)
    # while older days are still pending, a newly scored day does not trigger a submission of its own: the chain
    # gets the weights of the latest day once a tempo, not one submission per day caught up
    caught_up = not pending_days(store, cfg.start_day, previous_utc_day(now))
    report["weights"] = await submit_weights(cfg, store, client, wallet, snap, caught_up)
    return report


async def submit_weights(
    cfg: ValidatorConfig, store: Store, client: Any, wallet: Any, snap: Any, caught_up: bool = True
) -> dict[str, Any] | None:
    """Submit the latest scored day's weights when a tempo has passed since the last accepted submission (or a newer
    day was scored, once no older day is pending). A failed submission is retried a tempo later, not every pass;
    nothing is submitted while the chain's rate limit or the validator's own registration would refuse it."""
    latest = store.latest_day()
    if latest is None:
        return None
    last_block = int(store.get_meta("weights:block", "0") or 0)
    last_day = store.get_meta("weights:day", "")
    due = snap.block - last_block >= cfg.weights_every_blocks or (last_day != latest[0] and caught_up)
    failed_block = int(store.get_meta("weights:failed_block", "0") or 0)
    if not due or (failed_block and snap.block - failed_block < cfg.weights_every_blocks):
        return None
    fractions = json.loads(latest[2])["weights"]
    uids, values = weights_by_uid(fractions, snap.uid_of())
    if not uids:
        return {"day": latest[0], "uids": [], "ok": False, "message": "no registered hotkeys to weight"}
    own_hotkey = getattr(getattr(wallet, "hotkey", None), "ss58_address", None)
    me = next((n for n in snap.neurons if n.hotkey == own_hotkey), None)
    if me is None:
        return {"day": latest[0], "skipped": f"the validator hotkey {own_hotkey} is not registered on the subnet"}
    rate_limit = await weights_rate_limit(client, cfg.netuid)
    if rate_limit is not None:
        store.set_meta("weights:rate_limit", str(rate_limit))
    else:
        rate_limit = int(store.get_meta("weights:rate_limit", "100") or 100)
    if snap.block - me.last_update < rate_limit:
        return {"day": latest[0], "skipped": f"rate limit: set {snap.block - me.last_update} blocks ago"}
    try:
        result = await set_weights(client, wallet, cfg.netuid, uids, values)
        ok, message = bool(result.success), str(result.message)
    except Exception as exc:  # noqa: BLE001 - a refused submission is recorded and retried a tempo later
        ok, message = False, str(exc)[:500]
    store.put_weights_submission(latest[0], snap.block, uids, values, ok, message)
    if ok:
        store.set_meta("weights:block", str(snap.block))
        store.set_meta("weights:day", latest[0])
        store.set_meta("weights:failed_block", "0")
    else:
        store.set_meta("weights:failed_block", str(snap.block))
    return {"day": latest[0], "uids": uids, "ok": ok, "message": message}


def run_config(cfg: ValidatorConfig) -> dict[str, str]:
    """The settings kept in the run record (`runs.config`), with the database password hidden."""
    out = {k: str(v) for k, v in cfg.__dict__.items() if k != "extra"}
    if cfg.db_url:
        try:
            out["db_url"] = make_url(cfg.db_url).render_as_string(hide_password=True)
        except Exception:  # noqa: BLE001 - not a URL SQLAlchemy reads: keep none of it
            out["db_url"] = "(hidden)"
    return out


async def run(cfg: ValidatorConfig, params: Params = DEFAULT, once: bool = False, day: str | None = None) -> None:
    import bittensor as bt

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = Store(cfg.store_target())
    hl = HyperliquidInfo(cfg.hl_info_url)
    wallet = (
        bt.Wallet(name=cfg.wallet_name, hotkey=cfg.wallet_hotkey, path=cfg.wallet_path)
        if cfg.wallet_path
        else bt.Wallet(name=cfg.wallet_name, hotkey=cfg.wallet_hotkey)
    )
    started = False
    try:
        while True:
            try:
                async with bt.Subtensor(cfg.network) as client:
                    if not started:
                        store.start_run(
                            network=cfg.network,
                            netuid=cfg.netuid,
                            validator_hotkey=wallet.hotkey.ss58_address,
                            builder=cfg.builder,
                            burn_hotkey=cfg.burn_hotkey,
                            spec_version=SPEC_VERSION,
                            code_version=__version__,
                            config=run_config(cfg),
                        )
                        started = True
                    while True:
                        report = await step(cfg, store, hl, client, wallet, params, day=day)
                        log.info("step: %s", json.dumps(report, default=str))
                        if once:
                            return
                        await asyncio.sleep(cfg.poll_s)
            except Exception as exc:
                # a chain error (a dropped connection, a failed read) ends the pass, not the validator: it is
                # recorded, the connection is opened again and the loop goes on
                if once:
                    raise
                log.exception("validator pass failed; reconnecting")
                store.put_error(None, None, None, "loop", str(exc))
                await asyncio.sleep(cfg.poll_s)
    finally:
        await hl.close()
        store.close()
