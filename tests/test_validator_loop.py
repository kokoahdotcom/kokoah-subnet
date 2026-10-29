"""The whole validator pass against fakes: a cached builder file, a fake info endpoint, a fake chain."""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import lz4.frame
import pytest
from eth_account import Account

from kokoah_subnet.binding import AcceptedBinding, Binding, encode_binding, sign_binding, sign_unlink
from kokoah_subnet.mechanism import DEFAULT, MinerState
from kokoah_subnet.validator.collect import ValidatorConfig, collect_and_score, day_index, day_strings
from kokoah_subnet.validator.hyperliquid import HyperliquidInfo
from kokoah_subnet.validator.loop import step
from kokoah_subnet.validator.serve import create_app
from kokoah_subnet.validator.store import Store

HK_A, HK_B, OWNER, BURN = "hkA", "hkB", "hkOwner", "hkBurn"
EQUITY: dict[str, float] = {}  # address -> recorded account value in the fake history (default $25,000)
PORTFOLIO_FAILS: set[str] = set()  # addresses whose history read fails
HISTORY: dict[str, list[dict[str, Any]]] = {}  # address -> extra fills in its own history (not in the builder file)
OTHER_BUILDER: set[str] = set()  # addresses whose history-only orders paid another builder
EARNED = {"builderRewards": "100000"}
ACCOUNTS: dict[str, Any] = {}  # the accounts of the latest setup (their keys sign the bindings)
LIVE: dict[str, float] = {}  # address -> live account value (default $25,000)
STEP: dict[str, tuple[int, float]] = {}  # address -> (time ms, new value): the recorded value changes then
PNL_DAY: dict[str, float] = {}  # address -> recorded profit over the scored day (default $1,000, spread evenly)
LEDGER: dict[str, list[dict[str, Any]]] = {}  # address -> ledger records of the scored day
EXPLORER_FAILS: set[str] = set()  # addresses whose explorer reads fail
TX_LOOKUPS: list[str] = []  # every txDetails hash asked
DAY = "2026-10-21"
DAY_MS = 1792540800000


@dataclass
class _Balance:
    rao: int


@dataclass
class _Neuron:
    uid: int
    hotkey: str
    coldkey: str
    collateral_min: _Balance | None = None
    block_at_registration: int = 0


@dataclass
class _Metagraph:
    block: int
    tempo: int
    owner_hotkey: str
    neurons: list[_Neuron]
    price: float


@dataclass
class _Commitment:
    data: str
    block: int
    encrypted: bool = False


class _FakeChain:
    """Enough of `bt.Subtensor` for the validator: metagraph, commitments, stake, block, execute."""

    def __init__(self, commitments: dict[str, _Commitment], block: int = 10_000) -> None:
        self._block = block
        self._commitments = commitments
        self.executed: list[Any] = []
        self.subnets = self
        self.staking = self
        self.neurons = [
            _Neuron(0, OWNER, "ckOwner"),
            _Neuron(1, HK_A, "ckA", _Balance(10 * 10**9)),
            _Neuron(2, HK_B, "ckB", _Balance(10 * 10**9)),
            _Neuron(3, BURN, "ckBurn"),
        ]

    async def block(self) -> int:
        return self._block

    async def metagraph(self, netuid: int, block: int | None = None, commitments: bool = True) -> _Metagraph:
        return _Metagraph(block or self._block, 360, OWNER, list(self.neurons), 0.5)

    async def commitments(self, netuid: int) -> dict[str, _Commitment]:
        return self._commitments

    async def stake(self, coldkey: str, hotkey: str, netuid: int, block: int | None = None) -> _Balance:
        return _Balance(100 * 10**9 if hotkey == HK_A else 0)

    async def execute(self, intent: Any, wallet: Any) -> Any:
        self.executed.append(intent)

        @dataclass
        class R:
            success: bool = True
            message: str = "Success"

        return R()


def _csv(addr_a: str, addr_b: str) -> str:
    header = (
        "time,user,coin,side,px,sz,crossed,special_trade_type,tif,is_trigger,counterparty,"
        "closed_pnl,twap_id,builder_fee"
    )
    rows = [header]
    for i in range(20):
        side = "B" if i % 2 == 0 else "A"
        rows.append(f"{DAY_MS + i * 60000},{addr_a},BTC,{side},100000,0.5,True,,Ioc,False,0xcafe,0,,12.5")
    for i in range(10):
        side = "B" if i % 2 == 0 else "A"
        rows.append(f"{DAY_MS + i * 90000},{addr_b},ETH,{side},4000,5,False,,Gtc,False,0xbeef,0,,5")
    return "\n".join(rows) + "\n"


