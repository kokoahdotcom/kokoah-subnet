"""The skill pool's entry rules and weight (specification section 7a)."""

import random

import pytest

from kokoah_subnet.mechanism import DEFAULT
from kokoah_subnet.mechanism.skillpool import adjusted_skill, entry_psr, passes_entry, skill_weight, volume_factor


def test_luck_floor_and_volume_cap() -> None:
    assert adjusted_skill(0.25, DEFAULT) == 0.0 and adjusted_skill(0.20, DEFAULT) == 0.0
    assert adjusted_skill(1.0, DEFAULT) == pytest.approx(1.0)
    assert adjusted_skill(0.625, DEFAULT) == pytest.approx(0.5)
    assert volume_factor(250_000, DEFAULT) == pytest.approx(0.25)
    assert volume_factor(1_000_000, DEFAULT) == 1.0 and volume_factor(100_000_000, DEFAULT) == 1.0


def test_agent_bonus_is_ten_percent_of_the_skill_weight() -> None:
    plain = skill_weight(0.9, 2_000_000, False, DEFAULT)
    assert skill_weight(0.9, 2_000_000, True, DEFAULT) == pytest.approx(1.1 * plain)
    assert skill_weight(0.2, 2_000_000, True, DEFAULT) == 0.0  # no bonus on nothing


def test_entry_test_needs_ninety_days_and_a_real_record() -> None:
    rnd = random.Random(11)
    strong = [rnd.gauss(0.004, 0.01) for _ in range(90)]
    assert entry_psr(strong[:89], DEFAULT) is None and not passes_entry(strong[:89], DEFAULT)
    assert passes_entry(strong, DEFAULT)
    # zero-edge accounts almost never pass (the simulator: about 2%)
    passed = sum(passes_entry([random.Random(s).gauss(0, 0.02) for _ in range(90)], DEFAULT) for s in range(200))
    assert passed <= 12
