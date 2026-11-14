"""The validator daemon: score each completed UTC day in order (waiting for its builder file until the deadline), and
keep the weights set.

`step` is one pass and takes its clients as arguments, so it runs against fakes in tests; `run` wires the real ones.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.engine import make_url

from kokoah_subnet import __version__
from kokoah_subnet.chain import NETWORKS
from kokoah_subnet.mechanism import DEFAULT, SPEC_VERSION, Params

from .builder_feed import NotPublished, download, sha256
from .chain import BLOCK_SECONDS, set_weights, snapshot, subnet_counters, weights_by_uid, weights_rate_limit
from .collect import ValidatorConfig, collect_and_score
from .hyperliquid import HyperliquidInfo
from .store import Store

log = logging.getLogger("kokoah_subnet.validator")


def previous_utc_day(now: float | None = None) -> str:
    t = datetime.fromtimestamp(now if now is not None else time.time(), tz=UTC)
    return (t - timedelta(days=1)).strftime("%Y-%m-%d")


def start_day_for(store: Store, given: str, now: float | None = None) -> str:
    """The first day this validator scores: the one given, else the one its record remembers, else yesterday (UTC).
    Remembered, so a restart without the setting goes on from the same day."""
    day = given or store.get_meta("start_day") or previous_utc_day(now)
    if store.get_meta("start_day") != day:
        store.set_meta("start_day", day)
    return day


def other_subnet(previous: tuple[str, int] | None, network: str, netuid: int) -> str | None:
    """Why a record cannot serve this subnet, or None. A record belongs to one subnet: its days, bindings and states
    mean nothing on another. Another netuid, or another named network, is refused; an endpoint URL cannot be told
    apart from a named network, so it is let through."""
    if previous is None:
        return None
    was_network, was_netuid = previous
    named = was_network in NETWORKS and network in NETWORKS
    if was_netuid == netuid and not (named and was_network != network):
        return None
    return (
        f"this record is for {was_network} netuid {was_netuid}, not {network} netuid {netuid}: "
        "give this subnet a fresh database (--db-url) or data directory"
    )


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


def scoring_target(
    cfg: ValidatorConfig, store: Store, now: float | None = None, day: str | None = None
) -> tuple[str, dict[str, Any]]:
    """The day a pass looks at (the given day, else the oldest pending one, else yesterday) and what to report about
    it before scoring: the pending days of a backlog, or why a given day is out of order."""
    report: dict[str, Any] = {}
    pending = pending_days(store, cfg.start_day, previous_utc_day(now))
    if day is not None:
        if store.day(day) is None and (not pending or day != pending[0]):
            # a day out of order would score against later rows and replace the states the next day starts from
            oldest = f"the oldest pending day is {pending[0]}" if pending else "no completed day is pending"
            report["scored"] = {"day": day, "error": f"{day} is out of order: {oldest}"}
        return day, report
    if len(pending) > 1:
        report["pending_days"] = pending
    return (pending[0] if pending else previous_utc_day(now)), report


def scoring_due(cfg: ValidatorConfig, store: Store, now: float | None = None, day: str | None = None) -> bool:
    """Whether a pass would score now: the day is unscored, in order, not before the start day, and its last attempt
    is at least `retry_missing_s` ago. Asked before any connection is opened for it."""
    clock = now if now is not None else time.time()
    target, report = scoring_target(cfg, store, now, day)
    if store.day(target) is not None or "scored" in report or target < cfg.start_day:
        return False
    return clock - float(store.get_meta(f"attempt:{target}", "0") or 0) >= cfg.retry_missing_s


async def score_step(
    cfg: ValidatorConfig,
    store: Store,
    hl: HyperliquidInfo,
    client: Any,
    params: Params = DEFAULT,
    now: float | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """Score the oldest pending day if there is one (retrying missing files hourly). Returns the report's scoring
    part: `scored` (what happened, or None), and `pending_days` while there is a backlog."""
    clock = now if now is not None else time.time()
    target, report = scoring_target(cfg, store, now, day)
    report.setdefault("scored", None)
    if scoring_due(cfg, store, now, day):
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
    return report


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
    """One pass in order: score the oldest pending day if there is one (`score_step`), then the chain's part
    (`chain_step`): the tempo's record, the file re-check and the weights. Returns what happened, for logs and
    tests. The running validator does the two side by side instead (`run`), so a long scoring pass never holds up
    the weights."""
    target, _ = scoring_target(cfg, store, now, day)
    report: dict[str, Any] = {"scored": None, "weights": None}
    report.update(await score_step(cfg, store, hl, client, params, now, day))
    report.update(await chain_step(cfg, store, client, wallet, target, now))
    return report


async def chain_step(
    cfg: ValidatorConfig, store: Store, client: Any, wallet: Any, target: str, now: float | None = None
) -> dict[str, Any]:
    """The chain's part of a pass: record the metagraph once a tempo, re-check `target`'s builder file later in the
    day, and submit the latest weights when they are due (`submit_weights`). Returns the report's `weights` (and
    `epoch`, `file_recheck` when they happened)."""
    report: dict[str, Any] = {"weights": None}
    clock = now if now is not None else time.time()
    snap = await snapshot(client, cfg.netuid)
    # every epoch: record the metagraph as the chain paid it (emission per UID), and who is who. With the chain's own
    # epoch block, once per epoch it ran (when no read since that block is recorded: epochs recorded by a release
    # without it count too), dated by that block (12 s a block); without it, once per tempo window
    if snap.last_step is not None:
        step_now = snap.last_step
        last_read = store.last_epoch_block()
        due = last_read is None or last_read < step_now
        ran_at = datetime.fromtimestamp(clock - (snap.block - snap.last_step) * BLOCK_SECONDS, UTC)
    else:
        step_now = snap.block // max(1, snap.tempo)
        last_epoch = store.last_epoch_block()
        due = last_epoch is None or step_now != last_epoch // max(1, snap.tempo)
        ran_at = None
    if due:
        rows = [n.as_row() for n in snap.neurons]
        if store.put_epoch(snap.block, step_now, snap.tempo, snap.price_tao, rows, at=ran_at):
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
    if cfg.watch:
        report["weights"] = {"skipped": "watch mode: weights are never submitted"}
    else:
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
    # a block ahead of the chain (a reset local chain, or a record moved to another network) counts as long ago
    since_last = snap.block - last_block
    due = not 0 <= since_last < cfg.weights_every_blocks or (last_day != latest[0] and caught_up)
    failed_block = int(store.get_meta("weights:failed_block", "0") or 0)
    if not due or (failed_block and 0 <= snap.block - failed_block < cfg.weights_every_blocks):
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


class BackgroundScoring:
    """Scoring passes run one at a time beside the validator's loop: each `poll` starts a pass when none is running
    and hands back a finished one's report, so the loop records the tempo and submits weights every pass however long
    a scoring pass takes. A pass that raises reports the error; the next poll starts the next pass."""

    def __init__(self, scoring_pass: Callable[[], Coroutine[Any, Any, dict[str, Any]]]) -> None:
        self._pass = scoring_pass
        self._task: asyncio.Task[dict[str, Any]] | None = None

    def poll(self) -> dict[str, Any]:
        done: dict[str, Any] = {}
        if self._task is not None and self._task.done():
            task, self._task = self._task, None
            if task.cancelled():
                log.warning("scoring pass cancelled")
                done = {}
            elif (exc := task.exception()) is not None:
                log.error("scoring pass failed: %s", exc)
                done = {"scored": {"error": str(exc)[:200]}}
            else:
                done = task.result()
        if self._task is None:
            self._task = asyncio.create_task(self._pass())
        return done

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass
        self._task = None


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
    refused = other_subnet(store.last_run_subnet(), cfg.network, cfg.netuid)
    if refused:
        store.close()
        raise SystemExit(refused)
    cfg = replace(cfg, start_day=start_day_for(store, cfg.start_day))
    hl = HyperliquidInfo(cfg.hl_info_url, budget_per_minute=cfg.hl_budget)
    wallet = (
        bt.Wallet(name=cfg.wallet_name, hotkey=cfg.wallet_hotkey, path=cfg.wallet_path)
        if cfg.wallet_path
        else bt.Wallet(name=cfg.wallet_name, hotkey=cfg.wallet_hotkey)
    )
    started = False

    async def scoring_pass() -> dict[str, Any]:
        # on a connection of its own, so the chain's part of each pass goes on beside it
        target, report = scoring_target(cfg, store, day=day)
        if not scoring_due(cfg, store, day=day):
            return report  # a backlog's pending days, or why a given day is out of order
        try:
            async with bt.Subtensor(cfg.network) as scoring_client:
                return await score_step(cfg, store, hl, scoring_client, params, day=day)
        except Exception:
            # the connection failed before the day was tried: it is tried again at the usual pace, not every pass
            store.set_meta(f"attempt:{target}", str(time.time()))
            raise

    scorer = BackgroundScoring(scoring_pass)
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
                    if once:
                        report = await step(cfg, store, hl, client, wallet, params, day=day)
                        log.info("step: %s", json.dumps(report, default=str))
                        return
                    while True:
                        report = {"scored": None, "weights": None, **scorer.poll()}
                        report.update(
                            await chain_step(cfg, store, client, wallet, scoring_target(cfg, store, day=day)[0])
                        )
                        log.info("step: %s", json.dumps(report, default=str))
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
        await scorer.close()
        await hl.close()
        store.close()