def _info_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    t = body["type"]
    if t == "userDetails" and body["user"].lower() in EXPLORER_FAILS:
        return httpx.Response(500, json={"error": "down"})
    if t == "userDetails":
        user = body["user"].lower()
        txs = [
            {
                "hash": f"0xtx{r['tid']}",
                "user": user,
                "error": None,
                "action": {
                    "type": "order",
                    "orders": [{"c": r["cloid"]}] if r.get("cloid") else [{"a": 0}],
                    "builder": {"b": "0xother" if user in OTHER_BUILDER and r["tid"] >= 1000 else "0xbuilder", "f": 25},
                },
            }
            for r in HISTORY.get(user, [])
        ]
        return httpx.Response(200, json={"type": "userDetails", "txs": txs})
    if t == "txDetails":
        TX_LOOKUPS.append(body["hash"])
        return httpx.Response(200, json={"type": "txDetails", "tx": {}})
    if t == "referral":
        return httpx.Response(200, json=dict(EARNED))
    if t == "extraAgents":
        return httpx.Response(200, json=[])
    if t == "userFillsByTime" and body["user"].lower() in HISTORY and body.get("startTime") == DAY_MS:
        return httpx.Response(200, json=HISTORY[body["user"].lower()])
    if t == "userFillsByTime":
        if body["user"].lower().endswith(body["user"].lower()[-4:]) and body.get("startTime") == DAY_MS:
            return httpx.Response(
                200,
                json=[
                    {
                        "coin": "BTC",
                        "px": "98000",
                        "sz": "1",
                        "side": "B",
                        "time": DAY_MS + 5_000_000,
                        "crossed": True,
                        "fee": "44.1",
                        "closedPnl": "0",
                    },
                ],
            )
        return httpx.Response(200, json=[])
    if t == "clearinghouseState":
        return httpx.Response(
            200,
            json={
                "marginSummary": {"accountValue": str(LIVE.get(body["user"].lower(), 25000.0))},
                "assetPositions": [{"position": {"coin": "BTC", "szi": "0.5", "positionValue": "50000"}}],
            },
        )
    if t == "userNonFundingLedgerUpdates":
        return httpx.Response(200, json=LEDGER.get(body["user"].lower(), []))
    if t == "userFees":
        return httpx.Response(200, json={"userCrossRate": "0.00045", "userAddRate": "0.00015"})
    if t == "metaAndAssetCtxs":
        return httpx.Response(200, json=[{"universe": [{"name": "TAO"}]}, [{"markPx": "400"}]])
    if t == "candleSnapshot":
        req = body["req"]
        if req["interval"] == "1h":
            return httpx.Response(200, json=[{"t": req["startTime"] + h * 3_600_000, "c": "400"} for h in range(24)])
        px = "100000" if req["coin"] == "BTC" else "4000"
        return httpx.Response(200, json=[{"t": req["startTime"] + m * 60_000, "c": px} for m in range(0, 1440, 1)])
    if t == "portfolio":
        user = body["user"].lower()
        if user in PORTFOLIO_FAILS:
            return httpx.Response(500, json={"error": "down"})
        hist = {"accountValueHistory": [[DAY_MS - 86_400_000, "24000"]], "pnlHistory": [[DAY_MS - 86_400_000, "0"]]}
        base = EQUITY.get(user, 25_000.0)
        step = STEP.get(user)
        day_pnl = PNL_DAY.get(user, 1_000.0)

        def value_at(ts: int) -> str:
            return str(step[1] if step and ts >= step[0] else base)

        def pnl_at(ts: int) -> str:
            return str(day_pnl * min(1.0, max(0.0, (ts - DAY_MS) / 86_400_000)))

        # the 7-day window: a made-up point at the far edge, recorded points every two hours, the live point last
        times = [DAY_MS + h * 7_200_000 for h in range(-24, 18)]
        week = [[DAY_MS - 7 * 86_400_000 + 123, "1"]] + [[ts, value_at(ts)] for ts in times]
        week += [[DAY_MS + 86_400_000 + 3_600_000 + 456, "999999"]]
        week_pnl = [[DAY_MS - 7 * 86_400_000 + 123, "0"]] + [[ts, pnl_at(ts)] for ts in times]
        week_pnl += [[DAY_MS + 86_400_000 + 3_600_000 + 456, "777"]]
        all_time = {"accountValueHistory": [[DAY_MS - 90 * 86_400_000, "1000"]], "pnlHistory": []}
        return httpx.Response(
            200,
            json=[
                ["perpMonth", hist],
                ["perpWeek", {"accountValueHistory": week, "pnlHistory": week_pnl}],
                ["perpAllTime", all_time],
            ],
        )
    if t == "subAccounts":
        return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
    return httpx.Response(400, json={"error": t})


def _setup(tmp_path: Path) -> tuple[ValidatorConfig, Store, HyperliquidInfo, _FakeChain, str, str]:
    EQUITY.clear()
    LIVE.clear()
    STEP.clear()
    PNL_DAY.clear()
    LEDGER.clear()
    EXPLORER_FAILS.clear()
    TX_LOOKUPS.clear()
    PORTFOLIO_FAILS.clear()
    HISTORY.clear()
    OTHER_BUILDER.clear()
    EARNED["builderRewards"] = "100000"
    a, b = Account.create(), Account.create()
    ACCOUNTS.update(a=a, b=b)
    cfg = ValidatorConfig(
        network="fake",
        netuid=2,
        builder="0xbuilder",
        data_dir=tmp_path,
        start_day="2026-10-21",  # the cached file is for 2026-10-21; day zero (params) is 2026-10-19
        burn_hotkey=BURN,
        weights_every_blocks=360,
        retry_missing_s=3600,
    )
    cache = tmp_path / "fills"
    cache.mkdir()
    (cache / "20261021.csv.lz4").write_bytes(lz4.frame.compress(_csv(a.address.lower(), b.address.lower()).encode()))
    commitments = {
        HK_A: _Commitment(encode_binding(Binding(a.address, sign_binding(a.key, 2, HK_A, a.address))), 9000),
        HK_B: _Commitment(encode_binding(Binding(b.address, sign_binding(b.key, 2, HK_B, b.address))), 9001),
        OWNER: _Commitment("not a binding", 1),
    }
    store = Store(tmp_path / "v.sqlite")
    hl = HyperliquidInfo(rate_limit_s=0.0)
    hl._client = httpx.AsyncClient(transport=httpx.MockTransport(_info_handler))
    return cfg, store, hl, _FakeChain(commitments), a.address.lower(), b.address.lower()


