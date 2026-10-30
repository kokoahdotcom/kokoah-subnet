"""The miner's and the owner's agent commands: the start permission is signed by the hotkey and checks out the way
the service checks it; one file per permission; the service key is created once, private."""

import stat
from pathlib import Path
from typing import Any

import bittensor as bt
import httpx
import pytest
from bittensor.wallets import verify_message
from eth_account import Account

from kokoah_subnet.agent import keystore
from kokoah_subnet.agent.keystore import encrypt_key, parse_permit, start_message
from kokoah_subnet.cli import main

MASTER = "0x1c8970117aD600590a21A4D0651865790F7A7253"
SUBMISSION = "eb" * 32


def test_authorize_writes_a_permission_signed_by_the_hotkey(tmp_path: Path) -> None:
    wallet = bt.Wallet(name="miner", hotkey="hk", path=str(tmp_path / "wallets"))
    wallet.create_new_hotkey(use_password=False, overwrite=True, suppress=True)
    out = tmp_path / "permit.txt"
    args = ["agent", "authorize", "--netuid", "385", "--master", MASTER, "--submission", SUBMISSION]
    args += ["--wallet", "miner", "--wallet-hotkey", "hk", "--wallet-path", str(tmp_path / "wallets")]
    args += ["--out", str(out)]
    assert main(args) == 0
    t, signature = parse_permit(out.read_text())
    hotkey = wallet.hotkey.ss58_address
    assert verify_message(start_message(385, hotkey, MASTER, SUBMISSION, t), signature, hotkey)
    assert not verify_message(start_message(386, hotkey, MASTER, SUBMISSION, t), signature, hotkey)
    assert main(args) == 1  # never overwritten: a permission is for one machine start


def test_the_service_key_is_created_once_and_private(tmp_path: Path) -> None:
    out = tmp_path / "wrap.key"
    assert main(["agent", "service-key", "--out", str(out)]) == 0
    assert len(bytes.fromhex(out.read_text().strip())) == 32
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    before = out.read_text()
    assert main(["agent", "service-key", "--out", str(out)]) == 1  # never replaced: every stored copy depends on it
    assert out.read_text() == before


def test_packaging_encrypts_by_default(tmp_path: Path) -> None:
    from kokoah_subnet.agent.bundle import decrypt, is_encrypted

    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "entry.py").write_text("print('agent')\n")
    out = tmp_path / "a.bundle"
    assert main(["agent", "package", str(tmp_path / "agent"), "--out", str(out)]) == 0
    key_file = tmp_path / "a.bundle.key"
    assert is_encrypted(out.read_bytes()) and stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert decrypt(out.read_bytes(), bytes.fromhex(key_file.read_text()))[:2] == b"PK"  # the zip inside
    public = tmp_path / "p.bundle"
    assert main(["agent", "package", str(tmp_path / "agent"), "--out", str(public), "--public"]) == 0
    assert not is_encrypted(public.read_bytes()) and not (tmp_path / "p.bundle.key").exists()
    (tmp_path / "agent" / ".env").write_text("SECRET=1\n")
    refused = tmp_path / "r.bundle"
    assert main(["agent", "package", str(tmp_path / "agent"), "--out", str(refused), "--public"]) == 1
    assert not refused.exists()


