"""Eligibility (spec section 9a): the equity floor on the day's average, the qualifying trades, ineligible days,
and identity changes that must never carry credit or qualification to a fresh identity."""

import random
from dataclasses import replace
from datetime import date

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


def test_the_floor_is_the_scored_day_s() -> None:
    """$7,000 on average: below the $10,000 floor of the days before `min_equity_from`, above the $5,000 from it on."""
    assert (
        DEFAULT.min_equity_for("2026-11-23") == 10_000.0 and DEFAULT.min_equity_for(DEFAULT.min_equity_from) == 5_000.0
    )
    state = {"hk": MinerState(qualified=True, address=A, registered_block=100)}
    switch = (date.fromisoformat(DEFAULT.min_equity_from) - date.fromisoformat(DEFAULT.day_zero)).days
    assert DEFAULT.day_of(switch) == DEFAULT.min_equity_from and DEFAULT.day_of(switch - 1) == "2026-11-23"
    for day, eligible in ((switch - 1, False), (switch, True)):
        r = run(day, day_fills(day, 4, 10_000.0), [miner(7_000.0)], state).records[0]
        assert r.eligible is eligible and (eligible or r.ineligible_reason == "average equity below the minimum")


def test_spot_fills_count_for_the_volume_pool_from_their_day() -> None:
    """Spec 6.9: from `spot_from` on, a spot sell with the builder code earns credit like a perpetual fill and
    qualifies a new identity; the day before, it counts for nothing. Its notional never reaches the skill pool's
    volume."""
    switch = (date.fromisoformat(DEFAULT.spot_from) - date.fromisoformat(DEFAULT.day_zero)).days
    assert DEFAULT.day_of(switch) == DEFAULT.spot_from and not DEFAULT.spot_counts(DEFAULT.day_of(switch - 1))
    state = {"hk": MinerState(qualified=True, address=A, registered_block=100)}

    def spot_sells(day: int) -> list[Fill]:
        return [fill(DAY0 + day * D + i * 60_000, 2_000.0, coin="@1155", side="A") for i in range(3)]

    before = run(switch - 1, spot_sells(switch - 1), [miner(20_000.0)], state).records[0]
    assert before.fees.fills == 0 and before.credited.credit == 0.0 and before.phi_hat == 0.0
    after = run(switch, spot_sells(switch), [miner(20_000.0)], state).records[0]
    assert after.eligible and after.credited.fills == 3 and after.credited.credit == pytest.approx(3 * 2_000.0 * 2.5e-4)
    assert after.phi_hat > 0 and after.volume_pay_alpha > 0
    assert after.credited_perp_notional == 0.0 and after.volume_window_usd == 0.0  # the skill pool's: perpetual only
    # spot and perpetual fills together: both earn; only the perpetual notional is the skill pool's volume
    mixed = run(switch, spot_sells(switch) + day_fills(switch, 2, 1_000.0), [miner(20_000.0)], state).records[0]
    assert mixed.credited.fills == 5 and mixed.credited_perp_notional == pytest.approx(2_000.0)
    assert mixed.volume_window_usd == pytest.approx(2_000.0)
    # a new identity qualifies with spot trades alone (its first two trades earn nothing, as always)
    fresh = run(switch, spot_sells(switch), [miner(20_000.0)], {}).records[0]
    assert fresh.qualified and fresh.qual_trades == 3 and fresh.credited.fills == 1
    # the anti-wash rules hold for spot: a spot sell to the miner's own cluster earns nothing
    own = [replace(f, counterparty=A) for f in spot_sells(switch)]
    assert run(switch, own, [miner(20_000.0)], state).records[0].credited.fills == 0


def test_no_floor_from_its_day() -> None:
    """Spec 7.0: from `no_floor_from` on, any balance is eligible, even an unknown one (the balance is never checked);
    the day before, $50 or an unknown average is not. The activity gates' mean account value drops to $100."""
    from kokoah_subnet.mechanism.gates import passes_gates

    switch = (date.fromisoformat(DEFAULT.no_floor_from) - date.fromisoformat(DEFAULT.day_zero)).days
    before_day = DEFAULT.day_of(switch - 1)
    assert DEFAULT.min_equity_for(DEFAULT.no_floor_from) == 0.0 and DEFAULT.min_equity_for(before_day) == 5_000.0
    state = {"hk": MinerState(qualified=True, address=A, registered_block=100)}
    for avg in (50.0, None):
        before = run(switch - 1, day_fills(switch - 1, 4, 1_000.0), [miner(avg)], state).records[0]
        assert not before.eligible and before.credited.fills == 0
        after = run(switch, day_fills(switch, 4, 1_000.0), [miner(avg)], state).records[0]
        assert after.eligible and after.ineligible_reason is None and after.credited.fills == 4
        assert after.volume_pay_alpha > 0
    assert (
        DEFAULT.gate_mean_equity_for(before_day) == 500.0
        and DEFAULT.gate_mean_equity_for(DEFAULT.no_floor_from) == 100.0
    )
    assert passes_gates(10, 7, 150.0, DEFAULT, DEFAULT.gate_mean_equity_for(DEFAULT.no_floor_from))
    assert not passes_gates(10, 7, 150.0, DEFAULT, DEFAULT.gate_mean_equity_for(before_day))
    assert not passes_gates(10, 7, 99.0, DEFAULT, DEFAULT.gate_mean_equity_for(DEFAULT.no_floor_from))