async def test_collect_and_score_a_day(tmp_path: Path) -> None:
    cfg, store, hl, chain, addr_a, addr_b = _setup(tmp_path)
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    assert day_index(cfg, DAY, DEFAULT) == 2 and result.day_index == 2
    records = {r.hotkey: r for r in result.records}
    assert records[HK_A].fees.builder_fee == pytest.approx(250.0) and records[HK_B].fees.builder_fee == pytest.approx(
        50.0
    )
    assert records[HK_B].fees.credit == pytest.approx(50.0)  # maker fills count in full
    assert result.allocation.price_usd == pytest.approx(0.5 * 400)
    assert not records[HK_A].gates_pass  # one day of history cannot pass the gates
    assert records[HK_A].skill_pool_reason is not None and records[HK_A].skill_pay_alpha == 0.0
    # stored: equity, positions, activity, bindings, states, the day's artifact with its file hash
    # the backfilled day, then today; the 2,000-dollar off-market gift (1 BTC bought 2% under the mark) is a flow
    assert store.equity_series(addr_a, 5) == [("2026-10-20", 24000.0, 0.0), (DAY, 25000.0, pytest.approx(2000.0))]
    assert store.returns(addr_a, 5) == [pytest.approx((25000 - 24000 - 2000) / 24000)]
    assert store.position_series([addr_a], [DAY])[addr_a] == {"BTC": [50000.0]}
    assert store.activity_window(addr_a, 30) == (10, 1, 1_000_000.0)
    assert set(store.bindings()) == {HK_A, HK_B}
    assert set(store.states()) == {HK_A, HK_B}
    published = json.loads(store.day(DAY) or "{}")
    assert published["builder_fills"] == 30 and len(published["builder_file_sha256"]) == 64
    assert published["bindings_refused"] == {} and published["errors"] == {}
    assert BURN not in published["weights"] and published["burn_alpha"] == 0.0  # the whole pool goes to miners
    await hl.close()


async def test_step_scores_then_sets_weights_once_per_tempo(tmp_path: Path) -> None:
    cfg, store, hl, chain, _, _ = _setup(tmp_path)
    now = DAY_MS / 1000 + 86_400 + 600  # ten minutes into the next UTC day
    report = await step(cfg, store, hl, chain, wallet=None, now=now)
    assert report["scored"]["day"] == DAY and report["weights"]["ok"]
    assert sorted(report["weights"]["uids"]) == [1, 2]
    assert len(chain.executed) == 1
    # a second pass in the same tempo: nothing scored again, no second submission
    report = await step(cfg, store, hl, chain, wallet=None, now=now + 60)
    assert report["scored"] is None and report["weights"] is None
    # a tempo later the same weights are submitted again
    chain._block += 360
    report = await step(cfg, store, hl, chain, wallet=None, now=now + 120)
    assert report["weights"]["ok"] and len(chain.executed) == 2
    await hl.close()


async def test_step_retries_a_missing_file_hourly(tmp_path: Path) -> None:
    cfg, store, hl, chain, _, _ = _setup(tmp_path)
    (tmp_path / "fills" / "20261021.csv.lz4").unlink()
    cfg = ValidatorConfig(**{**cfg.__dict__, "builder_base_url": "http://127.0.0.1:9/none"})
    now = DAY_MS / 1000 + 86_400 + 600
    report = await step(cfg, store, hl, chain, wallet=None, now=now)
    assert report["scored"]["day"] == DAY and ("missing" in report["scored"] or "error" in report["scored"])
    report = await step(cfg, store, hl, chain, wallet=None, now=now + 60)
    assert report["scored"] is None  # not retried within the hour
    await hl.close()


def test_day_strings() -> None:
    assert day_strings("2026-10-21", 3) == ["2026-10-19", "2026-10-20", "2026-10-21"]


