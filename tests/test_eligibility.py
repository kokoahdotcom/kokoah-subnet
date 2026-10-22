"""Eligibility (spec section 9a): the equity floor on the day's average, the qualifying trades, ineligible days,
and identity changes that must never carry credit or qualification to a fresh identity."""

import random

import pytest

from kokoah_subnet.mechanism import DEFAULT, Fill, MinerState
from kokoah_subnet.mechanism.day import DayInputs, DayResult, MinerInputs, score_day
from kokoah_subnet.mechanism.eligibility import (
    daily_average_equity,
    fresh_identity,
    identity_change,
    qualify,
    trades,
)

A = "0xaaaa000000000000000000000000000000000001"
B = "0xbbbb000000000000000000000000000000000002"
DAY0 = 1792454400000  # 2026-10-20 00:00 UTC
H = 3_600_000
D = 86_400_000


def fill(t: int, notional: float, user: str = A, coin: str = "BTC", side: str = "B") -> Fill:
    return Fill(
        t,
        user,
        coin,
        side,
        100.0,
        notional / 100.0,
        True,
        notional * 2.5e-4,
        counterparty="0xcafe",
        total_fee=notional * 7e-4,
    )


def day_fills(day: int, count: int, notional: float, user: str = A) -> list[Fill]:
    return [fill(DAY0 + day * D + i * 60_000, notional, user, side="B" if i % 2 == 0 else "A") for i in range(count)]


def hourly(day_start: int, values: list[float]) -> list[tuple[int, float]]:
    return [(day_start + i * H, v) for i, v in enumerate(values)]


# ----- the day's average account value ------------------------------------------------------------------------


def test_average_is_time_weighted_not_the_end_of_day_balance() -> None:
    # $2,000 all day, $20,000 deposited for the last two hours: the close is 20k, the day's average is far below
    pts = hourly(DAY0 - D, [2_000.0] * 24) + hourly(DAY0, [2_000.0] * 22 + [20_000.0] * 2) + [(DAY0 + D, 20_000.0)]
    avg = daily_average_equity(pts, DAY0, DAY0 + D, origin_ms=DAY0 - 30 * D, min_points=4)
    assert avg.value is not None and avg.value < 4_000 and avg.points == 24
    assert avg.value < DEFAULT.min_equity_usd


def test_constant_and_linear_paths() -> None:
    flat = hourly(DAY0 - 2 * H, [12_000.0] * 30)
    assert daily_average_equity(flat, DAY0, DAY0 + D, DAY0 - D, 4).value == pytest.approx(12_000.0)
    ramp = [(DAY0 - D, 0.0), (DAY0, 0.0), (DAY0 + 12 * H, 12_000.0), (DAY0 + D, 24_000.0), (DAY0 + 2 * D, 24_000.0)]
    assert daily_average_equity(ramp, DAY0, DAY0 + D, DAY0 - 5 * D, 2).value == pytest.approx(12_000.0)


def test_a_snapshot_is_never_enough() -> None:
    # one recorded point inside the day: the average is unknown, not estimated from that point
    pts = [(DAY0 - H, 50_000.0), (DAY0 + 5 * H, 50_000.0), (DAY0 + D + H, 50_000.0)]
    avg = daily_average_equity(pts, DAY0, DAY0 + D, DAY0 - 9 * D, 4)
    assert avg.value is None and "too few" in avg.note


def test_history_that_does_not_reach_the_day_start_is_unknown() -> None:
    # the account is old but the recorded points start mid-day (a window that no longer reaches back)
    pts = hourly(DAY0 + 6 * H, [50_000.0] * 30)
    assert daily_average_equity(pts, DAY0, DAY0 + D, origin_ms=DAY0 - 30 * D, min_points=4).value is None


def test_an_account_born_during_the_day_had_nothing_before() -> None:
    pts = hourly(DAY0 + 12 * H, [30_000.0] * 20)
    avg = daily_average_equity(pts, DAY0, DAY0 + D, origin_ms=DAY0 + 12 * H, min_points=4)
    assert avg.value == pytest.approx(15_000.0)
    assert daily_average_equity([], DAY0, DAY0 + D, None, 4).value == 0.0


# ----- qualifying trades --------------------------------------------------------------------------------------


def test_the_third_trade_with_enough_notional_qualifies_and_earns() -> None:
    fills = [fill(DAY0 + i * 60_000, 400.0) for i in range(5)]
    q = qualify(fills, False, 0, 0.0, DEFAULT)
    assert q.qualified and q.trades == 3 and q.notional == pytest.approx(1_200.0)
    assert [f.time_ms for f in q.credited] == [DAY0 + i * 60_000 for i in (2, 3, 4)]
    assert q.qualified_at_ms == DAY0 + 2 * 60_000


