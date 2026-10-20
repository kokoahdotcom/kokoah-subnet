"""The validator daemon: score each completed UTC day once its builder file is published, and keep the weights set.

`step` is one pass and takes its clients as arguments, so it runs against fakes in tests; `run` wires the real ones.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from kokoah_subnet import __version__
from kokoah_subnet.mechanism import DEFAULT, SPEC_VERSION, Params

from .builder_feed import NotPublished
from .chain import set_weights, snapshot, weights_by_uid
from .collect import ValidatorConfig, collect_and_score
from .hyperliquid import HyperliquidInfo
from .store import Store

log = logging.getLogger("kokoah_subnet.validator")


def previous_utc_day(now: float | None = None) -> str:
    t = datetime.fromtimestamp(now if now is not None else time.time(), tz=UTC)
    return (t - timedelta(days=1)).strftime("%Y-%m-%d")


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
    """One pass: score the previous day if it is not scored yet (retrying missing files hourly), then submit the
    latest weights when a tempo has passed since the last submission. Returns what happened, for logs and tests."""
    report: dict[str, Any] = {"scored": None, "weights": None}
    target = day or previous_utc_day(now)
    clock = now if now is not None else time.time()
    if store.day(target) is None:
        last_try = float(store.get_meta(f"attempt:{target}", "0") or 0)
        if clock - last_try >= cfg.retry_missing_s:
            store.set_meta(f"attempt:{target}", str(clock))
            try:
                result = await collect_and_score(cfg, target, store, hl, client, params)
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
    latest = store.latest_day()
    if latest is not None:
        last_block = int(store.get_meta("weights:block", "0") or 0)
        last_day = store.get_meta("weights:day", "")
        if snap.block - last_block >= cfg.weights_every_blocks or last_day != latest[0]:
            fractions = json.loads(latest[2])["weights"]
            uids, values = weights_by_uid(fractions, snap.uid_of())
            if uids:
                result = await set_weights(client, wallet, cfg.netuid, uids, values)
                ok = bool(getattr(result, "success", True))
                message = str(getattr(result, "message", ""))
                store.put_weights_submission(latest[0], snap.block, uids, values, ok, message)
                report["weights"] = {"day": latest[0], "uids": uids, "ok": ok, "message": message}
                if ok:
                    store.set_meta("weights:block", str(snap.block))
                    store.set_meta("weights:day", latest[0])
            else:
                report["weights"] = {
                    "day": latest[0],
                    "uids": [],
                    "ok": False,
                    "message": "no registered hotkeys to weight",
                }
    return report


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
    try:
        async with bt.Subtensor(cfg.network) as client:
            store.start_run(
                network=cfg.network,
                netuid=cfg.netuid,
                validator_hotkey=wallet.hotkey.ss58_address,
                builder=cfg.builder,
                burn_hotkey=cfg.burn_hotkey,
                spec_version=SPEC_VERSION,
                code_version=__version__,
                config={k: str(v) for k, v in cfg.__dict__.items() if k not in ("extra",)},
            )
            while True:
                report = await step(cfg, store, hl, client, wallet, params, day=day)
                log.info("step: %s", json.dumps(report, default=str))
                if once:
                    return
                await asyncio.sleep(cfg.poll_s)
    finally:
        await hl.close()
        store.close()
