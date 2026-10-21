"""Skill, holding and gates."""

import random

import pytest

from kokoah_subnet.mechanism import (
    DEFAULT,
    Fill,
    active_days,
    daily_return,
    holding_factor,
    passes_gates,
    round_trips,
    skill_factor,
    skill_raw,
)
from kokoah_subnet.mechanism.skill import ew_gain_to_pain, prob_sharpe


def test_daily_return_removes_flows() -> None:
    assert daily_return(1000.0, 1100.0, 50.0) == pytest.approx(0.05)
    assert daily_return(0.0, 100.0, 0.0) == 0.0


def test_prob_sharpe_basics() -> None:
    assert prob_sharpe([0.01] * 5, 0.03, 10) == 0.5  # too few days
    assert prob_sharpe([0.0] * 30, 0.03, 10) == 0.5  # no variance
    rng = random.Random(1)
    edge = [rng.gauss(0.006, 0.02) for _ in range(60)]
    noise = [rng.gauss(0.0, 0.02) for _ in range(60)]
    assert prob_sharpe(edge, 0.03, 10) > 0.8
    assert prob_sharpe(noise, 0.03, 10) < 0.6


def test_gain_to_pain_weights_recent_days() -> None:
    assert ew_gain_to_pain([0.01, 0.01, -0.01], 10.0) < ew_gain_to_pain([-0.01, 0.01, 0.01], 10.0)
    assert ew_gain_to_pain([0.01, 0.02], 10.0) == 10.0
    assert ew_gain_to_pain([-0.01], 10.0) == 0.0


def test_skill_raw_profiles() -> None:
    strong = [0.015, -0.005, 0.01, 0.02, -0.01] * 12  # mean +0.6% a day, daily Sharpe about 0.5
    luck = [0.01, -0.01] * 30  # mean zero
    s, n = skill_raw(strong, DEFAULT), skill_raw(luck, DEFAULT)
    assert s.raw > 0.5 > n.raw and n.raw == 0.0
    assert 0 <= n.raw <= 1 and 0 <= s.raw <= 1
    assert s.raw == pytest.approx(s.confidence * s.quality)


def test_blowups_halve_quality_and_returns_are_capped() -> None:
    base = [0.01] * 58
    one = skill_raw([*base, -0.5, 0.01], DEFAULT)
    two = skill_raw([*base[:-1], -0.5, -0.5, 0.01], DEFAULT)
    assert two.quality <= one.quality
    # a −50% day counts as −10%: the quality of [+1% × 58, −50%, +1%] equals that with −10% in its place
    same = skill_raw([*base, -0.10, 0.01], DEFAULT)
    assert one.quality == pytest.approx(same.quality)


def test_skill_factor_scale() -> None:
    assert skill_factor(0.0, DEFAULT) == 0.0
    assert skill_factor(0.325, DEFAULT) == pytest.approx(0.5)
    assert skill_factor(0.65, DEFAULT) == 1.0
    assert skill_factor(2.0, DEFAULT) == 1.0


def test_holding_factor() -> None:
    assert holding_factor(100.0, 100.0) == 1.0
    assert holding_factor(0.0, 100.0) == 0.0
    assert holding_factor(50.0, 100.0) == 0.5
    assert holding_factor(110.0, 100.0, collateral_locked_alpha=10.0) == 1.0  # collateral does not count
    assert holding_factor(10.0, 0.0, collateral_locked_alpha=10.0) == 0.0  # a fresh hotkey starts at zero
    assert holding_factor(10.0, 0.0) == 1.0  # bought alpha counts even before any pay
    assert holding_factor(0.5, 0.0) == 0.5  # judged against one alpha when nothing was paid


def _fill(t: int, coin: str, side: str, sz: float) -> Fill:
    return Fill(t * 1000, "0xa", coin, side, 100.0, sz, True, 0.0)


def test_round_trips_count_zero_crossings_and_flips() -> None:
    fills = [
        _fill(1, "BTC", "B", 1.0),
        _fill(2, "BTC", "A", 1.0),  # back to flat: 1
        _fill(3, "BTC", "A", 2.0),  # short
        _fill(4, "BTC", "B", 3.0),  # flip to long: 2
        _fill(5, "ETH", "B", 1.0),
        _fill(6, "ETH", "A", 0.5),  # reduce only
    ]
    assert round_trips(fills) == 2
    assert round_trips(fills, {"ETH": -0.5}) == 4  # ETH started short: the buy flips it, the sell flattens it


def test_active_days_and_gates() -> None:
    fills = [_fill(86400 * d, "BTC", "B", 1.0) for d in range(7)]
    assert active_days(fills) == 7
    assert passes_gates(10, 7, 500.0, DEFAULT)
    assert not passes_gates(9, 7, 500.0, DEFAULT)
    assert not passes_gates(10, 6, 500.0, DEFAULT)
    assert not passes_gates(10, 7, 499.0, DEFAULT)
