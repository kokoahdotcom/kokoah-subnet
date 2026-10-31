"""A whole scoring day through `score_day`: the two pools, the skill pool rules, a mirrored pair."""

import random
from dataclasses import replace

import pytest

from kokoah_subnet.mechanism import DEFAULT, Fill, MinerState, to_u16
from kokoah_subnet.mechanism.day import DayInputs, MinerInputs, score_day

A = "0xaaaa000000000000000000000000000000000001"
B = "0xbbbb000000000000000000000000000000000002"
DAY0 = 1792454400000  # 2026-10-20 00:00 UTC


def taker_fills(
    user: str, count: int, notional_each: float, day_ms: int = DAY0, cp: str = "0xcafe", coin: str = "BTC"
) -> list[Fill]:
    px, sz = 100_000.0, notional_each / 100_000.0
    fee = notional_each * DEFAULT.builder_fee_rate
    out = []
    for i in range(count):
        side = "B" if i % 2 == 0 else "A"
        out.append(
            Fill(
                day_ms + i * 60_000,
                user,
                coin,
                side,
                px,
                sz,
                True,
                fee,
                counterparty=cp,
                total_fee=fee + notional_each * 4.5 / 1e4,
            )
        )
    return out


def window_fills(user: str) -> list[Fill]:
    # 30 round trips over 15 days, enough for every gate
    fills: list[Fill] = []
    for d in range(15):
        fills += taker_fills(user, 4, 1000.0, DAY0 - (d + 1) * 86_400_000)
    return fills


def miner(hotkey: str, address: str, returns: list[float], agent: bool = False, volume: float = 2e6) -> MinerInputs:
    return MinerInputs(
        hotkey=hotkey,
        address=address,
        returns=returns,
        mean_equity_usd=50_000.0,
        avg_equity_usd=50_000.0,
        window_fills=window_fills(address),
        credited_notional_prior_usd=volume,
        agent_verified=agent,
    )


def strong_record(seed: int) -> list[float]:
    rnd = random.Random(seed)
    return [rnd.gauss(0.004, 0.01) for _ in range(90)]


def established(address: str, g: float) -> MinerState:
    return MinerState(phi_hat=1000.0, fee_hat=2800.0, g=g, qualified=True, address=address)


def test_worked_example_two_pools() -> None:
    # equal volume today; A has a proven record, B none: the volume pool splits evenly, the skill pool goes to A
    fills = taker_fills(A, 40, 100_000.0) + taker_fills(B, 40, 100_000.0, coin="ETH")
    states = {"hkA": established(A, 0.65), "hkB": established(B, 0.0)}
    flat = [0.01, -0.01] * 45
    inputs = DayInputs(
        day_index=100, price_usd=5.0, fills=fills, states=states,
        miners=[miner("hkA", A, strong_record(1)), miner("hkB", B, flat)],
    )  # fmt: skip
    res = score_day(inputs, DEFAULT)
    ra, rb = res.records
    pool = DEFAULT.miner_pool_alpha
    assert ra.entry_pass and ra.skill_pool_reason is None and ra.skill_weight > 0
    assert not rb.entry_pass and rb.skill_pool_reason == "entry test"
    assert ra.volume_pay_alpha == pytest.approx(0.4 * pool) and rb.volume_pay_alpha == pytest.approx(0.4 * pool)
    assert ra.skill_pay_alpha == pytest.approx(0.2 * pool) and rb.skill_pay_alpha == 0.0
    assert res.allocation.paid_alpha == pytest.approx(pool) and res.allocation.burn_alpha == 0.0
    w = res.weights("burn")
    assert sum(w.values()) == pytest.approx(1.0) and "burn" not in w
    assert sum(to_u16(w).values()) == 65535
    last_day, last_pay = res.states["hkA"].paid_days[-1]
    assert last_day == 100 and last_pay == pytest.approx(ra.pay_alpha)


def test_agent_bonus_is_ten_percent_more_skill_weight() -> None:
    fills = taker_fills(A, 10, 100_000.0) + taker_fills(B, 10, 100_000.0, coin="ETH")
    rec = strong_record(4)
    states = {"hkA": established(A, 0.65), "hkB": established(B, 0.65)}
    other = strong_record(6)  # an independent record, not a copy
    inputs = DayInputs(
        day_index=5, price_usd=1.0, fills=fills, states=states,
        miners=[miner("hkA", A, rec, agent=True), miner("hkB", B, other)],
    )  # fmt: skip
    ra, rb = score_day(inputs, DEFAULT).records
    assert ra.agent_verified and ra.entry_pass and rb.entry_pass
    assert ra.skill_weight / (1.1 * ra.skill_adjusted * ra.volume_factor) == pytest.approx(1.0)
    assert ra.skill_weight == pytest.approx(1.1 * rb.skill_weight * ra.skill_adjusted / rb.skill_adjusted)


def test_fresh_miner_earns_volume_pay_but_not_yet_skill_pay() -> None:
    fills = taker_fills(A, 10, 10_000.0)
    inputs = DayInputs(
        day_index=0, price_usd=1.0, fills=fills, states={}, miners=[MinerInputs("hk", A, avg_equity_usd=20_000.0)]
    )
    res = score_day(inputs, DEFAULT)
    r = res.records[0]
    assert r.eligible and r.qualified and r.credited.fills == 8  # the first two trades qualify but earn nothing
    assert res.states["hk"].phi_hat == pytest.approx(0.3 * r.credited.credit)
    assert r.skill_pool_reason == "activity gates" and r.skill_pay_alpha == 0.0
    assert r.volume_pay_alpha == pytest.approx(DEFAULT.miner_pool_alpha)


