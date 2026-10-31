"""The skill pool's entry rules and weight (specification section 7a)."""

import random

import pytest

from kokoah_subnet.mechanism import DEFAULT, Fill
from kokoah_subnet.mechanism.skillpool import (
    adjusted_skill,
    apply_copy_groups,
    copy_groups,
    copy_leader,
    copy_leads,
    copy_matches,
    entry_psr,
    passes_entry,
    skill_weight,
    smoothed_leads,
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
    registered = {"a": 5_000, "b": 1_000, "c": 1_000}  # the copier even registered earlier
    leads = copy_leads(["a", "b"], {"a": a_fills, "b": b_fills})
    assert leads["a"]["b"] > 5 * leads["b"].get("a", 0.0)  # b followed within a minute; a's next trade came later
    leader = copy_leader(["a", "b"], leads, registered, DEFAULT)
    assert leader == "a"
    w = apply_copy_groups({"a": 0.6, "b": 1.0, "c": 1.0}, groups, {("a", "b"): leader})
    assert w == {"a": 0.6, "b": 0.0, "c": 1.0}
    # without fills to compare, the earlier registration leads
    assert copy_leader(["a", "b"], {}, registered, DEFAULT) == "b"
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
    leaders = {tuple(g): copy_leader(g, {}, {}, DEFAULT) for g in groups}
    assert sum(apply_copy_groups(parts, groups, leaders).values()) == pytest.approx(single)


T0 = 1_792_540_800_000
REGISTERED = {"orig": 9_000, "copier": 1_000}  # the copier registered first: only a real lead may beat that


def order(user: str, t: int, notional: float, side: str = "B", pieces: int = 1) -> list[Fill]:
    return [Fill(t, user, "BTC", side, 100_000.0, notional / pieces / 100_000.0, True, 0.0) for _ in range(pieces)]


def test_small_orders_placed_just_before_do_not_take_the_lead() -> None:
    """The copier sends a $10 order every minute all day and copies each of the original's $50,000 orders a quarter
    of an hour later: most of the original's orders have a copier order just before them, but each match is worth
    the smaller order, and the real copies all follow the original."""
    trades = [T0 + h * 3 * 3_600_000 for h in range(8)]
    orig = [f for t in trades for f in order("orig", t, 50_000.0)]
    spam = [f for m in range(1440) for f in order("copier", T0 - 30_000 + m * 60_000, 10.0)]
    copies = [f for t in trades for f in order("copier", t + 15 * 60_000, 50_000.0)]
    leads = copy_leads(["orig", "copier"], {"orig": orig, "copier": spam + copies})
    assert leads["orig"]["copier"] > 100 * leads["copier"]["orig"]
    assert copy_leader(["orig", "copier"], leads, REGISTERED, DEFAULT) == "orig"


def test_an_order_filled_in_pieces_is_one_order() -> None:
    copy = order("copier", T0 + 60_000, 400_000.0, pieces=8)  # one copy, filled against eight resting orders
    leads = copy_leads(["orig", "copier"], {"orig": order("orig", T0, 400_000.0), "copier": copy})
    assert leads["orig"]["copier"] == pytest.approx(400_000.0 * 60 / (60 + 60))  # whole, weighed by its minute


def test_a_late_copy_is_still_seen_even_from_the_day_before() -> None:
    """Copying hours late, or the next day, leaves the copier no way to fall back on its earlier registration: the
    match looks back three days, and counts less the longer the copier waited."""
    late = copy_leads(["orig", "copier"], {"orig": order("orig", T0, 100_000.0),
                                            "copier": order("copier", T0 + 6 * 3_600_000, 100_000.0)})  # fmt: skip
    assert late["orig"]["copier"] == pytest.approx(100_000.0 * 60 / (60 + 6 * 3_600))
    next_day, matched = copy_matches(
        ["orig", "copier"],
        {"orig": [], "copier": order("copier", T0 + 20 * 3_600_000, 300_000.0)},
        earlier_of={"orig": order("orig", T0, 300_000.0)},
    )
    assert next_day["orig"]["copier"] > 0 and matched["orig"]["copier"] == pytest.approx(300_000.0)
    # weighed, the late copy is a small lead; unweighed, it is plenty to go by
    assert copy_leader(["orig", "copier"], next_day, REGISTERED, DEFAULT, matched) == "orig"


def test_a_few_tiny_orders_decide_nothing() -> None:
    """A copier copying late adds a few $1 orders just before the original usually trades: a lead that small
    decides nothing, and without a real lead the earlier registration keeps the weight."""
    leads = {"copier": {"orig": 3.0}}
    assert copy_leader(["orig", "copier"], leads, {"orig": 1_000, "copier": 9_000}, DEFAULT) == "orig"
    close = {"orig": {"copier": 5_000.0}, "copier": {"orig": 4_000.0}}  # neither clearly ahead: registration
    assert copy_leader(["orig", "copier"], close, {"orig": 9_000, "copier": 1_000}, DEFAULT) == "copier"
    assert copy_leader(["orig", "copier"], {"orig": {"copier": 9_000.0}}, REGISTERED, DEFAULT) == "orig"


def test_an_original_that_sends_its_orders_in_slices_keeps_the_lead() -> None:
    """The original buys $100,000 as twenty $5,000 slices over ten minutes, every one, two or four hours; the copier
    buys the whole $100,000 at once a minute after each burst. The copy is matched against every slice it follows."""
    for every_h in (1, 2, 4):
        bursts = [T0 + b * every_h * 3_600_000 for b in range(24 // every_h)]
        orig = [f for b in bursts for k in range(20) for f in order("orig", b + k * 30_000, 5_000.0)]
        copies = [f for b in bursts for f in order("copier", b + 11 * 60_000, 100_000.0)]
        leads = copy_leads(["orig", "copier"], {"orig": orig, "copier": copies})
        assert leads["orig"]["copier"] > 3 * leads["copier"].get("orig", 0.0), every_h
        assert copy_leader(["orig", "copier"], leads, REGISTERED, DEFAULT) == "orig"


def test_a_copier_s_second_account_makes_up_no_lead() -> None:
    """C copies the original an hour late on a two-hour rhythm, and C's second account S copies C a second later:
    C's lead over S says nothing about the original. Head to head neither C nor the original is clearly ahead, so
    the earlier registration decides, here the original's."""
    trades = [T0 + h * 2 * 3_600_000 for h in range(12)]
    orig = [f for t in trades for f in order("orig", t, 10_000.0)]
    c = [f for t in trades for f in order("c", t + 3_600_000, 10_000.0)]
    second = [f for t in trades for f in order("s", t + 3_601_000, 10_000.0)]
    leads = copy_leads(["orig", "c", "s"], {"orig": orig, "c": c, "s": second})
    assert sum(leads["c"].values()) > sum(leads["orig"].values())  # added up, C would look like the leader
    registered = {"orig": 1_000, "c": 5_000, "s": 6_000}
    assert copy_leader(["orig", "c", "s"], leads, registered, DEFAULT) == "orig"


def test_the_lead_is_judged_over_the_days() -> None:
    """A month of the original leading is not undone by one day on which the copier, by chance or on purpose, trades
    first: the leads are smoothed like credit and skill."""
    led: dict[str, dict[str, float]] = {}
    for _ in range(30):
        led = smoothed_leads(led, {"a": {"b": 100_000.0}}, DEFAULT)
    led = smoothed_leads(led, {"b": {"a": 150_000.0}}, DEFAULT)
    assert copy_leader(["a", "b"], led, {}, DEFAULT) == "a"
    for _ in range(60):  # nothing more matched: the leads fade away and are dropped
        led = smoothed_leads(led, {}, DEFAULT)
    assert led == {}


def test_matching_many_fills_stays_fast() -> None:
    import time as clock

    t0 = 1_792_540_800_000
    a = [Fill(t0 + i * 1_000, "a", "BTC", "B", 100.0, 1.0, True, 0.0) for i in range(20_000)]
    b = [Fill(t0 + i * 1_000 + 500, "b", "BTC", "B", 100.0, 1.0, True, 0.0) for i in range(20_000)]
    started = clock.monotonic()
    copy_leads(["a", "b"], {"a": a, "b": b})
    assert clock.monotonic() - started < 5  # the old pairwise scan took minutes here
