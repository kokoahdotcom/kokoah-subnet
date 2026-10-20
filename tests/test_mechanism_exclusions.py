"""Cluster self-matches, the mark clip and the two mirror rules."""

import pytest

from kokoah_subnet.mechanism import (
    DEFAULT,
    Fill,
    clip_to_mark,
    drop_cluster_matches,
    mirror_action,
    mirror_position_share,
    mirror_timing_notional,
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


def test_mirror_timing_rule() -> None:
    fills = {
        "a": [_fill("a", 0, "BTC", "B", 1.0), _fill("a", 500, "ETH", "B", 2.0)],
        "b": [_fill("b", 30, "BTC", "A", 1.1), _fill("b", 600, "ETH", "A", 2.0)],  # ETH is 100 s later: no match
        "c": [_fill("c", 10, "BTC", "A", 2.0)],  # size off by 100%: no match
    }
    m = mirror_timing_notional(fills, DEFAULT)
    assert m["a"] == pytest.approx(100.0)
    assert m["b"] == pytest.approx(110.0)
    assert m["c"] == 0.0


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
