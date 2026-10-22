"""Agent bundles: reproducible fingerprints, encryption for private code, safe unpacking."""

import io
import zipfile
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

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
    with pytest.raises(InvalidTag):
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


def test_the_service_checks_plain_bundles_and_records_their_fingerprint(tmp_path: Path) -> None:
    import base64

    from fastapi.testclient import TestClient

    from kokoah_subnet.agent.service import create_app

    svc = TestClient(create_app(tmp_path / "svc", verify=lambda m, s, h: True, images=["sha256:aa"]))
    plain = package(agent_dir(tmp_path))
    r = svc.post("/v1/agents", json={"hotkey": "h1", "bundle": base64.b64encode(plain).decode(), "signature": "00"})
    assert r.status_code == 200 and r.json()["code_sha256"] == sha256_hex(plain)
    assert svc.get(f"/v1/agents/{sha256_hex(plain)}/bundle").content == plain
    assert svc.get("/v1/images").json() == {"images": ["sha256:aa"]}
    bad = base64.b64encode(b"not a zip").decode()
    assert svc.post("/v1/agents", json={"hotkey": "h2", "bundle": bad, "signature": "00"}).status_code == 400
    assert svc.get("/v1/agents/" + "0" * 64 + "/bundle").status_code == 404
    assert svc.get("/v1/agents/../../etc/bundle").status_code in (400, 404)