async def test_serve_reads_the_store(tmp_path: Path) -> None:
    cfg, store, hl, chain, _, _ = _setup(tmp_path)
    await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    app = create_app(store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v") as c:
        assert (await c.get("/health")).json() == {"ok": True, "latest_day": DAY}
        assert (await c.get("/days")).json() == [DAY]
        assert (await c.get("/days/latest")).json()["day"] == DAY
        assert (await c.get("/days/2026-01-01")).status_code == 404
        assert len((await c.get("/bindings")).json()) == 2
        board = (await c.get("/leaderboard")).json()
        assert len(board) == 2 and board[0]["volume_usd"] >= board[1]["volume_usd"]
        assert board[0]["days"] == 1 and board[0]["total_volume_usd"] == board[0]["volume_usd"]
        assert board[0]["volume_pay_alpha"] > 0 and board[0]["skill_pay_alpha"] == 0.0
        assert all(r["eligible"] and r["qualified"] and r["avg_equity_usd"] == 25_000.0 for r in board)
        page = await c.get("/")
        assert page.status_code == 200 and "Leaderboard" in page.text and 'fetch("leaderboard")' in page.text
        summary = (await c.get("/summary")).json()
        assert summary["latest_day"] == DAY and summary["days"] == 1
        # each row carries the account's record, and the whole record is one request away
        assert {
            "record_days",
            "return_90d",
            "worst_drop_90d",
            "losing_days_90d",
            "return_days_90d",
            "agent_days",
        } <= set(board[0])
        account = (await c.get(f"/accounts/{board[0]['address']}")).json()
        assert account["hotkey"] == board[0]["hotkey"] and account["linked"] is True
        assert account["days"][-1]["day"] == DAY and account["days"][-1]["scored"] is True
        assert account["record"]["record_days"] == board[0]["record_days"]
        assert (await c.get("/accounts/0x" + "99" * 20)).status_code == 404
        assert (await c.get("/accounts/not-an-address")).status_code == 400
        assert summary["burn_alpha"] == pytest.approx(DEFAULT.miner_pool_alpha - summary["paid_alpha"])
        assert summary["builder_fee_usd"] == pytest.approx(300.0)
        assert "access-control-allow-origin" in {
            k.lower() for k in (await c.get("/summary", headers={"Origin": "https://x"})).headers
        }
    await hl.close()


async def test_without_a_registered_burn_hotkey_the_owner_hotkey_is_the_fallback(tmp_path: Path) -> None:
    cfg, store, hl, chain, _, _ = _setup(tmp_path)
    cfg = ValidatorConfig(**{**cfg.__dict__, "burn_hotkey": "hkNowhere"})
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    published = json.loads(store.day(DAY) or "{}")
    assert published["burn_hotkey"] == OWNER and result.allocation.burn_alpha == 0.0
    await hl.close()


async def test_step_survives_a_refused_submission(tmp_path: Path) -> None:
    cfg, store, hl, chain, _, _ = _setup(tmp_path)

    async def refuse(intent: Any, wallet: Any) -> Any:
        raise RuntimeError("Weights on netuid 2 were last set 11 blocks ago; the rate limit is 100 blocks. Retry.")

    chain.execute = refuse  # type: ignore[method-assign]
    now = DAY_MS / 1000 + 86_400 + 600
    report = await step(cfg, store, hl, chain, wallet=None, now=now)
    assert report["weights"]["ok"] is False and "rate limit" in report["weights"]["message"]
    assert store.get_meta("weights:rate_limit") == "100"
    # the failure is on the record and the loop goes on
    with store._engine.connect() as c:
        from kokoah_subnet.validator import schema as S

        rows = c.execute(S.weights_submitted.select()).all()
    assert len(rows) == 1 and rows[0].ok is False
    await hl.close()


async def test_pending_days_are_scored_in_order_and_a_missing_file_blocks(tmp_path: Path) -> None:
    from kokoah_subnet.validator.loop import pending_days

    cfg, store, hl, chain, _, _ = _setup(tmp_path)
    cfg = ValidatorConfig(**{**cfg.__dict__, "start_day": "2026-10-20", "builder_base_url": "http://127.0.0.1:9/none"})
    # two days are due (10-20 and 10-21); 10-20 has no file, so the loop waits on it and never jumps to 10-21
    now = DAY_MS / 1000 + 86_400 + 600
    assert pending_days(store, "2026-10-20", "2026-10-21") == ["2026-10-20", "2026-10-21"]
    report = await step(cfg, store, hl, chain, wallet=None, now=now)
    assert report["scored"]["day"] == "2026-10-20" and report["pending_days"] == ["2026-10-20", "2026-10-21"]
    assert store.day("2026-10-21") is None
    await hl.close()


def test_skip_day_advances_states_with_zero_credit(tmp_path: Path) -> None:
    from kokoah_subnet.mechanism import MinerState
    from kokoah_subnet.validator.skip import skip_day

    store = Store(tmp_path / "s.sqlite")
    store.put_states({"hk": MinerState(phi_hat=100.0, g=0.5, paid_days=((0, 10.0),))}, day="2026-10-20")
    msg = skip_day(store, "2026-10-21", 2, BURN, "never published", DEFAULT)
    assert "closed as skipped" in msg
    after = store.states()["hk"]
    assert after.phi_hat == pytest.approx(70.0) and after.g == pytest.approx(0.35)
    artifact = json.loads(store.day("2026-10-21") or "{}")
    assert artifact["skipped"] == "never published" and artifact["weights"] == {BURN: 1.0}
    assert "already scored" in skip_day(store, "2026-10-21", 2, BURN, "x", DEFAULT)


async def test_eligibility_through_the_whole_pass(tmp_path: Path) -> None:
    """A new miner qualifies on its third trade (the first two earn nothing); one below $10,000 of average equity
    is recorded but paid nothing and takes no share; the averages and their points are on the record."""
    cfg, store, hl, chain, addr_a, addr_b = _setup(tmp_path)
    EQUITY[addr_b] = 9_999.0
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    ra, rb = (next(r for r in result.records if r.hotkey == hk) for hk in (HK_A, HK_B))
    assert ra.eligible and ra.qualified and ra.qual_trades == 3 and ra.avg_equity_usd == pytest.approx(25_000.0)
    assert ra.credited.fills == 18 and ra.credited.builder_fee == pytest.approx(18 * 12.5)
    assert ra.pay_alpha > 0
    assert not rb.eligible and rb.ineligible_reason == "average equity below the minimum"
    assert rb.qualified  # trades still count toward qualification
    assert rb.fees.fills == 10 and rb.credited.fills == 0  # recorded, not credited
    assert rb.pay_alpha == 0.0 and rb.volume_pay_alpha == 0.0
    assert store.states()[HK_B].phi_hat == 0.0
    with store._engine.connect() as c:
        from kokoah_subnet.validator import schema as S

        daily = {r.address: r for r in c.execute(S.equity_daily.select()).all()}
        rows = {r.hotkey: r for r in c.execute(S.miner_days.select()).all()}
        points = c.execute(S.equity_points.select().where(S.equity_points.c.address == addr_a)).all()
    assert daily[addr_b].avg_equity_usd == pytest.approx(9_999.0) and daily[addr_a].points == 12
    assert rows[HK_A].eligible is True and rows[HK_A].credited_fills == 18 and rows[HK_A].qual_trades == 3
    assert rows[HK_B].ineligible_reason == "average equity below the minimum"
    assert all(p.value not in (1.0, 999999.0) for p in points)  # the edge and live points were dropped
    await hl.close()


async def test_a_failed_history_read_fails_the_day_instead_of_zeroing_the_trader(tmp_path: Path) -> None:
    cfg, store, hl, chain, addr_a, _ = _setup(tmp_path)
    PORTFOLIO_FAILS.add(addr_a)
    with pytest.raises(RuntimeError, match="portfolio history"):
        await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    assert store.day(DAY) is None  # not scored: the loop retries within the hour
    await hl.close()


KO = "0x6b6f0101" + "cd" * 12


def _history_of_file_fills(offset_ms: int = 321) -> list[dict[str, Any]]:
    """Trader A's own history: the 20 fills the file lists (to the millisecond) and four more after midday."""
    out = []
    for i in range(20):
        side = "B" if i % 2 == 0 else "A"
        t = DAY_MS + i * 60000 + offset_ms
        out.append(
            {
                "time": t,
                "coin": "BTC",
                "side": side,
                "px": "100000",
                "sz": "0.5",
                "crossed": True,
                "fee": "35",
                "builderFee": "12.5",
                "tid": 1 + i,
                "cloid": KO[:-4] + f"{i:04x}",
                "closedPnl": "0",
            }
        )
    for i in range(4):
        side = "B" if i % 2 == 0 else "A"
        t = DAY_MS + 15 * 3_600_000 + i * 60000 + offset_ms
        out.append(
            {
                "time": t,
                "coin": "BTC",
                "side": side,
                "px": "100000",
                "sz": "0.5",
                "crossed": True,
                "fee": "35",
                "builderFee": "12.5",
                "tid": 1000 + i,
                "cloid": KO[:-4] + f"{1000 + i:04x}",
                "closedPnl": "0",
            }
        )
    return out


async def test_fills_after_midday_count_from_the_traders_own_history(tmp_path: Path) -> None:
    cfg, store, hl, chain, addr_a, _ = _setup(tmp_path)
    HISTORY[addr_a] = _history_of_file_fills()
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    ra = next(r for r in result.records if r.hotkey == HK_A)
    assert ra.fees.fills == 24 and ra.fees.builder_fee == pytest.approx(300.0)
    assert ra.fees.total_fee == pytest.approx(24 * 35.0)  # exact total fees, matched on the second
    assert ra.credited.fills == 22  # the first two trades qualify but earn nothing
    with store._engine.connect() as c:
        from kokoah_subnet.validator import schema as S

        rows = c.execute(S.fills.select().where(S.fills.c.address == addr_a)).all()
        earned = c.execute(S.builder_rewards.select()).all()
    sources = sorted(r.source for r in rows)
    assert sources.count("both") == 20 and sources.count("history") == 4
    assert all(r.tid is not None and r.cloid.startswith(KO[:-4]) for r in rows)
    assert len(earned) == 1 and earned[0].builder_rewards == 100000.0
    await hl.close()


async def test_a_missing_file_waits_until_the_deadline_then_the_histories_score_the_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kokoah_subnet.validator import collect as collect_module
    from kokoah_subnet.validator.builder_feed import NotPublished

    async def not_published(cfg: Any, day: str) -> Any:
        raise NotPublished(day)

    cfg, store, hl, chain, addr_a, _ = _setup(tmp_path)
    monkeypatch.setattr(collect_module, "fetch_builder_file", not_published)
    HISTORY[addr_a] = _history_of_file_fills()
    end_s = DAY_MS / 1000 + 86_400
    with pytest.raises(NotPublished):
        await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, now_ts=end_s + 3_600)
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, now_ts=end_s + 13 * 3_600)
    ra = next(r for r in result.records if r.hotkey == HK_A)
    assert ra.fees.fills == 24 and ra.fees.builder_fee == pytest.approx(300.0)
    assert store.file(DAY) == ("", 0)
    await hl.close()


