"""The SDK's unit-tagged money: TAO and a subnet's alpha never mix silently."""

import bittensor as bt
import pytest
from bittensor.balance import UnitMismatchError


def test_tao_and_alpha_are_distinct_units() -> None:
    tao = bt.Balance.from_tao("1.5")
    alpha = bt.Balance.from_alpha("2.5", netuid=42)
    assert str(tao.tao) == "1.5"
    with pytest.raises(UnitMismatchError):
        _ = alpha.tao
