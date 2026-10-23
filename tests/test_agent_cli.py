"""The miner's and the owner's agent commands: the start permission is signed by the hotkey and checks out the way
the service checks it; one file per permission; the service key is created once, private."""

import stat
from pathlib import Path

import bittensor as bt
from bittensor.wallets import verify_message

from kokoah_subnet.agent.keystore import parse_permit, start_message
from kokoah_subnet.cli import main

MASTER = "0x1c8970117AD600590a21a4d0651865790f7a7253"
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