async def test_credited_fees_beyond_what_the_builder_earned_raise_the_alarm(tmp_path: Path) -> None:
    from kokoah_subnet.validator.health import check

    cfg, store, hl, chain, addr_a, _ = _setup(tmp_path)
    EARNED["builderRewards"] = "1"  # the exchange says the builder earned $1; the day credits $300
    await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    report = check(store)
    assert report["builder_earned_usd"] == 1.0 and report["credited_builder_fees_usd"] > 1.0
    assert any("did not reach the builder" in p for p in report["problems"])
    await hl.close()


async def test_an_order_that_paid_another_builder_is_not_counted(tmp_path: Path) -> None:
    cfg, store, hl, chain, addr_a, _ = _setup(tmp_path)
    HISTORY[addr_a] = _history_of_file_fills()
    OTHER_BUILDER.add(addr_a)  # the four afternoon orders carried someone else's builder code
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    ra = next(r for r in result.records if r.hotkey == HK_A)
    assert ra.fees.fills == 20  # only the fills the file confirms
    with store._engine.connect() as c:
        from kokoah_subnet.validator import schema as S

        stages = [r.stage for r in c.execute(S.errors.select()).all()]
    assert "builder" in stages
    await hl.close()


async def test_a_sealed_machine_agent_is_verified_through_the_whole_pass(tmp_path: Path) -> None:
    """Hotkey A's account trades from a sealed machine (the real harness, a development attestor standing in for
    Google): its binding carries the agent field, the validator challenges the machine and reads the operator's
    released list over HTTP, and the day records A as a verified agent; once the key is released, it is not."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    from kokoah_subnet.agent.attest import DevelopmentAttestor, StaticKeys
    from kokoah_subnet.agent.harness import Harness, HarnessConfig, public_app
    from kokoah_subnet.binding import AgentRef

    cfg, store, hl, chain, addr_a, _ = _setup(tmp_path)
    image, code = "sha256:" + "ab" * 32, "cd" * 32
    machine_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    harness = Harness(
        HarnessConfig(netuid=2, hotkey=HK_A, master=Account.create().address, code_sha256=code, builder="0xbuilder"),
        ACCOUNTS["a"].key,
        DevelopmentAttestor(machine_key, image),
    )
    line = harness.binding()
    assert line["address"] == addr_a
    agent_binding = Binding(line["address"], line["signature"], agent=AgentRef(code, "203.0.113.7", 8080))
    chain._commitments[HK_A] = _Commitment(encode_binding(agent_binding), 9000)
    released: list[dict[str, Any]] = []
    machine = httpx.ASGITransport(app=public_app(harness))

    async def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "203.0.113.7":
            return await machine.handle_async_request(request)
        if request.url.path == "/unlinks":
            return httpx.Response(200, json={"unlinks": []})
        assert request.url.path == "/key-store/released"
        return httpx.Response(200, json={"released": released})

    class Router(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await route(request)

    cfg = ValidatorConfig(**{**cfg.__dict__, "agent_images": (image,), "agent_service": "https://svc.example"})
    keys = StaticKeys(machine_key.public_key())
    async with httpx.AsyncClient(transport=Router()) as http:
        result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, agent_keys=keys, agent_http=http)
        records = {r.hotkey: r for r in result.records}
        assert records[HK_A].agent_verified and not records[HK_B].agent_verified
        assert records[HK_A].fees.builder_fee == pytest.approx(250.0)  # an agent's fills count like anyone's
        artifact = json.loads(store.day(DAY) or "{}")
        assert next(m for m in artifact["miners"] if m["hotkey"] == HK_A)["agent_verified"] is True
        released.append({"address": addr_a, "released_at": 1.0})  # the miner takes the key out
        again = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, agent_keys=keys, agent_http=http)
        assert not {r.hotkey: r for r in again.records}[HK_A].agent_verified
    await hl.close()


async def test_money_back_from_a_vault_is_not_trading_profit(tmp_path: Path) -> None:
    """B's account value jumps by $5,000 at noon: a vault withdrawal (whose record names the amount as
    `netWithdrawnUsd`), while the exchange's recorded profit stays flat. The day's return is the recorded profit
    over the account value, not the jump."""
    cfg, store, hl, chain, _, addr_b = _setup(tmp_path)
    noon = DAY_MS + 12 * 3_600_000
    STEP[addr_b] = (noon, 30_000.0)
    LIVE[addr_b] = 30_000.0
    PNL_DAY[addr_b] = 2_000.0  # the day's recorded profit: the off-market fill's gain, which the return leaves out
    LEDGER[addr_b] = [
        {
            "time": noon,
            "hash": "0x" + "ab" * 32,
            "delta": {
                "type": "vaultWithdraw",
                "vault": "0x" + "cd" * 20,
                "user": addr_b,
                "requestedUsd": "5000.0",
                "commission": "0.0",
                "closingCost": "0.0",
                "basis": "5000.0",
                "netWithdrawnUsd": "5000.0",
            },
        },
    ]
    await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    assert store.dated_returns(addr_b, 5) == {DAY: pytest.approx(0.0)}
    await hl.close()


async def test_the_return_does_not_depend_on_when_a_validator_scores(tmp_path: Path) -> None:
    """Two validators score the same day at different times, with different live account values: the return is
    read from the recorded history at the day's edges, so both get the same."""
    results = []
    for i, live in enumerate((25_000.0, 31_000.0)):
        (tmp_path / f"v{i}").mkdir()
        cfg, store, hl, chain, addr_a, _ = _setup(tmp_path / f"v{i}")
        LIVE[addr_a] = live
        await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
        results.append(store.dated_returns(addr_a, 5)[DAY])
        await hl.close()
    assert results[0] == pytest.approx(results[1])


