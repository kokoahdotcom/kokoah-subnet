"""Payout: the spec's worked example (section 13), the 15% rule, sybil neutrality, caps, burn and weights."""

import pytest

from kokoah_subnet.mechanism import DEFAULT, MinerDay, Params, allocate, cap_usd, multiplier, to_u16, weights

PRICE = 5.33


def test_worked_example_young_subnet() -> None:
    a = MinerDay("A", phi_hat=1000.0, fee_hat=2800.0, multiplier=1.0)
    b = MinerDay("B", phi_hat=1000.0, fee_hat=2800.0, multiplier=0.85)
    alloc = allocate([a, b], PRICE, DEFAULT)
    assert round(alloc.shares["A"]) == 1284
    assert round(alloc.shares["B"]) == 1092
    assert round(alloc.caps["A"]) == 375
    assert round(alloc.caps["B"]) == 319
    assert alloc.pays["A"] == alloc.caps["A"] and alloc.pays["B"] == alloc.caps["B"]
    assert round(alloc.burn_alpha) == 1682
    assert alloc.pays["A"] * PRICE == pytest.approx(2000.0)
    assert alloc.pays["B"] * PRICE == pytest.approx(1700.0)


def test_busy_subnet_pays_shares_and_burns_nothing() -> None:
    miners = [MinerDay(f"m{i}", 8770.0, 24556.0, 0.9) for i in range(10)]
    alloc = allocate(miners, PRICE, DEFAULT)
    assert alloc.burn_alpha == pytest.approx(0.0)
    assert sum(alloc.pays.values()) == pytest.approx(DEFAULT.miner_pool_alpha)
    assert all(p == pytest.approx(DEFAULT.miner_pool_alpha / 10) for p in alloc.pays.values())


@pytest.mark.parametrize("price", [5.33, 0.02])
def test_fifteen_percent_rule_in_both_regimes(price: float) -> None:
    best = MinerDay("best", 1000.0, 2800.0, multiplier(1.0, 1.0, True, DEFAULT))
    worst = MinerDay("worst", 1000.0, 2800.0, multiplier(0.0, 0.0, True, DEFAULT))
    alloc = allocate([best, worst], price, DEFAULT)
    assert alloc.pays["worst"] / alloc.pays["best"] == pytest.approx(0.85)


@pytest.mark.parametrize("price", [5.33, 0.02])
@pytest.mark.parametrize("k", [2, 5])
def test_splitting_an_account_pays_the_same(price: float, k: int) -> None:
    others = [MinerDay("o1", 500.0, 1400.0, 0.9), MinerDay("o2", 2000.0, 5600.0, 0.95)]
    whole = allocate([MinerDay("w", 1000.0, 2800.0, 0.9), *others], price, DEFAULT).pays["w"]
    parts = [MinerDay(f"p{i}", 1000.0 / k, 2800.0 / k, 0.9) for i in range(k)]
    split = allocate([*parts, *others], price, DEFAULT)
    assert sum(split.pays[p.hotkey] for p in parts) == pytest.approx(whole)


def test_second_cap_binds_for_low_fee_accounts() -> None:
    # tier-6 taker with the Diamond discount: 1.44 bps a side on $4M = $576 exchange fee + $1,000 builder fee
    whale = MinerDay("whale", 1000.0, 1576.0, 1.0)
    assert cap_usd(whale, DEFAULT) == pytest.approx(0.75 * 1576.0)
    # a rebated market maker: total fee below the builder fee, cap lower still
    mm = MinerDay("mm", 400.0, 880.0, 1.0)
    assert cap_usd(mm, DEFAULT) == pytest.approx(min(800.0, 660.0))


def test_multiplier_bounds_and_gate() -> None:
    assert multiplier(0.0, 0.0, True, DEFAULT) == pytest.approx(0.85)
    assert multiplier(1.0, 1.0, True, DEFAULT) == pytest.approx(1.0)
    assert multiplier(0.5, 1.0, True, DEFAULT) == pytest.approx(0.975)
    assert multiplier(1.0, 1.0, False, DEFAULT) == pytest.approx(0.85)
    assert multiplier(7.0, -3.0, True, DEFAULT) == pytest.approx(0.90)


def test_weights_sum_to_one_and_u16_to_65535() -> None:
    a = MinerDay("A", 1000.0, 2800.0, 1.0)
    b = MinerDay("B", 1000.0, 2800.0, 0.85)
    alloc = allocate([a, b], PRICE, DEFAULT)
    w = weights(alloc, "owner")
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["owner"] == pytest.approx(alloc.burn_alpha / alloc.pool_alpha)
    u = to_u16(w)
    assert sum(u.values()) == 65535
    assert set(u) == {"A", "B", "owner"}


def test_no_activity_burns_everything() -> None:
    alloc = allocate([MinerDay("A", 0.0, 0.0, 0.85)], PRICE, DEFAULT)
    assert alloc.burn_alpha == pytest.approx(DEFAULT.miner_pool_alpha)
    assert weights(alloc, "owner") == {"owner": pytest.approx(1.0)}


def test_bad_price_is_refused() -> None:
    with pytest.raises(ValueError):
        allocate([MinerDay("A", 1.0, 1.0, 1.0)], 0.0, DEFAULT)


def test_params_validation() -> None:
    assert DEFAULT.c_tot_limit == pytest.approx(0.8636, abs=1e-3)
    with pytest.raises(ValueError):
        Params(c_tot=0.9)
    with pytest.raises(ValueError):
        Params(m_min=0.8)
    assert Params(c_tot=0.6).c_tot == 0.6
