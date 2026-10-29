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
    s.put_neurons("2026-10-21", 100, [{"uid": 1, "hotkey": "hkOld", "coldkey": "ck", "block_at_registration": 7}])
    s.put_neurons("2026-10-22", 200, [{"uid": 1, "hotkey": "hkNew", "coldkey": "ck", "block_at_registration": 7}])
    assert s.latest_neurons() == [{"uid": 1, "hotkey": "hkNew", "coldkey": "ck", "block_at_registration": 7}]
    # the snapshot before a day, whatever was recorded for that day itself (a retried day)
    assert s.latest_neurons(before_day="2026-10-22")[0]["hotkey"] == "hkOld"
    assert s.latest_neurons(before_day="2026-10-21") == []
    s.put_swap("2026-10-21", 1, "hkOld", "hkNew", "ck")
    s.put_swap("2026-10-23", 2, "hkA", "hkB", "ck2")
    assert s.swaps_through("2026-10-22") == [("2026-10-21", "hkOld", "hkNew")]
    assert len(s.swaps_through("2026-10-23")) == 2


def test_an_accounts_record_is_the_series_the_validator_scores_on(tmp_path: Path) -> None:
    """Returns over consecutive known days only (a day the exchange did not record has none, nor the day after it),
    the growth of 1 carried over such days, the worst fall from a peak, and the scored days' figures."""
    s = Store(tmp_path / "r.sqlite")
    a = "0x" + "aa" * 20
    s.put_equity(a, "2026-10-01", 1000.0, 0.0)
    s.put_equity(a, "2026-10-02", 1100.0, 0.0)  # +10%
    s.put_equity(a, "2026-10-03", 990.0, 0.0)  # -10%
    s.put_equity(a, "2026-10-04", 990.0, 0.0, known=False)  # not recorded: no return
    s.put_equity(a, "2026-10-05", 1089.0, 0.0)  # follows an unknown day: no return
    s.put_equity(a, "2026-10-06", 1197.9, 0.0)  # +10%
    assert s.account_record(a) is None  # never scored
    for day, agent in (("2026-10-05", False), ("2026-10-06", True)):
        s.put_miner_days(day, [{"hotkey": "hk", "address": a, "notional": 5000.0, "fills": 3, "pay_alpha": 1.5,
                                "eligible": True, "agent_verified": agent, "avg_equity_usd": 1100.0}])  # fmt: skip
    rec = s.account_record(a.upper().replace("0X", "0x"))
    assert rec is not None and rec["hotkey"] == "hk" and rec["linked"] is False
    r = rec["record"]
    assert r["record_days"] == 3 and r["return_days_90d"] == 3 and r["losing_days_90d"] == 1 and r["agent_days"] == 1
    assert r["return_90d"] == pytest.approx(0.089) and r["return_all"] == pytest.approx(0.089)
    assert r["worst_drop_90d"] == pytest.approx(0.1) and r["worst_drop_all"] == pytest.approx(0.1)
    assert (r["first_day"], r["last_day"]) == ("2026-10-01", "2026-10-06")
    days = rec["days"]
    assert [d["return"] for d in days] == [
        None,
        pytest.approx(0.1),
        pytest.approx(-0.1),
        None,
        None,
        pytest.approx(0.1),
    ]
    assert [round(d["index"], 4) for d in days] == [1.0, 1.1, 0.99, 0.99, 0.99, 1.089]
    assert [d["scored"] for d in days] == [False] * 4 + [True] * 2 and days[0]["hotkey"] is None
    assert days[-1] == {"day": "2026-10-06", "scored": True, "hotkey": "hk", "return": pytest.approx(0.1),
                        "index": pytest.approx(1.089), "avg_equity_usd": 1100.0, "volume_usd": 5000.0, "fills": 3,
                        "pay_alpha": 1.5, "eligible": True, "agent_verified": True}  # fmt: skip
    assert "days" not in (s.account_record(a, with_days=False) or {})


def test_a_record_without_known_returns_says_so_with_nulls(tmp_path: Path) -> None:
    s = Store(tmp_path / "n.sqlite")
    a = "0x" + "bb" * 20
    s.put_miner_days("2026-10-05", [{"hotkey": "hk", "address": a}])
    r = (s.account_record(a) or {})["record"]
    assert r["record_days"] == 0 and r["return_90d"] is None and r["worst_drop_90d"] is None
    assert r["losing_days_90d"] is None and r["return_days_90d"] is None and r["return_all"] is None
