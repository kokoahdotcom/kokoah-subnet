"""Cluster self-matches, the mark clip and the two mirror rules."""

import pytest

from kokoah_subnet.mechanism import (
    DEFAULT,
    Fill,
    bound_counterparty_notional,
    clip_to_mark,
    drop_bound_counterparties,
    drop_cluster_matches,
    mirror_action,
    mirror_position_share,
)


def _fill(user: str, t_s: float, coin: str, side: str, sz: float, cp: str = "", px: float = 100.0) -> Fill:
    return Fill(int(t_s * 1000), user, coin, side, px, sz, True, 0.0, counterparty=cp)


def test_drop_cluster_matches() -> None:
    fills = [_fill("a", 1, "BTC", "B", 1, cp="a2"), _fill("a", 2, "BTC", "B", 1, cp="z"), _fill("a", 3, "BTC", "B", 1)]
    kept = drop_cluster_matches(fills, {"a": "c1", "a2": "c1"})
    assert [f.time_ms for f in kept] == [2000, 3000]


def test_clip_to_mark() -> None:
    fills = [
        Fill(0, "a", "BTC", "B", 101.0, 1.0, True, 0.0, mark_px=100.0),  # 1% off: valued at the mark
        Fill(0, "a", "BTC", "B", 100.4, 1.0, True, 0.0, mark_px=100.0),  # 0.4% off: kept
        Fill(0, "a", "BTC", "B", 150.0, 1.0, True, 0.0),  # no mark: kept
    ]
    out = clip_to_mark(fills, DEFAULT)
    assert [f.px for f in out] == [100.0, 100.4, 150.0]


def test_bound_counterparty_rule_is_exact() -> None:
    fills = {
        "a": [_fill("a", 0, "BTC", "B", 1.0, cp="b"), _fill("a", 1, "BTC", "B", 2.0, cp="0xmarket")],
        "b": [_fill("b", 0, "BTC", "A", 1.0, cp="a"), _fill("b", 5, "ETH", "A", 1.0, cp="c")],
        "c": [_fill("c", 2, "BTC", "A", 1.0, cp="0xmarket")],  # same time, opposite side, same size: not flagged
    }
    against = bound_counterparty_notional(fills)
    assert against == {"a": 100.0, "b": 200.0, "c": 0.0}
    kept = drop_bound_counterparties(fills["b"], set(fills))
    assert kept == []
    assert len(drop_bound_counterparties(fills["a"], set(fills))) == 1


def test_mirror_position_rule() -> None:
    days = 12
    a = [float(1000 + 10 * d) for d in range(days)]
    pos = {
        "a": {"BTC": a, "ETH": [500.0] * days},
        "b": {"BTC": [-x for x in a]},  # perfectly opposite on every day
        "c": {"BTC": [1000.0, 1200.0, 900.0, 1300.0, 950.0, 1250.0, 1000.0, 1100.0, 1050.0, 1150.0, 980.0, 1220.0]},
    }
    share = mirror_position_share(pos, DEFAULT)
    total_a = sum(a) + 500.0 * days
    assert share["a"] == pytest.approx(sum(a) / total_a)
    assert share["b"] == pytest.approx(1.0)
    assert share["c"] == 0.0  # long like a, noisy: not opposite to b
    # fewer overlapping days than the minimum: nothing
    short = {"a": {"BTC": a[:5]}, "b": {"BTC": [-x for x in a[:5]]}}
    assert mirror_position_share(short, DEFAULT) == {"a": 0.0, "b": 0.0}


def test_mirror_action_thresholds() -> None:
    assert mirror_action(0.2, DEFAULT).credit_factor == 1.0
    mid = mirror_action(0.4, DEFAULT)
    assert mid.credit_factor == pytest.approx(0.6) and not mid.zero_multiplier
    high = mirror_action(0.7, DEFAULT)
    assert high.credit_factor == pytest.approx(0.3) and high.zero_multiplier
