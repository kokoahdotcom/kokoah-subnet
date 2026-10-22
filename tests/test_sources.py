"""The data plane (spec section 3): the builder file is incomplete, so each bound address's own fill history is the
primary source, merged with the file on the second; counterparties come from shared trade ids."""

import pytest

from kokoah_subnet.mechanism import DEFAULT, Fill, MinerState
from kokoah_subnet.mechanism.day import DayInputs, MinerInputs, score_day
from kokoah_subnet.mechanism.fills import (
    assign_counterparties,
    history_counts,
    match_total_fees,
    merge_sources,
    parse_user_fills,
)

A = "0xaaaa000000000000000000000000000000000001"
B = "0xbbbb000000000000000000000000000000000002"
T0 = 1792454400000 + 15 * 3_600_000  # 2026-10-20 15:00 UTC, after the file's cut
RATE = DEFAULT.builder_fee_rate
KO = "0x6b6f0101" + "ab" * 12


def hist(
    t: int,
    px: float = 100.0,
    sz: float = 10.0,
    bf: float | None = None,
    tid: int = 1,
    cloid: str | None = KO,
    user: str = A,
    side: str = "B",
) -> Fill:
    rec = {
        "time": t,
        "coin": "BTC",
        "side": side,
        "px": str(px),
        "sz": str(sz),
        "crossed": True,
        "fee": str(px * sz * 7e-4),
        "tid": tid,
        "cloid": cloid,
        "closedPnl": "0",
    }
    if bf is not None:
        rec["builderFee"] = str(bf)
    return parse_user_fills(user, [rec])[0]


def filed(t_s: int, px: float = 100.0, sz: float = 10.0, cp: str = "0xcafe") -> Fill:
    return Fill(t_s, A, "BTC", "B", px, sz, True, px * sz * RATE, counterparty=cp)


def test_total_fees_match_on_the_second_and_one_to_one() -> None:
    # the file says 15:00:00 (whole seconds), the history 15:00:00.731; two identical rows match two fills
    f1, f2 = filed(T0), filed(T0)
    h1, h2 = hist(T0 + 731, bf=0.25, tid=1), hist(T0 + 731, bf=0.25, tid=2)
    out = match_total_fees([f1, f2], [h1, h2])
    assert [f.total_fee for f in out] == [pytest.approx(0.7), pytest.approx(0.7)]
    assert match_total_fees([f1, f2], [h1])[1].total_fee is None


def test_merge_takes_history_details_keeps_file_counterparty_and_file_only_fills() -> None:
    in_both = hist(T0 + 731, bf=0.25, tid=7)
    afternoon = hist(T0 + 3_600_000, bf=0.25, tid=8)  # not in the file: the file stops near midday
    beyond = filed(T0 - 7_200_000, cp="0xbeef")  # in the file only (the history's reach is 10,000 fills)
    out = merge_sources([filed(T0, cp="0xfeed"), beyond], [in_both, afternoon], True, RATE)
    by_source = {f.source: f for f in out}
    assert set(by_source) == {"both", "history", "file"}
    assert by_source["both"].counterparty == "0xfeed" and by_source["both"].tid == 7
    assert by_source["both"].total_fee == pytest.approx(0.7) and by_source["both"].time_ms == T0 + 731
    assert by_source["history"].tid == 8 and by_source["file"].counterparty == "0xbeef"
    assert [f.time_ms for f in out] == sorted(f.time_ms for f in out)


def test_which_history_only_fills_count() -> None:
    fee = 100.0 * 10.0 * RATE
    # only our builder approved: every builder fee is ours (up to the configured rate)
    assert history_counts(hist(T0, bf=fee, cloid=None), True, RATE)
    assert history_counts(hist(T0, bf=fee / 2, cloid=None), True, RATE)  # a lower (promotional) fee
    assert not history_counts(hist(T0, bf=fee * 2, cloid=None), True, RATE)  # more than we charge: not ours
    assert not history_counts(hist(T0, bf=None), True, RATE)  # no builder fee: not a builder fill
    # another builder approved too: our order marker and exactly our rate
    assert history_counts(hist(T0, bf=fee), False, RATE)
    assert not history_counts(hist(T0, bf=fee, cloid=None), False, RATE)
    assert not history_counts(hist(T0, bf=fee, cloid="0x12345678" + "00" * 12), False, RATE)
    assert not history_counts(hist(T0, bf=fee * 0.8), False, RATE)


def test_trade_ids_name_the_counterparty() -> None:
    a = hist(T0, bf=0.25, tid=42)
    b = hist(T0, bf=0.25, tid=42, user=B, side="A")
    lone = hist(T0 + 1, bf=0.25, tid=43)
    owners = {42: {A, B}, 43: {A}}
    out = assign_counterparties([a, lone], owners) + assign_counterparties([b], owners)
    assert [f.counterparty for f in out] == [B, "", A]


def test_two_bound_traders_trading_each_other_after_midday_earn_nothing_from_it() -> None:
    fa = [hist(T0 + i * 60_000, bf=0.25, tid=100 + i) for i in range(10)]
    fb = [hist(T0 + i * 60_000, bf=0.25, tid=100 + i, user=B, side="A") for i in range(10)]
    owners = {100 + i: {A, B} for i in range(10)}
    fills = assign_counterparties(fa, owners) + assign_counterparties(fb, owners)
    miners = [MinerInputs("hkA", A, avg_equity_usd=1e6), MinerInputs("hkB", B, avg_equity_usd=1e6)]
    states = {"hkA": MinerState(qualified=True), "hkB": MinerState(qualified=True)}
    res = score_day(DayInputs(day_index=3, price_usd=1.0, fills=fills, miners=miners, states=states), DEFAULT)
    for r in res.records:
        assert r.fees.fills == 0 and r.mirrored_share == pytest.approx(1.0) and r.pay_alpha == 0.0
