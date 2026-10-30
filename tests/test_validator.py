"""Validator pieces that run offline: the builder file loader, the info client's parsing, the runner, the CLI."""

import json
from pathlib import Path

import httpx
import lz4.frame
import pytest

from kokoah_subnet.cli import build_parser, main
from kokoah_subnet.mechanism import MinerState
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

    client = HyperliquidInfo(rate_limit_s=0.0, backoff_s=0.0)
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


async def test_fill_history_paging_keeps_fills_at_the_page_boundary() -> None:
    """Pages of four: the first ends inside three fills at one millisecond, so the next starts there again; every
    fill is read once. A page made of one millisecond only moves on to the next one."""
    times = [1, 2, 3, 5, 5, 5, 7, 9, 9, 9, 9, 9, 10]
    records = [
        {"coin": "BTC", "px": "100", "sz": "1", "side": "B", "time": t, "crossed": True, "fee": "1", "tid": i}
        for i, t in enumerate(times)
    ]
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        starts.append(body["startTime"])
        page = [r for r in records if body["startTime"] <= r["time"] < body["endTime"]][:4]
        return httpx.Response(200, json=page)

    client = HyperliquidInfo(rate_limit_s=0.0, page_size=4, backoff_s=0.0)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gaps: list[int] = []
    fills = await client.fills_by_time("0xaaaa", 0, 11, gaps)
    got = sorted(f.tid for f in fills if f.tid is not None)
    # the five fills at t=9 do not fit in one page of four: the fifth is the one a page cannot reach, and is reported
    assert got == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12] and len(fills) == len(got)
    assert starts == [0, 5, 7, 9, 10] and gaps == [9]
    await client.close()


def test_linked_reads_put_sub_accounts_first_then_the_largest_transfers() -> None:
    from kokoah_subnet.validator.hyperliquid import linked_reads

    partners = {"0x01": 0.01, "0x02": 0.01, "0xbig": 5000.0, "0xsub": 3.0}
    assert linked_reads({"0xsub"}, partners, 3) == (["0xsub", "0xbig", "0x01"], 1)
    assert linked_reads(set(), {}, 25) == ([], 0)


