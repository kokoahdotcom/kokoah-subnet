"""The miner's secret for a sealed machine: only a strong password (checked by the tool) or random bytes, stretched
with scrypt and salted with the miner's address; the machine refuses a secret that is clearly not random."""

import io
import stat
import sys
from pathlib import Path

import pytest

from kokoah_subnet.agent.keystore import check_secret
from kokoah_subnet.cli import main
from kokoah_subnet.miner_secret import derive_secret, password_problems, random_secret

MASTER = "0x1c8970117aD600590a21A4D0651865790F7A7253"
STRONG = "rapid-olive-tundra-cobalt-mural-ember"


@pytest.mark.parametrize(
    "password",
    ["short-one", "password123456789", "kokoah kokoah kokoah kokoah", "qwertyuiopasdfghjkl", "aaaaaaaaaaaaaaaaaaaa"],
)
def test_weak_passwords_are_refused(password: str) -> None:
    assert password_problems(password)


def test_a_password_built_on_the_miners_own_words_is_refused() -> None:
    hotkey = "5FAATKVCG4wwonNBm4Z5Hf132AyWY5JFEyt9Q7on37La7adM"
    assert not password_problems(hotkey + "!")  # strong-looking to someone who does not know the hotkey
    assert password_problems(hotkey + "!", (hotkey,))


def test_a_strong_passphrase_passes_and_always_makes_the_same_secret() -> None:
    assert password_problems(STRONG) == []
    s = derive_secret(STRONG, MASTER)
    assert len(s) == 32 and s == derive_secret(STRONG, MASTER.lower())
    assert s != derive_secret(STRONG, "0x" + "11" * 20)  # salted with the address: no shared table of guesses
    assert s != derive_secret(STRONG + "x", MASTER)
    check_secret(s)
    check_secret(random_secret())


@pytest.mark.parametrize(
    "secret",
    [bytes(32), bytes(range(32)), b"my-hyperliquid-machine-secret-01", bytes([7, 9] * 16), b"\x01" * 64],
)
def test_the_machine_refuses_a_secret_that_is_clearly_not_random(secret: bytes) -> None:
    with pytest.raises(ValueError, match="too weak"):
        check_secret(secret)


def test_the_tool_writes_the_secret_only_for_a_strong_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weak = tmp_path / "weak.hex"
    monkeypatch.setattr(sys, "stdin", io.StringIO("password123456789\n"))
    assert main(["agent", "secret", "--master", MASTER, "--out", str(weak), "--password-stdin"]) == 1
    assert not weak.exists()
    good = tmp_path / "good.hex"
    monkeypatch.setattr(sys, "stdin", io.StringIO(STRONG + "\n"))
    assert main(["agent", "secret", "--master", MASTER, "--out", str(good), "--password-stdin"]) == 0
    assert bytes.fromhex(good.read_text()) == derive_secret(STRONG, MASTER)
    assert stat.S_IMODE(good.stat().st_mode) == 0o600
    monkeypatch.setattr(sys, "stdin", io.StringIO(STRONG + "\n"))
    assert main(["agent", "secret", "--master", MASTER, "--out", str(good), "--password-stdin"]) == 1  # kept
    rnd = tmp_path / "random.hex"
    assert main(["agent", "secret", "--master", MASTER, "--out", str(rnd), "--generate"]) == 0
    check_secret(bytes.fromhex(rnd.read_text()))
