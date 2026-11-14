"""Skill and gates."""

import random

import pytest

from kokoah_subnet.mechanism import (
    DEFAULT,
    Fill,
    active_days,
    daily_return,
    passes_gates,
    round_trips,
    skill_factor,
    skill_raw,
)
from kokoah_subnet.mechanism.skill import ew_gain_to_pain, prob_sharpe


def test_daily_return_removes_flows() -> None:
    assert daily_return(1000.0, 1100.0, 50.0) == pytest.approx(0.05)
    assert daily_return(0.0, 100.0, 0.0) == 0.0


def test_a_day_never_loses_more_than_everything() -> None:
    # the whole-day return of a few dollars that took a deposit and lost a little: −100% at most, never −1,080%
    assert daily_return(3.98, 3.98 + 11_900.0 - 43.0, 11_900.0) == -1.0
    assert daily_return(100.0, -50.0, 0.0) == -1.0


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


H = 3_600_000
DAY0 = 1_763_000_000_000 - 1_763_000_000_000 % 86_400_000


def _twr(  # type: ignore[no-untyped-def]
    values: list[tuple[int, float]],
    profits: list[tuple[int, float]],
    moved_in: list[tuple[int, float]] | None = None,
):
    from kokoah_subnet.mechanism.eligibility import day_twr

    return day_twr(values, profits, DAY0, DAY0 + 24 * H, None, moved_in or [])


def test_a_day_without_money_moved_has_the_whole_day_return() -> None:
    values = [(DAY0 - H, 10_000.0), (DAY0 + 6 * H, 10_100.0), (DAY0 + 18 * H, 9_900.0), (DAY0 + 25 * H, 9_950.0)]
    profits = [(t, v - 10_000.0) for t, v in values]
    t = _twr(values, profits)
    assert t is not None
    start = 10_000.0 + 100.0 / 7  # midnight on the line from 23:00 to 06:00
    end = 9_900.0 + (9_950.0 - 9_900.0) * 6 / 7  # and on the line from 18:00 to 01:00 the next day
    assert t.twr == pytest.approx(end / start - 1)
    assert t.profit == pytest.approx(end - start) and t.capital == pytest.approx(start)
    assert t.profit / t.capital == pytest.approx(t.twr)  # both measures agree when no money moved


def test_money_moved_in_late_on_a_losing_day_does_not_cut_that_day_s_loss() -> None:
    """$10,000 loses $300 from 23:00 to 22:00; $90,000 comes in at 22:00 and the last two hours lose nothing: the day's
    return is the loss of the money at work (−2.9% from midnight), not −0.3% over everything moved in."""
    values = [(DAY0 - H, 10_000.0), (DAY0 + 22 * H - 1, 9_700.0), (DAY0 + 22 * H, 99_700.0), (DAY0 + 25 * H, 99_700.0)]
    profits = [(DAY0 - H, 0.0), (DAY0 + 22 * H - 1, -300.0), (DAY0 + 22 * H, -300.0), (DAY0 + 25 * H, -300.0)]
    t = _twr(values, profits)
    at_midnight = 10_000.0 - 300.0 / 23
    assert t is not None and t.twr == pytest.approx(9_700.0 / at_midnight - 1, rel=1e-6)
    assert t.twr < -0.028  # the whole-day formula over the capital moved in would say −0.29%


def test_a_deposit_into_a_nearly_empty_account_counts_from_when_it_came() -> None:
    """$3.98 until a deposit of $11,900 recorded at 02:00, then $43 lost: about −0.36%, not −1,080%. The deposit counts
    from the step it came in (from midnight, the start of that step between recorded points)."""
    values = [(DAY0, 3.98), (DAY0 + 2 * H, 11_903.98), (DAY0 + 20 * H, 11_860.98), (DAY0 + 24 * H, 11_860.98)]
    profits = [(DAY0, 0.0), (DAY0 + 2 * H, 0.0), (DAY0 + 20 * H, -43.0), (DAY0 + 24 * H, -43.0)]
    t = _twr(values, profits)
    assert t is not None and t.twr == pytest.approx(-43 / 11_903.98)
    assert t.profit == pytest.approx(-43.0) and t.capital == pytest.approx(11_903.98)


def test_money_moved_in_off_market_comes_off_the_step_it_came_in() -> None:
    """$100 at midnight; at 01:00 an off-market fill moves $1,000 in (its profit shows at the 02:00 point); $100,000 is
    deposited by 04:00. The fill is money moved in, not a +1,000% step: the day made nothing."""
    values = [(DAY0, 100.0), (DAY0 + 2 * H, 1_100.0), (DAY0 + 4 * H, 101_100.0), (DAY0 + 24 * H, 101_100.0)]
    profits = [(DAY0, 0.0), (DAY0 + 2 * H, 1_000.0), (DAY0 + 4 * H, 1_000.0), (DAY0 + 24 * H, 1_000.0)]
    unmoved = _twr(values, profits)
    assert unmoved is not None and unmoved.twr == pytest.approx(10.0)  # what the profit record alone says
    t = _twr(values, profits, [(DAY0 + H, 1_000.0)])
    assert t is not None and t.twr == pytest.approx(0.0) and t.profit == pytest.approx(0.0)
    assert t.capital == pytest.approx(100 + 1_000 + 100_000 * 22 / 24)  # each from the start of its step


def test_money_moved_out_counts_until_the_end_of_its_step() -> None:
    """$10,000 makes $100 by 12:00, then $9,000 is withdrawn by 14:00: the capital at work keeps the withdrawn money
    until the end of that step, so it is never smaller than what was there."""
    values = [(DAY0, 10_000.0), (DAY0 + 12 * H, 10_100.0), (DAY0 + 14 * H, 1_100.0), (DAY0 + 24 * H, 1_100.0)]
    profits = [(DAY0, 0.0), (DAY0 + 12 * H, 100.0), (DAY0 + 14 * H, 100.0), (DAY0 + 24 * H, 100.0)]
    t = _twr(values, profits)
    assert t is not None and t.twr == pytest.approx(0.01)
    assert t.capital == pytest.approx(10_000 - 9_000 * 10 / 24)
