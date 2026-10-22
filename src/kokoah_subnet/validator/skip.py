"""Close a day whose builder file the exchange never published: scored as a day with no fills, so the smoothing
advances once with zero credit and the next days can follow. Used only by announcement, so that every validator
closes the same day the same way; the day's artifact says so."""

from __future__ import annotations

import json

from kokoah_subnet.mechanism import DayFees, Params, advance
from kokoah_subnet.mechanism.payout import Allocation, weights

from .store import Store


def skip_day(store: Store, day: str, day_index: int, burn_hotkey: str, reason: str, params: Params) -> str:
    if store.day(day) is not None:
        return f"{day} is already scored"
    states = store.states_before(day)
    advanced = {hk: advance(s, DayFees.zero(), 0.0, 0.0, params, day_index=day_index) for hk, s in states.items()}
    store.put_states(advanced, day=day)
    pool = params.miner_pool_alpha
    allocation = Allocation(pool, 0.0, 0.0, 0.0, {}, {}, pool)
    artifact = {
        "day": day,
        "day_index": day_index,
        "skipped": reason,
        "pool_alpha": pool,
        "paid_alpha": 0.0,
        "burn_alpha": pool,
        "burn_hotkey": burn_hotkey,
        "weights": weights(allocation, burn_hotkey),
        "miners": [],
    }
    store.put_day(
        day,
        day_index,
        json.dumps(artifact, indent=1, sort_keys=True),
        pool_alpha=pool,
        paid_alpha=0.0,
        burn_alpha=pool,
        burn_hotkey=burn_hotkey,
        miners=0,
        miners_with_fills=0,
        volume_usd=0.0,
        builder_fee_usd=0.0,
        spec_version=params.__class__.__module__ and "skipped",
    )
    return f"{day} closed as skipped ({reason}); {len(advanced)} states advanced with zero credit"