def test_trades_between_miners_are_not_paid_and_report_no_one() -> None:
    """A and B trade against each other all day (the counterparty field says so): none of it earns credit, but
    nobody is reported, since any miner can take another's resting orders to get it reported."""
    fa = [Fill(DAY0 + i * 60_000, A, "BTC", "B", 100_000.0, 1.0, True, 25.0, counterparty=B) for i in range(20)]
    fb = [Fill(DAY0 + i * 60_000, B, "BTC", "A", 100_000.0, 1.0, False, 25.0, counterparty=A) for i in range(20)]
    own = taker_fills(B, 4, 10_000.0)  # B's own trades with the market
    inputs = DayInputs(
        day_index=5,
        price_usd=1.0,
        fills=fa + fb + own,
        states={"hkA": MinerState(qualified=True), "hkB": MinerState(qualified=True)},
        miners=[MinerInputs("hkA", A, avg_equity_usd=1e6), MinerInputs("hkB", B, avg_equity_usd=1e6)],
    )
    res = score_day(inputs, DEFAULT)
    ra, rb = res.records
    assert ra.counterparty_share == pytest.approx(1.0) and rb.counterparty_share > 0.9  # recorded
    for r in res.records:
        assert r.mirrored_share == 0.0 and r.credit_factor == 1.0 and not r.reported and not r.zeroed
    assert ra.credited.fills == 0 and rb.credited.fills == 4  # only B's trades with the market earn credit
    assert not res.states["hkB"].is_zeroed(6)


def test_opposite_positions_report_only_a_pair_one_owner_tied_together() -> None:
    days = 12
    long = [float(100_000 + 1_000 * d) for d in range(days)]
    positions = {A: {"BTC": long}, B: {"BTC": [-x for x in long]}}
    states = {"hkA": MinerState(qualified=True), "hkB": MinerState(qualified=True)}
    miners = [MinerInputs("hkA", A, avg_equity_usd=1e6), MinerInputs("hkB", B, avg_equity_usd=1e6)]
    base = DayInputs(day_index=5, price_usd=1.0, fills=[], states=states, miners=miners, positions=positions)
    stranger = score_day(base, DEFAULT)  # anyone can hold the opposite of a trader's position: nobody reported
    assert all(not r.reported and r.position_share == 0.0 for r in stranger.records)
    same_owner = score_day(replace(base, owner_group_of={A: A, B: A}), DEFAULT)  # e.g. hotkeys on one coldkey
    assert all(r.reported and r.position_share == pytest.approx(1.0) for r in same_owner.records)


def test_cluster_self_matches_count_for_nothing() -> None:
    fills = taker_fills(A, 10, 10_000.0, cp=B)
    inputs = DayInputs(
        day_index=0, price_usd=1.0, fills=fills, states={}, miners=[MinerInputs("hk", A)], cluster_of={A: "c", B: "c"}
    )
    res = score_day(inputs, DEFAULT)
    assert res.records[0].fees.fills == 0 and res.records[0].pay_alpha == 0.0


def test_spot_fills_count_for_nothing() -> None:
    spot = [
        Fill(DAY0 + i * 60_000, A, "PURR/USDC", "A", 0.5, 20_000.0, True, 2.5, counterparty="0x1") for i in range(10)
    ] + [Fill(DAY0 + 1, A, "@107", "A", 1.0, 1_000.0, True, 0.25, counterparty="0x1")]
    perp = taker_fills(A, 2, 10_000.0)
    inputs = DayInputs(day_index=0, price_usd=1.0, fills=spot + perp, states={}, miners=[MinerInputs("hk", A)])
    res = score_day(inputs, DEFAULT)
    assert res.records[0].fees.fills == 2 and res.records[0].fees.builder_fee == pytest.approx(5.0)


def test_a_penalty_that_follows_the_address_keeps_it_out_of_the_skill_pool() -> None:
    fills = taker_fills(A, 40, 100_000.0)
    m = miner("hkNew", A, strong_record(6))
    states = {"hkNew": established(A, 0.65)}
    res = score_day(
        DayInputs(day_index=10, price_usd=1.0, fills=fills, states=states, miners=[m], penalized_until={A: 20}),
        DEFAULT,
    )
    r = res.records[0]
    assert r.zeroed and r.skill_pool_reason == "mirror penalty" and r.skill_pay_alpha == 0.0
    assert r.volume_pay_alpha > 0  # the volume pool still pays its builder fees


def test_accounts_that_trade_alike_each_keep_their_own_skill_weight() -> None:
    """Copy trading is never punished: two accounts with the very same record, one copying the other, are each
    weighed on their own."""
    rec = strong_record(1)
    fills = taker_fills(A, 10, 100_000.0) + taker_fills(B, 10, 100_000.0, coin="ETH")
    states = {"hkA": established(A, 0.65), "hkB": established(B, 0.65)}
    inputs = DayInputs(
        day_index=5, price_usd=1.0, fills=fills, states=states,
        miners=[miner("hkA", A, rec), miner("hkB", B, list(rec))],
    )  # fmt: skip
    ra, rb = score_day(inputs, DEFAULT).records
    assert ra.skill_weight > 0 and ra.skill_weight == pytest.approx(ra.skill_adjusted * ra.volume_factor)
    assert rb.skill_weight == pytest.approx(ra.skill_weight)