def test_three_trades_are_not_enough_without_the_notional() -> None:
    fills = [fill(DAY0 + i * 60_000, 100.0) for i in range(3)] + [fill(DAY0 + 10 * 60_000, 800.0)]
    q = qualify(fills, False, 0, 0.0, DEFAULT)
    assert q.qualified and q.trades == 4 and len(q.credited) == 1  # the fourth trade completes $1,000
    q2 = qualify(fills[:3], False, 0, 0.0, DEFAULT)
    assert not q2.qualified and q2.credited == () and q2.trades == 3


def test_one_order_filled_in_pieces_is_one_trade() -> None:
    # one taker order crossing three resting orders: three fills at the same millisecond, one trade
    pieces = [fill(DAY0, 500.0), fill(DAY0, 500.0), fill(DAY0, 500.0)]
    assert len(trades(pieces)) == 1
    q = qualify(pieces, False, 0, 0.0, DEFAULT)
    assert not q.qualified and q.trades == 1 and q.notional == pytest.approx(1_500.0)


def test_qualification_progress_carries_across_days_and_order_of_input_does_not_matter() -> None:
    day1 = [fill(DAY0 + i * 60_000, 600.0) for i in range(2)]
    q1 = qualify(day1, False, 0, 0.0, DEFAULT)
    assert not q1.qualified and q1.trades == 2
    day2 = [fill(DAY0 + D + i * 60_000, 600.0) for i in range(3)]
    shuffled = day2[:]
    random.Random(7).shuffle(shuffled)
    q2 = qualify(shuffled, q1.qualified, q1.trades, q1.notional, DEFAULT)
    assert q2.qualified and q2.trades == 3 and len(q2.credited) == 3  # the first trade of day 2 completes it


# ----- through the scoring day --------------------------------------------------------------------------------


def miner(equity: float | None, hotkey: str = "hk", address: str = A, reg: int | None = 100) -> MinerInputs:
    return MinerInputs(hotkey, address, avg_equity_usd=equity, registered_block=reg, staked_min_alpha=0.0)


def run(day: int, fills: list[Fill], miners: list[MinerInputs], states: dict[str, MinerState]) -> DayResult:
    return score_day(DayInputs(day_index=day, price_usd=1.0, fills=fills, miners=miners, states=states), DEFAULT)


def test_registration_alone_earns_nothing() -> None:
    res = run(0, [], [miner(1e6)], {})
    r = res.records[0]
    assert not r.eligible and r.ineligible_reason == "not yet qualified" and r.pay_alpha == 0.0
    assert res.allocation.burn_alpha == pytest.approx(DEFAULT.miner_pool_alpha)


def test_new_miner_qualifies_the_same_day_and_the_first_two_trades_earn_nothing() -> None:
    res = run(0, day_fills(0, 10, 1_000.0), [miner(20_000.0)], {})
    r = res.records[0]
    assert r.eligible and r.qualified and r.credited.fills == 8 and r.fees.fills == 10
    assert res.states["hk"].phi_hat == pytest.approx(0.3 * 8 * 0.25)
    assert r.pay_alpha > 0


def test_equity_below_the_floor_pays_nothing_and_credit_only_decays() -> None:
    state = {"hk": MinerState(phi_hat=100.0, fee_hat=280.0, qualified=True, address=A, registered_block=100)}
    other = MinerInputs("hk2", B, avg_equity_usd=50_000.0, registered_block=200)
    states = {**state, "hk2": MinerState(phi_hat=100.0, fee_hat=280.0, qualified=True, address=B, registered_block=200)}
    fills = day_fills(5, 10, 10_000.0) + day_fills(5, 10, 10_000.0, user=B)
    low = run(5, fills, [miner(9_999.99), other], states)
    r, r2 = low.records
    assert not r.eligible and r.ineligible_reason == "average equity below the minimum"
    assert r.pay_alpha == 0.0 and r.volume_pay_alpha == 0.0 and r.credited.fills == 0
    assert r.fees.fills == 10 and r.fees.notional == pytest.approx(100_000.0)  # volume still recorded
    assert low.states["hk"].phi_hat == pytest.approx(70.0) and low.states["hk"].fee_hat == pytest.approx(196.0)
    assert r2.pay_alpha == pytest.approx(DEFAULT.miner_pool_alpha)  # the ineligible miner dilutes nobody
    # exactly at the floor counts
    assert run(5, fills, [miner(10_000.0), other], states).records[0].eligible