async def test_transfer_partners_are_ranked_by_usd_never_by_a_token_count() -> None:
    """A billion units of a worthless token sent (`usdcValue` 0) moved no USD, so it cannot outrank a real transfer
    partner in the linked reads."""
    from kokoah_subnet.validator.hyperliquid import linked_reads, transfer_partners

    me = "0x" + "aa" * 20

    def send(to: str, token: str, amount: str, usd: str | None) -> dict[str, object]:
        delta = {"type": "send", "user": me, "destination": to, "token": token, "amount": amount}
        return {"time": 1, "delta": delta | ({"usdcValue": usd} if usd is not None else {})}

    updates = [
        send("0xdust", "DUST:0x01", "1000000000", "0"),
        send("0xreal", "USDC", "5000", "5000"),
        send("0xold", "USDC", "300", None),  # no USD value given: the USDC amount is one
        send("0xhype", "HYPE:0x02", "10", "400"),
        send("0xother", "PURR:0x03", "70000", None),  # no USD value, not USDC: nothing known to have moved
        {"time": 2, "delta": {"type": "internalTransfer", "user": me, "destination": "0xint", "usdc": "50"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=updates)

    client = HyperliquidInfo(rate_limit_s=0.0, backoff_s=0.0)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    partners = await transfer_partners(client, me, 0, 10)
    assert partners == {"0xdust": 0.0, "0xreal": 5000.0, "0xold": 300.0, "0xhype": 400.0, "0xother": 0.0, "0xint": 50.0}
    assert linked_reads(set(), partners, 2) == (["0xreal", "0xhype"], 4)
    await client.close()


async def test_info_client_retries_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from kokoah_subnet.validator import hyperliquid

    calls = {"n": 0}
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    async def wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(f"{hyperliquid.__name__}.asyncio.sleep", wait)
    client = HyperliquidInfo(rate_limit_s=0.0)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        await client.post({"type": "meta"}, attempts=3)
    assert calls["n"] == 3
    assert waits == [0.5, 1.0]  # doubling waits between attempts, none after the last
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
        json.dumps(
            [
                {
                    "hotkey": "hkA",
                    "address": "0xAAAA000000000000000000000000000000000001",
                    "staked_min_alpha": 5,
                    "avg_equity_usd": 20_000,
                }
            ]
        )
    )
    state = tmp_path / "state.json"
    state.write_text(dump_states({"hkA": MinerState(qualified=True)}))  # an established miner
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
    assert result["weights"] == {"hkA": pytest.approx(1.0)} and result["burn_alpha"] == 0.0  # the whole pool
    saved = load_states(state)
    assert saved["hkA"].phi_hat == pytest.approx(1.5)


def test_cli_parses_new_commands() -> None:
    args = build_parser().parse_args(["verify"])
    assert args.command == "verify"


def test_verify_runs_the_checks_shipped_in_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    from kokoah_subnet.mechanism import verify

    monkeypatch.setattr(verify, "main", lambda: 1)  # a failed check: the command's exit code says so
    assert main(["verify"]) == 1


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


def test_a_sqlite_record_from_before_the_note_level_gets_the_column(tmp_path: Path) -> None:
    from sqlalchemy import text

    from kokoah_subnet.validator.store import Store

    path = tmp_path / "trial.sqlite"
    old = Store(path)
    with old._engine.begin() as c:  # a trial record from before step 11: no errors.level
        c.execute(text("ALTER TABLE errors DROP COLUMN level"))
        c.execute(text("UPDATE meta SET value = '10' WHERE key = 'schema_version'"))
    old.close()
    s = Store(path)
    assert s.migrations_applied[0] == "11: errors.level"
    s.put_error("2026-10-21", None, None, "loop", "x")
    s.put_note("2026-10-21", None, None, "builder", "y")
    assert s.execute_sql("select level from errors order by id") == [{"level": "error"}, {"level": "note"}]
    assert Store(path).migrations_applied == []


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


def test_marks_and_off_market_transfers() -> None:
    from kokoah_subnet.validator.hyperliquid import flows_from_updates, mark_by_minute, mean_close, off_market_transfer

    candles = [{"t": 1792454400000, "c": "100"}, {"t": 1792454460000, "c": "102"}, {"t": 1792454520000, "c": "bad"}]
    marks = mark_by_minute(candles)
    assert marks == {1792454400000: 100.0, 1792454460000: 102.0}
    assert mean_close(candles[:2]) == 101.0
    # buying 2 coins at 90 when the mark is 100: a 20-dollar gift; within the 0.5% band nothing counts
    assert off_market_transfer(90.0, 100.0, 2.0, "B", 0.005) == pytest.approx(20.0)
    assert off_market_transfer(110.0, 100.0, 2.0, "A", 0.005) == pytest.approx(20.0)
    assert off_market_transfer(110.0, 100.0, 2.0, "B", 0.005) == pytest.approx(-20.0)
    assert off_market_transfer(100.4, 100.0, 2.0, "B", 0.005) == 0.0
    # vault moves are flows, not profit or loss
    updates = [
        {"delta": {"type": "vaultDeposit", "usdc": "500"}},
        {"delta": {"type": "vaultWithdraw", "requestedUsd": "200", "netWithdrawnUsd": "200"}},  # the real format
        {"delta": {"type": "deposit", "usdc": "1000"}},
    ]
    assert flows_from_updates("0xa", updates) == pytest.approx(700.0)


def test_day_sample_blocks_agree_between_validators() -> None:
    from kokoah_subnet.validator.chain import day_sample_blocks

    day_start = 1792454400.0
    a = day_sample_blocks(
        now_block=1_000_000, now_ts=day_start + 86_400 + 600, day_start_ts=day_start, tempo=360, samples=20
    )
    # a second validator scoring 20 minutes later, 100 blocks on (consistent with 12 s blocks)
    b = day_sample_blocks(
        now_block=1_000_100, now_ts=day_start + 86_400 + 1800, day_start_ts=day_start, tempo=360, samples=20
    )
    assert a == b and len(a) == 20 and a[-1] < 1_000_000 and a[0] % 360 == 0
    assert all(y - x == 360 for x, y in zip(a, a[1:], strict=False))


def test_states_before_a_rescore(tmp_path: Path) -> None:
    from kokoah_subnet.mechanism import MinerState
    from kokoah_subnet.validator.store import Store

    s = Store(tmp_path / "r.sqlite")
    assert s.states_before("2026-10-21") == {}
    s.put_states({"hk": MinerState(phi_hat=10.0, paid_days=((19, 1.0),))}, day="2026-10-20")
    s.put_states({"hk": MinerState(phi_hat=13.0, paid_days=((19, 1.0), (20, 2.0)))}, day="2026-10-21")
    # scoring 10-22 starts from the 10-21 state; re-scoring 10-21 starts from the 10-20 state; 10-20 from nothing
    assert s.states_before("2026-10-22")["hk"].phi_hat == 13.0
    assert s.states_before("2026-10-21")["hk"].phi_hat == 10.0 and s.states_before("2026-10-21")["hk"].paid_days == (
        (19, 1.0),
    )
    assert s.states_before("2026-10-20") == {}


def test_a_report_never_reaches_an_address_through_a_transfer_it_received() -> None:
    """Miner A sends $1 to a competitor V, then gets reported: V is untouched. A's own sub-account and the address
    of A's other hotkey on the same coldkey carry the report."""
    from kokoah_subnet.validator.clusters import penalty_reach

    edges = [
        ("0xa", "0xv", "transfer"),  # A's $1 to the victim
        ("0xa", "0xsub", "subaccount"),
        ("0xa", "0xsame", "coldkey"),
        ("0xv", "0xw", "coldkey"),  # the victim's own other address stays untouched too
    ]
    assert penalty_reach("0xA", edges) == {"0xa", "0xsub", "0xsame"}


def test_positions_are_taken_at_the_day_s_end_whenever_they_are_read() -> None:
    from kokoah_subnet.mechanism import Fill
    from kokoah_subnet.validator.hyperliquid import day_volume_usd, end_of_day_positions

    state = {
        "time": 1_792_600_000_000,
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "0.7"}},
            {"position": {"coin": "ETH", "szi": "-2"}},
            {"position": {"coin": "SOL", "szi": "5"}},
        ],
    }
    # since the day ended: bought 0.2 BTC, bought back half of a 4 ETH short, sold out of a DOGE long
    later = [
        Fill(1, "a", "BTC", "B", 101_000.0, 0.2, True, 0.0),
        Fill(2, "a", "ETH", "B", 4_000.0, 2.0, True, 0.0),
        Fill(3, "a", "DOGE", "A", 0.2, 1_000.0, True, 0.0),
    ]
    prices = {"BTC": 100_000.0, "ETH": 3_900.0, "SOL": None, "DOGE": 0.19}
    out = end_of_day_positions(state, later, prices)
    assert out["BTC"]["notional"] == pytest.approx(0.5 * 100_000.0)  # what it held at the day's end, at its mark
    assert out["ETH"]["notional"] == pytest.approx(-4 * 3_900.0)
    assert out["DOGE"]["notional"] == pytest.approx(1_000 * 0.19)  # sold after the day's end: long at the end
    assert "SOL" not in out  # no mark for the day's end: left out
    assert day_volume_usd([{"c": "10", "v": "3"}, {"c": "20", "v": "1"}]) == 50.0
    assert day_volume_usd([{"c": "10"}]) is None  # candles without volume: unknown, never zero
