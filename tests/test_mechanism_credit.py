"""Fee credit, smoothing, fills parsing and matching."""

import pytest

from kokoah_subnet.mechanism import (
    DEFAULT,
    Fill,
    MinerState,
    Params,
    advance,
    by_user,
    day_fees,
    ema,
    match_total_fees,
    parse_builder_csv,
    parse_user_fills,
)

CSV = """time,user,coin,side,px,sz,crossed,special_trade_type,tif,is_trigger,counterparty,closed_pnl,twap_id,builder_fee
1792454400000,0xAAAA000000000000000000000000000000000001,BTC,B,100000,0.1,True,,Ioc,False,0xbbbb000000000000000000000000000000000002,0,,2.5
1792454460000,0xaaaa000000000000000000000000000000000001,BTC,A,100100,0.1,False,,Gtc,False,0xcccc000000000000000000000000000000000003,10,7,2.5025
"""


def test_parse_builder_csv() -> None:
    fills = parse_builder_csv(CSV)
    assert len(fills) == 2
    f0, f1 = fills
    assert f0.user == "0xaaaa000000000000000000000000000000000001"
    assert f0.counterparty.startswith("0xbbbb")
    assert f0.crossed and not f1.crossed
    assert f0.notional == pytest.approx(10_000.0)
    assert f1.twap_id == 7 and f1.closed_pnl == 10.0
    assert f0.day == "2026-10-20"
    assert f0.signed_size == pytest.approx(0.1) and f1.signed_size == pytest.approx(-0.1)


def test_parse_rejects_other_headers() -> None:
    with pytest.raises(ValueError):
        parse_builder_csv("time,user\n1,2\n")


def test_day_fees_maker_and_taker_credit_alike() -> None:
    fills = parse_builder_csv(CSV)
    fees = day_fees(fills, DEFAULT)
    assert fees.builder_fee == pytest.approx(5.0025)
    assert fees.credit == pytest.approx(5.0025)  # the builder fee is charged on both, so both count in full
    assert day_fees(fills, Params(maker_credit=0.4)).credit == pytest.approx(2.5 + 0.4 * 2.5025)
    assert fees.total_fee == pytest.approx(5.0025)  # unknown total fee counts the builder fee only
    assert fees.taker_notional == pytest.approx(10_000.0)
    assert fees.fills == 2
    assert fees.closed_pnl == pytest.approx(10.0)  # published only


def test_total_fee_fallback_rates() -> None:
    fills = parse_builder_csv(CSV)
    fees = day_fees(fills, DEFAULT, fallback_rates_bps=(4.5, 1.5))
    assert fees.total_fee == pytest.approx(5.0025 + 10_000 * 4.5 / 1e4 + 10_010 * 1.5 / 1e4)


def test_match_total_fees_from_public_fills() -> None:
    fills = parse_builder_csv(CSV)
    public = parse_user_fills(
        "0xAAAA000000000000000000000000000000000001",
        [
            {
                "coin": "BTC",
                "px": "100000",
                "sz": "0.1",
                "side": "B",
                "time": 1792454400000,
                "crossed": True,
                "fee": "7.0",
                "builderFee": "2.5",
                "closedPnl": "0",
            },
        ],
    )
    matched = match_total_fees(fills, public)
    assert matched[0].total_fee == 7.0
    assert matched[1].total_fee is None
    assert day_fees(matched, DEFAULT).total_fee == pytest.approx(7.0 + 2.5025)


def test_ema_counts_every_dollar_once() -> None:
    # one day of $100 credit, then nothing: the smoothed values over time sum to $100
    x = 0.0
    total = 0.0
    for day in range(400):
        x = ema(x, 100.0 if day == 0 else 0.0, DEFAULT)
        total += x
    assert total == pytest.approx(100.0, rel=1e-6)
    assert ema(0.0, 100.0, DEFAULT) == pytest.approx(30.0)


def test_state_advance_and_holding_window() -> None:
    s = MinerState()
    fees = day_fees(parse_builder_csv(CSV), DEFAULT)
    for d in range(70):
        s = advance(s, fees, g_raw=0.5, paid_alpha=float(d), params=DEFAULT)
    assert len(s.paid) == DEFAULT.holding_days
    assert s.paid[-1] == 69.0 and s.paid[0] == 10.0
    assert s.phi_hat == pytest.approx(fees.credit, rel=1e-6)
    assert s.g == pytest.approx(0.5, rel=1e-6)
    s = advance(s, fees, 0.5, 1.0, DEFAULT, credit_factor=0.0, zero_from_day=70)
    assert s.is_zeroed(99) and not s.is_zeroed(100)


def test_by_user_groups_lowercase() -> None:
    groups = by_user(parse_builder_csv(CSV))
    assert list(groups) == ["0xaaaa000000000000000000000000000000000001"]
    assert len(groups["0xaaaa000000000000000000000000000000000001"]) == 2


def test_fill_defaults() -> None:
    f = Fill(0, "0xa", "ETH", "A", 2000.0, 2.0, True, 1.0)
    assert f.counterparty == "" and f.total_fee is None and f.notional == 4000.0