def test_recovery_resumes_automatically_without_back_pay() -> None:
    state = {"hk": MinerState(phi_hat=100.0, fee_hat=280.0, qualified=True, address=A, registered_block=100)}
    states = state
    for day in range(1, 11):  # ten days below the floor, trading every day
        res = run(day, day_fills(day, 4, 10_000.0), [miner(500.0)], states)
        assert res.records[0].pay_alpha == 0.0
        states = res.states
    assert states["hk"].phi_hat == pytest.approx(100.0 * 0.7**10)
    assert states["hk"].qualified  # never disqualified
    back = run(11, day_fills(11, 4, 10_000.0), [miner(10_500.0)], states)
    r = back.records[0]
    assert r.eligible and r.credited.fills == 4
    # credit today is only today's trades: nothing from the ineligible days was kept for later
    assert back.states["hk"].phi_hat == pytest.approx(0.7 * 100.0 * 0.7**10 + 0.3 * 4 * 2.5)
    assert r.pay_alpha > 0


def test_unknown_equity_is_not_eligible() -> None:
    state = {"hk": MinerState(qualified=True, address=A, registered_block=100)}
    r = run(3, day_fills(3, 4, 10_000.0), [miner(None)], state).records[0]
    assert not r.eligible and r.ineligible_reason == "average equity unknown" and r.pay_alpha == 0.0


def test_skill_is_recorded_on_ineligible_days() -> None:
    m = MinerInputs("hk", A, returns=[0.01, -0.005] * 30, avg_equity_usd=100.0, registered_block=100)
    res = run(3, day_fills(3, 4, 10_000.0), [m], {"hk": MinerState(qualified=True, address=A)})
    assert res.records[0].skill_raw > 0 and res.states["hk"].g == pytest.approx(0.3 * res.records[0].skill_raw)


# ----- wallets and hotkeys ------------------------------------------------------------------------------------


def established(address: str = A, reg: int = 100) -> MinerState:
    return MinerState(
        phi_hat=500.0,
        fee_hat=1400.0,
        g=0.4,
        paid_days=((1, 50.0),),
        zero_until_day=20,
        qualified=True,
        qual_trades=3,
        qual_notional=5_000.0,
        address=address,
        registered_block=reg,
    )


def test_migrating_to_a_new_wallet_drops_credit_and_qualification_but_keeps_the_record() -> None:
    res = run(2, day_fills(2, 2, 10_000.0, user=B), [miner(1e6, address=B)], {"hk": established()})
    r = res.records[0]
    assert r.identity_reset == "address changed"
    assert not r.eligible and r.ineligible_reason == "not yet qualified" and r.pay_alpha == 0.0
    s = res.states["hk"]
    assert s.phi_hat == 0.0 and s.fee_hat == 0.0 and not s.qualified and s.qual_trades == 2
    assert s.address == B and s.g == pytest.approx(0.7 * 0.4)  # skill record kept
    assert s.paid_days[0] == (1, 50.0) and s.zero_until_day == 20  # pay history and penalty kept


def test_registering_again_is_a_fresh_identity() -> None:
    res = run(2, [], [miner(1e6, reg=999)], {"hk": established()})
    r = res.records[0]
    assert r.identity_reset == "registered again" and not r.qualified and res.states["hk"].phi_hat == 0.0
    assert res.states["hk"].registered_block == 999
    # the same registration is not a reset
    assert run(2, [], [miner(1e6, reg=100)], {"hk": established()}).records[0].identity_reset is None


def test_a_new_hotkey_starts_from_nothing() -> None:
    # the address leaves one hotkey and binds another: the new hotkey has no state and must qualify itself
    r = run(2, day_fills(2, 2, 10_000.0), [miner(1e6, hotkey="hkNew")], {"hk": established()}).records[0]
    assert not r.qualified and r.pay_alpha == 0.0


def test_a_hotkey_swap_carries_history_but_not_credit() -> None:
    s = fresh_identity(established())
    assert s.phi_hat == 0.0 and s.fee_hat == 0.0 and not s.qualified and s.qual_trades == 0
    assert s.g == 0.4 and s.paid_days == ((1, 50.0),) and s.zero_until_day == 20
    assert identity_change(s, A, 100) is None  # applied once, not again on the next day


def test_identical_inputs_give_identical_results() -> None:
    fills = day_fills(4, 12, 3_000.0) + day_fills(4, 12, 2_000.0, user=B)
    shuffled = fills[:]
    random.Random(3).shuffle(shuffled)
    miners = [miner(30_000.0), miner(40_000.0, hotkey="hk2", address=B, reg=7)]
    one = run(4, fills, miners, {})
    two = run(4, shuffled, miners, {})
    assert one.records == two.records and one.states == two.states