def test_release_checks_everything_it_can_before_taking_the_key_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    account = Account.create()
    secret = bytes(range(32))
    calls: list[str] = []
    answer: list[Any] = [encrypt_key(bytes(account.key), account.address, secret)]

    def release(self: Any, address: str, time_ms: int, signature: str) -> bytes:
        calls.append(address)
        if isinstance(answer[0], Exception):
            raise answer[0]
        return bytes(answer[0])

    monkeypatch.setattr(keystore.KeyService, "release", release)
    secret_file = tmp_path / "secret.hex"
    secret_file.write_text(secret.hex())
    out = tmp_path / "key.txt"

    def run(secret_path: Path = secret_file) -> int:
        args = ["agent", "release", "--service", "http://svc", "--account", account.address, "--time-ms", "1"]
        return main([*args, "--signature", "0x00", "--secret-file", str(secret_path), "--out", str(out)])

    out.write_text("keep me")
    assert run() == 1 and "already exists" in capsys.readouterr().out
    assert out.read_text() == "keep me" and not calls  # nothing was released
    out.unlink()
    (tmp_path / "typo.hex").write_text("not hex")
    assert run(tmp_path / "typo.hex") == 1 and not calls and not out.exists()
    refused = httpx.Response(403, json={"detail": "not signed by the account's master address"})
    answer[0] = httpx.HTTPStatusError("403", request=httpx.Request("POST", "http://svc"), response=refused)
    assert run() == 1 and "master address" in capsys.readouterr().out and not out.exists()
    answer[0] = encrypt_key(bytes(account.key), account.address, bytes(range(1, 33)))  # made with another secret
    assert run() == 1 and "does not open it" in capsys.readouterr().out and not out.exists()
    answer[0] = httpx.InvalidURL("Invalid port: '[::1'")  # a --service that is not a URL
    assert run() == 1 and "no usable answer" in capsys.readouterr().out and not out.exists()
    answer[0] = RuntimeError("unexpected")
    with pytest.raises(RuntimeError):
        run()
    assert not out.exists()  # no empty file left to block the next run
    answer[0] = encrypt_key(bytes(account.key), account.address, secret)
    assert run() == 0 and Account.from_key(out.read_text().strip()).address == account.address
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_the_withdraw_message_needs_a_real_amount(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    account = "0x" + "ab" * 20
    for amount in (
        [],
        ["--amount", ""],
        ["--amount", "ten"],
        ["--amount", "0"],
        ["--amount", "-5"],
        ["--amount", "1e3"],
        ["--amount", "\u0662\u0665"],  # digits, but not ones the exchange reads
        ["--amount", "\uff11\uff10"],
    ):
        assert main(["agent", "message", "withdraw", "--account", account, *amount]) == 1
        assert "--amount" in capsys.readouterr().out
    assert main(["agent", "message", "withdraw", "--account", account, "--amount", "25.5"]) == 0
    printed = capsys.readouterr().out
    message = json.loads(printed.splitlines()[0])["message"]
    assert message.startswith(f"kokoah-agent-withdraw-1 account={account} amount=25.5 time=")
    assert '"amount_usd": "25.5"' in printed


def test_every_agent_message_shows_its_deadline(capsys: pytest.CaptureFixture[str]) -> None:
    import json
    from datetime import UTC, datetime

    for kind in (["withdraw", "--amount", "1"], ["stop"], ["release"]):
        assert main(["agent", "message", *kind, "--account", "0x" + "cd" * 20]) == 0
        printed = capsys.readouterr().out
        first = json.loads(printed.splitlines()[0])
        assert first["deadline_ms"] - first["time_ms"] == 300_000  # what the harness and the service accept
        deadline = datetime.fromtimestamp(first["deadline_ms"] / 1000, UTC).strftime("%H:%M:%S UTC")
        assert f"before {deadline}" in printed


def test_agent_bind_takes_only_an_http_endpoint_with_its_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wallet = bt.Wallet(name="miner", hotkey="hk", path=str(tmp_path / "wallets"))
    wallet.create_new_hotkey(use_password=False, overwrite=True, suppress=True)
    asked: list[str] = []

    def get(url: str, **kw: Any) -> httpx.Response:
        asked.append(url)
        if "203.0.113.9" in url:
            raise httpx.ConnectError("refused")
        answer = {"hotkey": "5Other", "netuid": 385, "code_sha256": "ab" * 32, "address": "0x" + "12" * 20}
        return httpx.Response(200, json={**answer, "signature": "0x00"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)

    def bind(endpoint: str) -> int:
        args = ["agent", "bind", "--netuid", "385", "--endpoint", endpoint, "--wallet", "miner"]
        return main([*args, "--wallet-hotkey", "hk", "--wallet-path", str(tmp_path / "wallets")])

    for wrong in ("203.0.113.5:8080", "https://203.0.113.5", "http://203.0.113.5", "http://203.0.113.5:99999"):
        assert bind(wrong) == 1 and "http://<host>:<port>" in capsys.readouterr().out
    for private in ("http://127.0.0.1:8080", "http://10.0.0.5:8080", "http://192.168.1.2:8080"):
        assert bind(private) == 1 and "public" in capsys.readouterr().out
    assert bind("http://no-such-host.invalid:8080") == 1 and "does not resolve" in capsys.readouterr().out
    assert not asked  # nothing was contacted
    assert bind("http://203.0.113.9:8080") == 1 and "cannot read the machine" in capsys.readouterr().out
    assert bind("http://203.0.113.5:8080/") == 1 and "set up for hotkey 5Other" in capsys.readouterr().out
    assert asked[-1] == "http://203.0.113.5:8080/binding"


def test_address_options_refuse_typos(capsys: pytest.CaptureFixture[str]) -> None:
    from kokoah_subnet.cli import build_parser

    for wrong in (MASTER.swapcase().replace("X", "x"), MASTER[:-1], MASTER[2:]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["agent", "secret", "--master", wrong, "--out", "s.hex", "--generate"])
        assert "--master" in capsys.readouterr().err
    args = build_parser().parse_args(["agent", "secret", "--master", MASTER, "--out", "s.hex", "--generate"])
    assert args.master == MASTER.lower()
    with pytest.raises(SystemExit):
        build_parser().parse_args(["agent", "message", "stop", "--account", "0x123"])


def test_missing_folders_files_and_services_are_reported_plainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wallet = bt.Wallet(name="miner", hotkey="hk", path=str(tmp_path / "wallets"))
    wallet.create_new_hotkey(use_password=False, overwrite=True, suppress=True)
    signer = ["--wallet", "miner", "--wallet-hotkey", "hk", "--wallet-path", str(tmp_path / "wallets")]
    nowhere = tmp_path / "no-such-folder" / "secret.hex"
    assert main(["agent", "secret", "--master", MASTER, "--out", str(nowhere), "--generate"]) == 1
    assert "cannot create" in capsys.readouterr().out
    assert main(["agent", "submit", "--service", "http://svc", "--bundle", str(tmp_path / "none"), *signer]) == 1
    assert "cannot read" in capsys.readouterr().out

    def down(*a: Any, **kw: Any) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", down)
    monkeypatch.setattr(httpx, "get", down)
    (tmp_path / "b.bundle").write_bytes(b"PK")
    assert main(["agent", "submit", "--service", "http://svc", "--bundle", str(tmp_path / "b.bundle"), *signer]) == 1
    assert "cannot reach" in capsys.readouterr().out
    assert main(["miner", "unlink-message", "--hotkey", wallet.hotkey.ss58_address, "--address", MASTER]) == 1
    assert "cannot reach" in capsys.readouterr().out
