"""A whole scoring day through `score_day`: the worked example, smoothing across days, a mirrored pair."""

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


def miner(hotkey: str, address: str, returns: list[float], staked: float) -> MinerInputs:
    return MinerInputs(
        hotkey=hotkey,
        address=address,
        returns=returns,
        mean_equity_usd=50_000.0,
        staked_min_alpha=staked,
        collateral_min_alpha=10.0,
        window_fills=window_fills(address),
    )


def test_worked_example_through_score_day() -> None:
    # two miners, $4M of taker volume each today; A strong and holding, B unskilled and sold out
    strong = [0.015, -0.005, 0.01, 0.02, -0.01] * 12
    flat = [0.01, -0.01] * 30
    # B trades another coin: equal volume, no mirror pattern between the two
    fills = taker_fills(A, 40, 100_000.0) + taker_fills(B, 40, 100_000.0, coin="ETH")
    # states carry a smoothed credit of $1,000 (as in the spec) and B has been paid 100 alpha it no longer holds
    states = {
        "hkA": MinerState(phi_hat=1000.0 / 0.7 - 0.3 / 0.7 * 1000.0, fee_hat=2800.0, g=0.65, paid=(100.0,)),
        "hkB": MinerState(phi_hat=1000.0 / 0.7 - 0.3 / 0.7 * 1000.0, fee_hat=2800.0, g=0.0, paid=(100.0,)),
    }
    inputs = DayInputs(
        day_index=100,
        price_usd=5.33,
        fills=fills,
        states=states,
        miners=[miner("hkA", A, strong, staked=100.0), miner("hkB", B, flat, staked=0.0)],
    )
    res = score_day(inputs, DEFAULT)
    ra, rb = res.records
    assert ra.fees.credit == pytest.approx(1000.0) and rb.fees.credit == pytest.approx(1000.0)
    assert ra.gates_pass and rb.gates_pass
    assert ra.holding == 1.0 and rb.holding == 0.0
    assert ra.skill > 0.9 and rb.skill == 0.0
    assert ra.multiplier == pytest.approx(0.85 + 0.05 * ra.skill + 0.10)
    assert rb.multiplier == pytest.approx(0.85)
    assert rb.pay_alpha / ra.pay_alpha >= 0.85
    assert rb.pay_alpha == pytest.approx(319.0, abs=1.0)
    assert res.allocation.burn_alpha > 1600
    w = res.weights("owner")
    assert sum(w.values()) == pytest.approx(1.0)
    assert sum(to_u16(w).values()) == 65535
    assert res.states["hkA"].paid[-1] == pytest.approx(ra.pay_alpha)


def test_fresh_miner_starts_at_the_floor_and_smooths_in() -> None:
    fills = taker_fills(A, 10, 10_000.0)
    inputs = DayInputs(day_index=0, price_usd=1.0, fills=fills, states={}, miners=[MinerInputs("hk", A)])
    res = score_day(inputs, DEFAULT)
    r = res.records[0]
    assert not r.gates_pass and r.multiplier == 0.85
    assert res.states["hk"].phi_hat == pytest.approx(0.3 * r.fees.credit)
    assert r.pay_alpha <= r.cap_alpha


def test_mirrored_pair_loses_credit_and_is_reported() -> None:
    # A buys, B sells the same size seconds later, all day: mirrored above 50%
    fa = [Fill(DAY0 + i * 60_000, A, "BTC", "B", 100_000.0, 1.0, True, 25.0, counterparty="0x1") for i in range(20)]
    fb = [
        Fill(DAY0 + i * 60_000 + 5_000, B, "BTC", "A", 100_000.0, 1.0, True, 25.0, counterparty="0x2")
        for i in range(20)
    ]
    inputs = DayInputs(
        day_index=5,
        price_usd=1.0,
        fills=fa + fb,
        states={},
        miners=[MinerInputs("hkA", A, staked_min_alpha=1.0), MinerInputs("hkB", B)],
    )
    res = score_day(inputs, DEFAULT)
    for r in res.records:
        assert r.mirrored_share == pytest.approx(1.0)
        assert r.credit_factor == pytest.approx(0.0) and r.reported
        assert r.multiplier == 0.85 and r.pay_alpha == 0.0
    assert res.states["hkA"].is_zeroed(34) and not res.states["hkA"].is_zeroed(35)
    assert res.allocation.burn_alpha == pytest.approx(DEFAULT.miner_pool_alpha)


def test_cluster_self_matches_count_for_nothing() -> None:
    fills = taker_fills(A, 10, 10_000.0, cp=B)
    inputs = DayInputs(
        day_index=0,
        price_usd=1.0,
        fills=fills,
        states={},
        miners=[MinerInputs("hk", A)],
        cluster_of={A: "c", B: "c"},
    )
    res = score_day(inputs, DEFAULT)
    assert res.records[0].fees.fills == 0 and res.records[0].pay_alpha == 0.0
