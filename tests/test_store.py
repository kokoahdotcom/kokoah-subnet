"""The validator's store: series, windows, states, bindings, days."""

from pathlib import Path

import pytest

from kokoah_subnet.binding import AcceptedBinding
from kokoah_subnet.mechanism import MinerState
from kokoah_subnet.validator.store import Store


def test_equity_returns_skip_gaps_and_remove_flows(tmp_path: Path) -> None:
    s = Store(tmp_path / "v.sqlite")
    s.put_equity("0xA", "2026-10-01", 1000.0, 0.0)
    s.put_equity("0xA", "2026-10-02", 1100.0, 50.0)
    s.put_equity("0xA", "2026-10-04", 1200.0, 0.0)  # 10-03 missing: no return across the gap
    s.put_equity("0xA", "2026-10-05", 1140.0, 0.0)
    assert s.returns("0xa", 60) == pytest.approx([0.05, -0.05])
    assert s.mean_equity("0xA", 30) == pytest.approx((1000 + 1100 + 1200 + 1140) / 4)
    assert s.equity_series("0xA", 2) == [("2026-10-04", 1200.0, 0.0), ("2026-10-05", 1140.0, 0.0)]


def test_positions_aligned_over_days(tmp_path: Path) -> None:
    s = Store(tmp_path / "v.sqlite")
    s.put_positions("0xA", "2026-10-01", {"BTC": 1000.0, "ETH": -500.0})
    s.put_positions("0xA", "2026-10-03", {"BTC": 800.0})
    s.put_positions("0xB", "2026-10-02", {"BTC": -1000.0, "SOL": 0.0})
    series = s.position_series(["0xA", "0xB"], ["2026-10-01", "2026-10-02", "2026-10-03"])
    assert series["0xa"]["BTC"] == [1000.0, 0.0, 800.0]
    assert series["0xa"]["ETH"] == [-500.0, 0.0, 0.0]
    assert series["0xb"] == {"BTC": [0.0, -1000.0, 0.0]}


def test_activity_window(tmp_path: Path) -> None:
    s = Store(tmp_path / "v.sqlite")
    for d in range(40):
        s.put_activity("0xA", f"2026-{(d // 28) + 8:02d}-{(d % 28) + 1:02d}", 1, 2 if d % 2 == 0 else 0, 100.0)
    rt, active, notional = s.activity_window("0xA", 30)
    assert rt == 30 and active == 15 and notional == 3000.0


def test_states_bindings_days_meta(tmp_path: Path) -> None:
    s = Store(tmp_path / "v.sqlite")
    states = {
        "hk": MinerState(
            phi_hat=1.0,
            fee_hat=2.0,
            g=0.3,
            paid=(1.0, 2.0),
            zero_until_day=5,
            paid_days=((4, 1.0), (5, 2.0)),
            last_day=5,
        )
    }
    s.put_states(states)
    assert s.states() == states
    b = {"hk": AcceptedBinding("hk", "0xa", 10, 5)}
    s.put_bindings(b)
    assert s.bindings() == b
    s.put_bindings({})
    assert s.bindings() == {}
    assert s.latest_day() is None
    s.put_day("2026-10-20", 1, '{"x": 1}')
    s.put_day("2026-10-21", 2, '{"x": 2}')
    assert s.latest_day() == ("2026-10-21", 2, '{"x": 2}')
    assert s.day("2026-10-20") == '{"x": 1}' and s.day("2026-10-19") is None
    assert s.days_list() == ["2026-10-20", "2026-10-21"]
    s.put_file("2026-10-20", "ab" * 32, 10)
    assert s.file("2026-10-20") == ("ab" * 32, 10)
    assert s.get_meta("k") is None and s.get_meta("k", "d") == "d"
    s.set_meta("k", "v")
    assert s.get_meta("k") == "v"
    s.close()


def test_penalties_swaps_and_latest_neurons(tmp_path: Path) -> None:
    s = Store(tmp_path / "v.sqlite")
    s.put_penalty("0xA", 40, "2026-10-21", "hk", "mirrored")
    s.put_penalty("0xA", 30, "2026-10-21", "hk", "shorter one is ignored")
    assert s.penalties(10) == {"0xa": 40} and s.penalties(40) == {}
    s.put_neurons("2026-10-21", 100, [{"uid": 1, "hotkey": "hkOld", "coldkey": "ck"}])
    assert s.latest_neurons() == [{"uid": 1, "hotkey": "hkOld", "coldkey": "ck"}]
    s.put_states({"hkOld": MinerState(phi_hat=7.0)})
    assert s.carry_state("hkOld", "hkNew") and set(s.states()) == {"hkNew"} and s.states()["hkNew"].phi_hat == 7.0
    assert not s.carry_state("nobody", "hkX")