async def test_a_day_the_record_does_not_cover_has_no_return(tmp_path: Path) -> None:
    """Without recorded points around the end of the day, the day is waited for until the file deadline (the
    record catches up within hours), then scored without a return: it is never guessed from a live value."""
    cfg, store, hl, chain, addr_a, _ = _setup(tmp_path)
    STEP[addr_a] = (DAY_MS + 22 * 3_600_000, 25_000.0)
    end = DAY_MS + 86_400_000
    import test_validator_loop as me

    original = me._info_handler

    def no_late_points(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if json.loads(request.content)["type"] == "portfolio":
            windows = response.json()
            for name, w in windows:
                if name == "perpWeek":
                    for key in ("accountValueHistory", "pnlHistory"):
                        w[key] = [p for p in w[key] if p[0] < end - 3_600_000] + [w[key][-1]]
            return httpx.Response(200, json=windows)
        return response

    hl._client = httpx.AsyncClient(transport=httpx.MockTransport(no_late_points))
    with pytest.raises(RuntimeError, match="does not reach the end"):
        await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, now_ts=end / 1000 + 3600)
    await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, now_ts=end / 1000 + 13 * 3600)
    assert DAY not in store.dated_returns(addr_a, 5)
    with store._engine.connect() as c:
        from kokoah_subnet.validator import schema as S

        stages = [r.stage for r in c.execute(S.errors.select()).all()]
    assert "daily return" in stages
    await hl.close()


