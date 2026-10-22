"""The two pools (spec v6): 80% by eligible smoothed builder fees, 20% by skill weight, nothing capped or burned."""

import pytest

from kokoah_subnet.mechanism import DEFAULT, Params, allocate, to_u16, weights

A = DEFAULT.miner_pool_alpha


def test_pools_split_80_20_and_pay_out_everything() -> None:
    alloc = allocate({"a": 300.0, "b": 100.0}, {"a": 0.0, "c": 0.5}, 5.0, DEFAULT)
    assert alloc.volume_pool == pytest.approx(0.8 * A) and alloc.skill_pool == pytest.approx(0.2 * A)
    assert alloc.volume_pays == {"a": pytest.approx(0.6 * A), "b": pytest.approx(0.2 * A)}
    assert alloc.skill_pays == {"c": pytest.approx(0.2 * A)}
    assert alloc.paid_alpha == pytest.approx(A) and alloc.burn_alpha == 0.0


def test_pay_is_not_capped_by_fees() -> None:
    # one miner paid a single dollar of fees: it takes the whole volume pool, however much that is worth
    alloc = allocate({"a": 1.0}, {}, 5.0, DEFAULT)
    assert alloc.pays == {"a": pytest.approx(A)}


def test_an_empty_skill_pool_joins_the_volume_pool_and_the_reverse() -> None:
    assert allocate({"a": 1.0}, {"a": 0.0}, 1.0, DEFAULT).volume_pool == pytest.approx(A)
    only_skill = allocate({}, {"s": 0.3}, 1.0, DEFAULT)
    assert only_skill.skill_pool == pytest.approx(A) and only_skill.pays == {"s": pytest.approx(A)}


def test_nobody_with_weight_goes_to_the_fallback_hotkey() -> None:
    alloc = allocate({"a": 0.0}, {}, 1.0, DEFAULT)
    assert alloc.burn_alpha == pytest.approx(A) and weights(alloc, "burn") == {"burn": 1.0}


@pytest.mark.parametrize("k", [2, 5])
def test_splitting_fees_across_accounts_pays_the_same(k: int) -> None:
    one = allocate({"x": 1000.0, "other": 500.0}, {}, 1.0, DEFAULT).pays["x"]
    split = allocate({**{f"x{i}": 1000.0 / k for i in range(k)}, "other": 500.0}, {}, 1.0, DEFAULT)
    assert sum(v for key, v in split.pays.items() if key.startswith("x")) == pytest.approx(one)


def test_weights_sum_to_one_and_u16_to_65535() -> None:
    w = weights(allocate({"a": 3.0, "b": 1.0}, {"b": 0.2, "c": 0.9}, 1.0, DEFAULT), "burn")
    assert sum(w.values()) == pytest.approx(1.0) and "burn" not in w
    assert sum(to_u16(w).values()) == 65535


def test_params_validation() -> None:
    with pytest.raises(ValueError):
        Params(volume_share=0.7, skill_share=0.2)
    with pytest.raises(ValueError):
        Params(entry_psr=0.4)
    with pytest.raises(ValueError):
        Params(skill_floor=1.0)
    assert Params().volume_share == 0.8 and Params().skill_share == 0.2