def test_a_late_large_trade_never_shrinks_a_loss() -> None:
    """Spec 7.0: what the positions open in an hour needed raises the base of a gain's step only. $100 loses $20 by
    23:00, then one $400,000 round trip in the last hour: the loss stays −20% over the $100 that was there. A gain
    made in that hour counts over the $10,000 the trade needed; one made in the hours before, over what was there."""
    from kokoah_subnet.mechanism.eligibility import DayReturn, day_twr

    d0 = DAY0
    bases = DEFAULT.return_bases_for(DEFAULT.no_floor_from, [0.0] * 23 + [400_000.0])
    assert bases is not None and bases[:2] == (100.0, 100.0) and bases[2][23] == 10_000.0 and bases[2][0] == 0.0

    def day(values: list[float], profits: list[float]) -> DayReturn:
        assert bases is not None
        times = [d0 - H, d0, d0 + 23 * H, d0 + D, d0 + D + H]
        t = day_twr(
            list(zip(times, values, strict=True)), list(zip(times, profits, strict=True)), d0, d0 + D, None, (), *bases
        )
        assert t is not None
        return t

    lost = day([100.0, 100.0, 80.0, 80.0, 80.0], [0.0, 0.0, -20.0, -20.0, -20.0])
    assert lost.twr == pytest.approx(-0.2) and lost.capital == pytest.approx(100.0)
    late = day([100.0, 100.0, 100.0, 120.0, 120.0], [0.0, 0.0, 0.0, 20.0, 20.0])
    assert late.twr == pytest.approx(20 / 10_000)
    early = day([100.0, 100.0, 120.0, 120.0, 120.0], [0.0, 0.0, 20.0, 20.0, 20.0])
    assert early.twr == pytest.approx(0.2)
    # honest leverage: down to $500 holding $20,000 (40x), back to $1,010: +1% for the day, over what was there
    bases = DEFAULT.return_bases_for(DEFAULT.no_floor_from, [20_000.0] * 24)
    assert bases is not None
    times = [d0 - H, d0, d0 + 12 * H, d0 + D, d0 + D + H]
    vals, pnls = [1_000.0, 1_000.0, 500.0, 1_010.0, 1_010.0], [0.0, 0.0, -500.0, 10.0, 10.0]
    t = day_twr(list(zip(times, vals, strict=True)), list(zip(times, pnls, strict=True)), d0, d0 + D, None, (), *bases)
    assert t is not None and t.twr == pytest.approx(0.01)
    # the days before the rule: one floor for both, nothing needed; from it, no positions kept means no base
    assert DEFAULT.return_bases_for("2026-11-26", [1e9] * 24) == (5_000.0, 5_000.0, ())
    assert DEFAULT.return_bases_for("2026-11-23", None) == (10_000.0, 10_000.0, ())
    assert DEFAULT.return_bases_for(DEFAULT.no_floor_from, None) is None


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


def test_an_account_funded_late_in_the_day_reads_the_same_whether_or_not_it_was_seen_empty() -> None:
    """Funded at 21:54 after a week of nothing: the exchange's record begins that day (a zero point, then the
    deposit), so the day counts zero before it. A made-up zero point kept from a read while the account was empty
    would put a point before the day and ask for the full day's points instead: that is why such points are never
    stored, and every validator reads this day the same."""
    h = 3_600_000
    recorded = [(DAY0 + 21 * h + 37 * 60_000, 0.0), (DAY0 + 21 * h + 54 * 60_000, 20_000.0), (DAY0 + 25 * h, 20_000.0)]
    origin = DAY0 + 21 * h + 37 * 60_000  # the record begins with the zero point just before the deposit
    avg = daily_average_equity(recorded, DAY0, DAY0 + D, origin, 4)
    assert avg.value == pytest.approx(20_000.0 * (2 * h + 6 * 60_000 + 17 * 60_000 / 2) / D)
    assert avg.note == "account history begins during the day"
    made_up = (DAY0 - 9 * h, 0.0)  # what a read the day before, while the account held nothing, made up
    assert daily_average_equity([made_up, *recorded], DAY0, DAY0 + D, origin, 4).value is None


def test_a_day_before_the_record_begins_starts_from_nothing() -> None:
    """An account whose recorded history begins during the day starts it from nothing: a zero value and a zero
    profit, so the funding day is known (its deposit is money moved in, not a return) and the next day has a start.
    One whose history begins after the day held nothing all of it. Without an origin, a missing edge stays unknown."""
    from kokoah_subnet.mechanism.eligibility import day_change

    h = 3_600_000
    origin = DAY0 + 13 * h + 37 * 60_000
    values = [(origin, 0.0), (origin + 17 * 60_000, 20_000.0), (DAY0 + D + h, 20_000.0)]
    profits = [(origin, 0.0), (origin + 17 * 60_000, 0.0), (DAY0 + D + h, 150.0)]
    change = day_change(values, profits, DAY0, DAY0 + D, origin)
    assert change.known and change.value_start == 0.0 and change.value_end == pytest.approx(20_000.0)
    assert change.profit == pytest.approx(
        150.0 * (D - (origin + 17 * 60_000 - DAY0)) / (D + h - (origin + 17 * 60_000 - DAY0))
    )
    assert not day_change(values, profits, DAY0, DAY0 + D).known  # no origin: no start
    later = day_change(values, profits, DAY0 - D, DAY0, origin)  # the day before: nothing held
    assert later.known and (later.value_start, later.value_end, later.profit) == (0.0, 0.0, 0.0)