def _taker_fills_after_midday(n: int) -> list[dict[str, Any]]:
    """Tiny taker fills after the file's cut, each its own transaction, none of them a Kokoah order."""
    return [
        {
            "time": DAY_MS + 15 * 3_600_000 + i * 1000,
            "coin": "BTC",
            "side": "B" if i % 2 == 0 else "A",
            "px": "100000",
            "sz": "0.0001",
            "crossed": True,
            "fee": "0.007",
            "builderFee": "0.0025",
            "tid": 50_000 + i,
            "hash": f"0x{i:064x}",
            "closedPnl": "0",
        }
        for i in range(n)
    ]


async def test_cheap_tiny_orders_cannot_flood_the_validator_with_lookups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kokoah_subnet.validator import collect

    monkeypatch.setattr(collect, "MAX_TX_LOOKUPS", 30)
    cfg, store, hl, chain, addr_a, _ = _setup(tmp_path)
    HISTORY[addr_a] = _history_of_file_fills() + _taker_fills_after_midday(300)
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    assert len(TX_LOOKUPS) == 30  # the cap, not 300
    assert {r.hotkey: r for r in result.records}[HK_A].pay_alpha > 0  # the day is scored
    with store._engine.connect() as c:
        from kokoah_subnet.validator import schema as S

        messages = [r.message for r in c.execute(S.errors.select()).all()]
    assert any("beyond the daily lookup cap" in m for m in messages)
    await hl.close()


async def test_one_accounts_explorer_failure_affects_only_that_account_after_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kokoah_subnet.validator import hyperliquid

    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr(f"{hyperliquid.__name__}.asyncio.sleep", instant)  # no back-off waits in the test
    cfg, store, hl, chain, addr_a, addr_b = _setup(tmp_path)
    HISTORY[addr_a] = _history_of_file_fills()
    HISTORY[addr_b] = _taker_fills_after_midday(3)
    EXPLORER_FAILS.add(addr_b)
    end = DAY_MS + 86_400_000
    with pytest.raises(RuntimeError, match="unavailable"):
        await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, now_ts=end / 1000 + 3600)
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, now_ts=end / 1000 + 13 * 3600)
    records = {r.hotkey: r for r in result.records}
    assert records[HK_A].credited.fills == 22  # A's day is untouched: its four history fills confirmed as usual
    assert records[HK_B].fees.fills == 10  # B keeps its file fills; its three unconfirmed later fills do not count
    await hl.close()


def _unlinks_service(rows: list[dict[str, Any]], fail: bool = False) -> httpx.AsyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(503)
        assert request.url.path == "/unlinks"
        return httpx.Response(200, json={"unlinks": rows})

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


