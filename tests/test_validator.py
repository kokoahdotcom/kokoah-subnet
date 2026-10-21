"""Validator pieces that run offline: the builder file loader, the info client's parsing, the runner, the CLI."""

import json
from pathlib import Path

import httpx
import lz4.frame
import pytest

from kokoah_subnet.cli import build_parser, main
from kokoah_subnet.mechanism import DEFAULT, MinerState
from kokoah_subnet.validator.builder_feed import NotPublished, decompress, file_url, load_file, sha256
from kokoah_subnet.validator.chain import sample_blocks, weights_by_uid
from kokoah_subnet.validator.hyperliquid import HyperliquidInfo, day_bounds_ms
from kokoah_subnet.validator.run import dump_states, load_states

CSV = (
    "time,user,coin,side,px,sz,crossed,special_trade_type,tif,is_trigger,counterparty,closed_pnl,twap_id,builder_fee\n"
    "1792454400000,0xaaaa000000000000000000000000000000000001,BTC,B,100000,0.1,True,,Ioc,False,0xcafe,0,,2.5\n"
    "1792454460000,0xaaaa000000000000000000000000000000000001,BTC,A,100000,0.1,True,,Ioc,False,0xcafe,0,,2.5\n"
)


def test_builder_file_round_trip(tmp_path: Path) -> None:
    data = lz4.frame.compress(CSV.encode())
    assert decompress(data) == CSV
    path = tmp_path / "20261020.csv.lz4"
    path.write_bytes(data)
    fills = load_file(path)
    assert len(fills) == 2 and fills[0].builder_fee == 2.5
    plain = tmp_path / "20261020.csv"
    plain.write_text(CSV)
    assert len(load_file(plain)) == 2
    assert len(sha256(data)) == 64


def test_file_url_is_lowercase_and_dateless() -> None:
    assert (
        file_url("0xABC", "2026-10-20")
        == "https://stats-data.hyperliquid.xyz/Mainnet/builder_fills/0xabc/20261020.csv.lz4"
    )
    assert issubclass(NotPublished, Exception)


def test_day_bounds() -> None:
    s, e = day_bounds_ms("2026-10-20")
    assert s == 1792454400000 and e - s == 86_400_000


async def test_info_client_parses_fills_flows_and_fees() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["type"] == "userFillsByTime":
            return httpx.Response(
                200,
                json=[
                    {
                        "coin": "BTC",
                        "px": "100000",
                        "sz": "0.1",
                        "side": "B",
                        "time": body["startTime"] + 1,
                        "crossed": True,
                        "fee": "7.0",
                        "builderFee": "2.5",
                        "closedPnl": "0",
                    },
                    {
                        "coin": "BTC",
                        "px": "100000",
                        "sz": "0.1",
                        "side": "A",
                        "time": body["endTime"] + 5,
                        "crossed": True,
                        "fee": "4.5",
                        "closedPnl": "1",
                    },
                ],
            )
        if body["type"] == "clearinghouseState":
            return httpx.Response(200, json={"marginSummary": {"accountValue": "12345.5"}})
        if body["type"] == "userNonFundingLedgerUpdates":
            return httpx.Response(
                200,
                json=[
                    {"delta": {"type": "deposit", "usdc": "1000"}},
                    {"delta": {"type": "withdraw", "usdc": "250"}},
                    {"delta": {"type": "funding", "usdc": "3"}},
                ],
            )
        if body["type"] == "userFees":
            return httpx.Response(200, json={"userCrossRate": "0.00045", "userAddRate": "0.00015"})
        if body["type"] == "metaAndAssetCtxs":
            return httpx.Response(200, json=[{"universe": [{"name": "BTC"}]}, [{"markPx": "100500"}]])
        return httpx.Response(400)

    client = HyperliquidInfo(rate_limit_s=0.0)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    start, end = day_bounds_ms("2026-10-20")
    fills = await client.fills_by_time("0xAAAA", start, end)
    assert len(fills) == 1 and fills[0].total_fee == 7.0 and fills[0].user == "0xaaaa"
    assert await client.account_value("0xaaaa") == 12345.5
    assert await client.net_flows("0xaaaa", start, end) == 750.0
    taker, maker = await client.fee_rates_bps("0xaaaa")
    assert taker == pytest.approx(4.5) and maker == pytest.approx(1.5)
    assert await client.mark_prices() == {"BTC": 100500.0}
    await client.close()


