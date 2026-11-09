"""The skill pool's entry rules and weight (specification section 7a)."""

import random

import pytest

from kokoah_subnet.mechanism import DEFAULT, Params
from kokoah_subnet.mechanism.skillpool import (
    adjusted_skill,
    entry_psr,
    passes_entry,
    record_days,
    record_factor,
    skill_weight,
    volume_factor,
)


def test_luck_floor_and_volume_cap() -> None:
    assert adjusted_skill(0.25, DEFAULT) == 0.0 and adjusted_skill(0.20, DEFAULT) == 0.0
    assert adjusted_skill(1.0, DEFAULT) == pytest.approx(1.0)
    assert adjusted_skill(0.625, DEFAULT) == pytest.approx(0.5)
    assert volume_factor(250_000, DEFAULT) == pytest.approx(0.25)
    assert volume_factor(1_000_000, DEFAULT) == 1.0 and volume_factor(100_000_000, DEFAULT) == 1.0


def test_agent_bonus_is_ten_percent_of_the_skill_weight() -> None:
    plain = skill_weight(0.9, 2_000_000, False, DEFAULT, days=90)
    assert skill_weight(0.9, 2_000_000, True, DEFAULT, days=90) == pytest.approx(1.1 * plain)
    assert skill_weight(0.2, 2_000_000, True, DEFAULT, days=90) == 0.0  # no bonus on nothing


def test_entry_test_needs_thirty_days_since_joining_and_reads_ninety_at_most() -> None:
    rnd = random.Random(11)
    strong = [rnd.gauss(0.004, 0.01) for _ in range(90)]
    assert entry_psr(strong[:29], DEFAULT) is None and not passes_entry(strong[:29], DEFAULT)
    assert passes_entry(strong[:30], DEFAULT) and passes_entry(strong, DEFAULT)
    # only the last 90 returns count: a long bad past no longer holds a strong recent record back
    bad = [rnd.gauss(-0.004, 0.01) for _ in range(200)]
    assert passes_entry(bad + strong, DEFAULT) and entry_psr(bad + strong, DEFAULT) == entry_psr(strong, DEFAULT)
    assert record_days(strong[:45], DEFAULT) == 45 and record_days(bad + strong, DEFAULT) == 90
    # zero-edge accounts almost never pass, at the shortest record or the longest (the simulator: about 3%)
    for days in (30, 90):
        passed = sum(passes_entry([random.Random(s).gauss(0, 0.02) for _ in range(days)], DEFAULT) for s in range(200))
        assert passed <= 14


def test_a_longer_record_weighs_more_up_to_ninety_days() -> None:
    assert record_factor(30, DEFAULT) == pytest.approx(0.8)
    assert record_factor(60, DEFAULT) == pytest.approx(0.9)
    assert record_factor(90, DEFAULT) == 1.0 and record_factor(365, DEFAULT) == 1.0
    full = skill_weight(0.9, 2_000_000, False, DEFAULT, days=90)
    assert skill_weight(0.9, 2_000_000, False, DEFAULT, days=30) == pytest.approx(0.8 * full)
    assert skill_weight(0.9, 2_000_000, True, DEFAULT, days=30) == pytest.approx(0.88 * full)


def test_record_parameters_are_checked() -> None:
    with pytest.raises(ValueError):
        Params(entry_min_days=5)  # below the PSR's own minimum
    with pytest.raises(ValueError):
        Params(entry_min_days=120)  # above the longest window
    with pytest.raises(ValueError):
        Params(record_start_weight=0.0)
    assert record_factor(30, Params(entry_min_days=90)) == 1.0  # one fixed window: no growth
