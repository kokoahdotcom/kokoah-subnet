"""The command line parses, and checks a miner's input before anything is published, without touching a network."""

from pathlib import Path

import bittensor as bt
import pytest
from eth_account import Account

from kokoah_subnet.binding import sign_binding
from kokoah_subnet.cli import build_parser, main


def test_info_defaults_to_the_localnet() -> None:
    args = build_parser().parse_args(["info"])
    assert args.command == "info"
    assert args.network == "local"
    assert args.json is False


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_a_migration_bind_says_which_signature_is_wrong(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wallet = bt.Wallet(name="miner", hotkey="hk", path=str(tmp_path / "wallets"))
    wallet.create_new_hotkey(use_password=False, overwrite=True, suppress=True)
    hotkey = wallet.hotkey.ss58_address
    new, old = Account.create(), Account.create()
    good_new = sign_binding(new.key, 385, hotkey, new.address)
    good_old = sign_binding(old.key, 385, hotkey, new.address)
    wrong = sign_binding(old.key, 385, hotkey, old.address)  # the old address signed its own line

    def bind(signature: str, previous_signature: str) -> str:
        args = ["miner", "bind", "--netuid", "385", "--address", new.address, "--signature", signature]
        args += ["--previous-address", old.address, "--previous-signature", previous_signature]
        args += ["--wallet", "miner", "--wallet-hotkey", "hk", "--wallet-path", str(tmp_path / "wallets")]
        assert main(args) == 1
        return capsys.readouterr().out

    assert f"previous address {old.address.lower()}" in bind(good_new, wrong)
    assert f"the address {new.address.lower()}" in bind(wrong, good_old)
    assert f"address={new.address.lower()}" in bind(good_new, wrong)  # both sign the line for the new address
    args = ["miner", "bind", "--netuid", "385", "--address", new.address, "--signature", good_new]
    args += ["--previous-signature", good_old, "--wallet", "miner", "--wallet-path", str(tmp_path / "wallets")]
    assert main([*args, "--wallet-hotkey", "hk"]) == 1 and "together" in capsys.readouterr().out
