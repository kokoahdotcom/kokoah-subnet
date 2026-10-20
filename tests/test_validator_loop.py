"""The whole validator pass against fakes: a cached builder file, a fake info endpoint, a fake chain."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import lz4.frame
import pytest
from eth_account import Account

from kokoah_subnet.binding import Binding, encode_binding, sign_binding
from kokoah_subnet.mechanism import DEFAULT
from kokoah_subnet.validator.collect import ValidatorConfig, collect_and_score, day_index, day_strings
from kokoah_subnet.validator.hyperliquid import HyperliquidInfo
from kokoah_subnet.validator.loop import step
from kokoah_subnet.validator.serve import create_app
from kokoah_subnet.validator.store import Store

HK_A, HK_B, OWNER = "hkA", "hkB", "hkOwner"
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

    async def block(self) -> int:
        return self._block

    async def metagraph(self, netuid: int, block: int | None = None, commitments: bool = True) -> _Metagraph:
        neurons = [
            _Neuron(0, OWNER, "ckOwner"),
            _Neuron(1, HK_A, "ckA", _Balance(10 * 10**9)),
            _Neuron(2, HK_B, "ckB", _Balance(10 * 10**9)),
        ]
        return _Metagraph(block or self._block, 360, OWNER, neurons, 0.5)

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
    if t == "userFillsByTime":
        return httpx.Response(200, json=[])  # no total-fee matches: builder fee only (conservative)
    if t == "clearinghouseState":
        return httpx.Response(
            200,
            json={
                "marginSummary": {"accountValue": "25000"},
                "assetPositions": [{"position": {"coin": "BTC", "szi": "0.5", "positionValue": "50000"}}],
            },
        )
    if t == "userNonFundingLedgerUpdates":
        return httpx.Response(200, json=[])
    if t == "userFees":
        return httpx.Response(200, json={"userCrossRate": "0.00045", "userAddRate": "0.00015"})
    if t == "metaAndAssetCtxs":
        return httpx.Response(200, json=[{"universe": [{"name": "TAO"}]}, [{"markPx": "400"}]])
    if t == "subAccounts":
        return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
    return httpx.Response(400, json={"error": t})


def _setup(tmp_path: Path) -> tuple[ValidatorConfig, Store, HyperliquidInfo, _FakeChain, str, str]:
    a, b = Account.create(), Account.create()
    cfg = ValidatorConfig(
        network="fake",
        netuid=2,
        builder="0xbuilder",
        data_dir=tmp_path,
        start_day="2026-10-20",
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
    assert day_index(cfg, DAY) == 1 and result.day_index == 1
    records = {r.hotkey: r for r in result.records}
    assert records[HK_A].fees.builder_fee == pytest.approx(250.0) and records[HK_B].fees.builder_fee == pytest.approx(
        50.0
    )
    assert records[HK_B].fees.credit == pytest.approx(50.0)  # maker fills count in full
    assert result.allocation.price_usd == pytest.approx(0.5 * 400)
    assert not records[HK_A].gates_pass  # one day of history cannot pass the gates
    assert records[HK_A].multiplier == 0.85
    # stored: equity, positions, activity, bindings, states, the day's artifact with its file hash
    assert store.equity_series(addr_a, 5) == [(DAY, 25000.0, 0.0)]
    assert store.position_series([addr_a], [DAY])[addr_a] == {"BTC": [50000.0]}
    assert store.activity_window(addr_a, 30) == (10, 1, 1_000_000.0)
    assert set(store.bindings()) == {HK_A, HK_B}
    assert set(store.states()) == {HK_A, HK_B}
    published = json.loads(store.day(DAY) or "{}")
    assert published["builder_fills"] == 30 and len(published["builder_file_sha256"]) == 64
    assert published["bindings_refused"] == {} and published["errors"] == {}
    assert published["weights"][OWNER] > 0.9  # a young subnet burns most of the pool
    await hl.close()


async def test_step_scores_then_sets_weights_once_per_tempo(tmp_path: Path) -> None:
    cfg, store, hl, chain, _, _ = _setup(tmp_path)
    now = DAY_MS / 1000 + 86_400 + 600  # ten minutes into the next UTC day
    report = await step(cfg, store, hl, chain, wallet=None, now=now)
    assert report["scored"]["day"] == DAY and report["weights"]["ok"]
    assert sorted(report["weights"]["uids"]) == [0, 1, 2]
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
    await hl.close()