async def test_info_client_retries_then_fails() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    client = HyperliquidInfo(rate_limit_s=0.0)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        await client.post({"type": "meta"}, attempts=2)
    assert calls["n"] == 2
    await client.close()


def test_sample_blocks_and_weights_by_uid() -> None:
    assert sample_blocks(1000, 360, 3) == [280, 640, 1000]
    assert sample_blocks(100, 360, 2) == [1, 100]
    uids, values = weights_by_uid({"a": 0.5, "owner": 0.5, "ghost": 0.1}, {"a": 3, "owner": 0})
    assert uids == [0, 3] and sorted(values) == [32767, 32768]


def test_states_round_trip(tmp_path: Path) -> None:
    states = {
        "hk": MinerState(
            phi_hat=1.5,
            fee_hat=2.0,
            g=0.1,
            paid=(1.0, 2.0),
            zero_until_day=9,
            paid_days=((8, 1.0), (9, 2.0)),
            last_day=9,
        )
    }
    path = tmp_path / "state.json"
    path.write_text(dump_states(states))
    assert load_states(path) == states
    assert load_states(None) == {}


def test_score_day_command(tmp_path: Path) -> None:
    fills = tmp_path / "fills.csv"
    fills.write_text(CSV)
    miners = tmp_path / "miners.json"
    miners.write_text(
        json.dumps([{"hotkey": "hkA", "address": "0xAAAA000000000000000000000000000000000001", "staked_min_alpha": 5}])
    )
    state = tmp_path / "state.json"
    out = tmp_path / "out.json"
    code = main(
        [
            "score-day",
            "--fills",
            str(fills),
            "--miners",
            str(miners),
            "--state",
            str(state),
            "--price-usd",
            "1.0",
            "--day-index",
            "3",
            "--burn-hotkey",
            "own",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    result = json.loads(out.read_text())
    assert result["day_index"] == 3
    assert result["miners"][0]["fees"]["credit"] == pytest.approx(5.0)
    assert result["weights"]["own"] == pytest.approx(result["burn_alpha"] / DEFAULT.miner_pool_alpha)
    saved = load_states(state)
    assert saved["hkA"].phi_hat == pytest.approx(1.5)


def test_cli_parses_new_commands() -> None:
    args = build_parser().parse_args(["verify"])
    assert args.command == "verify"


async def test_rate_limiter_enforces_the_budget() -> None:
    import time

    from kokoah_subnet.validator.hyperliquid import RateLimiter

    limiter = RateLimiter(budget_per_minute=50)
    t0 = time.monotonic()
    for _ in range(2):
        await limiter.acquire(20)  # 40 of 50 used at once
    assert time.monotonic() - t0 < 0.5
    # the third request of weight 20 does not fit in the minute; the limiter waits (we only check it does not pass)
    import asyncio

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire(20), timeout=0.3)
    await limiter.acquire(10)  # 50 of 50 fits


def test_migrations_record_their_version(tmp_path: Path) -> None:
    from kokoah_subnet.validator.migrations import STEPS, current_version
    from kokoah_subnet.validator.store import Store

    s = Store(tmp_path / "m.sqlite")
    assert len(s.migrations_applied) == len(STEPS)
    with s._engine.connect() as c:
        assert current_version(c) == STEPS[-1][0]
    again = Store(tmp_path / "m.sqlite")
    assert again.migrations_applied == []


def test_daily_equity_from_portfolio() -> None:
    from kokoah_subnet.validator.hyperliquid import daily_equity_from_portfolio

    day0 = 1792454400000
    windows = [
        ["day", {"accountValueHistory": [[day0, "1"]], "pnlHistory": [[day0, "0"]]}],
        [
            "perpMonth",
            {
                "accountValueHistory": [[day0, "1000"], [day0 + 86_400_000, "1100"], [day0 + 2 * 86_400_000, "1600"]],
                "pnlHistory": [[day0, "0"], [day0 + 86_400_000, "100"], [day0 + 2 * 86_400_000, "100"]],
            },
        ],
    ]
    rows = daily_equity_from_portfolio(windows)
    assert [r[0] for r in rows] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert rows[1][:2] == ("2026-10-21", 1100.0) and rows[1][2] == pytest.approx(0.0)  # +100 value, +100 pnl
    assert rows[2][:2] == ("2026-10-22", 1600.0) and rows[2][2] == pytest.approx(500.0)  # +500 value, no pnl
    assert daily_equity_from_portfolio([["week", {}]]) == []
