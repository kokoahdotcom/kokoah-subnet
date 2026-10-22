"""Agent bundles: reproducible fingerprints, encryption for private code, safe unpacking."""

import io
import zipfile
from pathlib import Path

import pytest

from kokoah_subnet.agent.bundle import check_zip, decrypt, encrypt, is_encrypted, new_key, package, sha256_hex, unpack


def agent_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agent"
    (d / "lib").mkdir(parents=True)
    (d / "entry.py").write_text("print('hello')\n")
    (d / "lib" / "signals.py").write_text("X = 1\n")
    return d


def test_the_same_files_always_give_the_same_fingerprint(tmp_path: Path) -> None:
    d = agent_dir(tmp_path)
    first = package(d)
    (d / "entry.py").touch()  # a newer timestamp changes nothing
    assert package(d) == first
    (d / "entry.py").write_text("print('changed')\n")
    assert sha256_hex(package(d)) != sha256_hex(first)


def test_private_code_round_trips_and_needs_the_key(tmp_path: Path) -> None:
    plain = package(agent_dir(tmp_path))
    key = new_key()
    blob = encrypt(plain, key)
    assert is_encrypted(blob) and plain not in blob
    assert decrypt(blob, key) == plain
    with pytest.raises(Exception):
        decrypt(blob, new_key())


def test_unpack_and_refuse_unsafe_archives(tmp_path: Path) -> None:
    plain = package(agent_dir(tmp_path))
    unpack(plain, tmp_path / "out")
    assert (tmp_path / "out" / "lib" / "signals.py").read_text() == "X = 1\n"
    for name in ("../evil.py", "/etc/passwd"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("entry.py", "")
            zf.writestr(name, "x")
        with pytest.raises(ValueError, match="unsafe"):
            check_zip(buf.getvalue())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.py", "")
    with pytest.raises(ValueError, match="entry.py"):
        check_zip(buf.getvalue())
    with pytest.raises(ValueError, match="entry.py"):
        package(tmp_path / "out" / "lib")