async def test_an_address_that_unlinked_itself_earns_its_hotkey_nothing_from_its_day(tmp_path: Path) -> None:
    """A's owner signed an unlink the day before: from this day A's hotkey is no longer linked, its commitment is
    refused, and A's trades pay nobody; B is untouched."""
    cfg, store, hl, chain, addr_a, _ = _setup(tmp_path)
    cfg = ValidatorConfig(**{**cfg.__dict__, "agent_service": "https://svc.example"})
    t = DAY_MS - 3_600_000  # an hour before the day starts
    signed = sign_unlink(ACCOUNTS["a"].key, 2, HK_A, addr_a, t)
    forged = sign_unlink(Account.create().key, 2, HK_B, ACCOUNTS["b"].address, t)  # not B's own signature
    rows = [
        {"hotkey": HK_A, "address": addr_a, "time_ms": t, "signature": signed},
        {"hotkey": HK_B, "address": ACCOUNTS["b"].address.lower(), "time_ms": t, "signature": forged},
    ]
    store.put_bindings({HK_A: AcceptedBinding(HK_A, addr_a, 9000, 9000)})  # A was linked before
    async with _unlinks_service(rows) as http:
        result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, agent_http=http)
    assert {r.hotkey for r in result.records} == {HK_B}
    assert HK_A not in store.bindings() and HK_B in store.bindings()
    events = store.execute_sql(f"select event, hotkey, reason from binding_events where day = '{DAY}'")
    assert {"event": "unlinked", "hotkey": HK_A, "reason": "the address ended the link"} in events
    assert {"event": "refused", "hotkey": HK_A, "reason": "the address unlinked itself from this hotkey"} in events
    assert [u.hotkey for u in store.unlinks()] == [HK_A]
    await hl.close()


async def test_a_validator_waits_for_the_unlink_list_until_the_deadline(tmp_path: Path) -> None:
    cfg, store, hl, chain, _, _ = _setup(tmp_path)
    cfg = ValidatorConfig(**{**cfg.__dict__, "agent_service": "https://svc.example"})
    early = DAY_MS / 1000 + 86_400 + 3600  # an hour after the day ended
    async with _unlinks_service([], fail=True) as http:
        with pytest.raises(RuntimeError, match="unlinks unavailable"):
            await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, agent_http=http, now_ts=early)
        late = DAY_MS / 1000 + 86_400 + (DEFAULT.file_deadline_hours + 1) * 3600
        result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT, agent_http=http, now_ts=late)
    assert {r.hotkey for r in result.records} == {HK_A, HK_B}
    await hl.close()


def _swap_hotkey_a(store: Store, chain: _FakeChain, registered: tuple[int, int]) -> None:
    """The day before, uid 1 was hotkey A (registered at `registered[0]`), with a smoothed skill of 0.4; today uid 1
    on the same coldkey is hotkey A2 (registered at `registered[1]`), bound to the same account."""
    store.put_neurons(
        "2026-10-20", 9_000, [{"uid": 1, "hotkey": HK_A, "coldkey": "ckA", "block_at_registration": registered[0]}]
    )
    store.put_states({HK_A: MinerState(g=0.4, phi_hat=50.0, qualified=True)}, day="2026-10-20")
    a = ACCOUNTS["a"]
    chain.neurons[1] = _Neuron(1, "hkA2", "ckA", _Balance(10 * 10**9), block_at_registration=registered[1])
    chain._commitments["hkA2"] = _Commitment(
        encode_binding(Binding(a.address, sign_binding(a.key, 2, "hkA2", a.address))), 9500
    )
    del chain._commitments[HK_A]
    # a failed earlier attempt of the same day recorded today's metagraph already: it is not the previous day
    store.put_neurons(
        DAY, 9_900, [{"uid": 1, "hotkey": "hkA2", "coldkey": "ckA", "block_at_registration": registered[1]}]
    )


async def test_a_hotkey_swap_is_detected_between_two_scored_days(tmp_path: Path) -> None:
    cfg, store, hl, chain, _, _ = _setup(tmp_path)
    _swap_hotkey_a(store, chain, (100, 100))
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    assert store.swaps_through(DAY) == [(DAY, HK_A, "hkA2")]
    events = store.execute_sql(f"select event, hotkey from binding_events where day = '{DAY}'")
    assert {"event": "swapped", "hotkey": "hkA2"} in events
    a2 = next(r for r in result.records if r.hotkey == "hkA2")
    assert a2.g == pytest.approx(0.7 * 0.4 + 0.3 * a2.skill_raw)  # the skill record follows the new hotkey
    assert a2.phi_hat == pytest.approx(0.3 * a2.credited.credit)  # the credit does not
    await hl.close()


async def test_a_new_registration_on_the_same_uid_is_not_a_swap(tmp_path: Path) -> None:
    cfg, store, hl, chain, _, _ = _setup(tmp_path)
    _swap_hotkey_a(store, chain, (100, 200))  # uid 1 was deregistered and registered again by the same coldkey
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    assert store.swaps_through(DAY) == []
    a2 = next(r for r in result.records if r.hotkey == "hkA2")
    assert a2.g == pytest.approx(0.3 * a2.skill_raw)  # a fresh hotkey: no skill record carried over
    await hl.close()


async def test_one_missing_day_keeps_a_long_record_in_the_entry_test(tmp_path: Path) -> None:
    """A's 120 stored days miss one day ten days ago (an account read that failed): the entry test still has its 90
    returns."""
    import random

    cfg, store, hl, chain, addr_a, _ = _setup(tmp_path)
    rng = random.Random(5)
    value = 25_000.0
    for back in range(120, 0, -1):
        value *= 1 + rng.gauss(0.002, 0.01)
        if back != 10:
            store.put_equity(addr_a, (date.fromisoformat(DAY) - timedelta(days=back)).isoformat(), value, 0.0)
    result = await collect_and_score(cfg, DAY, store, hl, chain, DEFAULT)
    a = next(r for r in result.records if r.hotkey == HK_A)
    assert a.entry_psr is not None and a.returns_days == 90
    await hl.close()
