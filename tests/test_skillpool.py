"""The skill pool's entry rules and weight (spec v6, section 7a)."""

import random

import pytest

from kokoah_subnet.mechanism import DEFAULT, Fill
from kokoah_subnet.mechanism.skillpool import (
    adjusted_skill,
    apply_copy_groups,
    copy_groups,
    copy_leader,
    entry_psr,
    passes_entry,
    skill_weight,
    volume_factor,
)


def fill(user: str, t: int, coin: str, side: str) -> Fill:
    return Fill(t, user, coin, side, 100_000.0, 0.01, True, 0.25)


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


def test_a_copy_earns_nothing_and_the_original_keeps_its_weight() -> None:
    rnd = random.Random(5)
    days = [f"2026-09-{d:02d}" for d in range(1, 31)] + [f"2026-10-{d:02d}" for d in range(1, 31)]
    base = {d: rnd.gauss(0.002, 0.01) for d in days}
    copy = {d: r + rnd.gauss(0, 0.0005) for d, r in base.items()}  # the same trades, a little noise
    other = {d: rnd.gauss(0.002, 0.01) for d in days}
    groups = copy_groups({"a": base, "b": copy, "c": other}, DEFAULT)
    assert groups == [["a", "b"]]
    # b copies a: each of b's fills comes 40 seconds after a's on the same coin and side
    t0 = 1_792_540_800_000
    a_fills = [fill("a", t0 + i * 3_600_000, "BTC", "B" if i % 2 else "A") for i in range(8)]
    b_fills = [fill("b", f.time_ms + 40_000, f.coin, f.side) for f in a_fills]
    starts = {"a": "2026-09-20", "b": "2026-09-01", "c": "2026-09-01"}  # the copier even has the older record
    leader = copy_leader(["a", "b"], {"a": a_fills, "b": b_fills}, starts)
    assert leader == "a"
    w = apply_copy_groups({"a": 0.6, "b": 1.0, "c": 1.0}, groups, {("a", "b"): leader})
    assert w == {"a": 0.6, "b": 0.0, "c": 1.0}
    # without fills to compare, the longer record leads
    assert copy_leader(["a", "b"], {}, starts) == "b"
    # too few common days: not grouped
    short = {d: base[d] for d in days[:20]}
    assert copy_groups({"a": base, "b": short}, DEFAULT) == []


def test_splitting_above_the_cap_gains_nothing_when_the_accounts_trade_alike() -> None:
    rnd = random.Random(8)
    days = [f"2026-08-{d:02d}" for d in range(1, 32)]
    r = {d: rnd.gauss(0.002, 0.01) for d in days}
    single = skill_weight(0.9, 10_000_000, False, DEFAULT)
    parts = {f"x{i}": skill_weight(0.9, 1_000_000, False, DEFAULT) for i in range(10)}
    groups = copy_groups({k: r for k in parts}, DEFAULT)
    leaders = {tuple(g): copy_leader(g, {}, {}) for g in groups}
    assert sum(apply_copy_groups(parts, groups, leaders).values()) == pytest.approx(single)
