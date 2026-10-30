"""Agent bundles: reproducible fingerprints, encryption for private code, safe unpacking."""

import io
import time
import zipfile
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from kokoah_subnet.agent.bundle import check_zip, decrypt, encrypt, is_encrypted, new_key, package, sha256_hex, unpack

BODY = "MHcCAQEEIOdkh8MBVi6sNTH8Rn0Iw3/a8sHgV0Kp1b9lRvZ0cZfeoAoGCCqGSM49"  # a key line


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


def test_keys_and_secrets_are_never_packaged(tmp_path: Path) -> None:
    d = agent_dir(tmp_path)
    clean = package(d)
    (d / ".git" / "objects").mkdir(parents=True)
    (d / ".git" / "config").write_text("[core]\n")
    (d / "lib" / "__pycache__").mkdir()
    (d / "lib" / "__pycache__" / "signals.cpython-312.pyc").write_bytes(b"\0")
    assert package(d) == clean  # version control and caches are left out
    for name in (
        ".env",
        "secret.hex",
        "key.txt",
        "my-agent.bundle.key",
        "wallets/miner/coldkey",
        "deploy/id_ed25519",
        ".SSH/config",
        "certs/client.p12",
        "backup/Wallets/miner/notes.txt",  # folders in any case
        "miner/coldkey",  # a wallet copied without its wallets folder
        "miner/coldkeypub.txt",
        "miner/hotkeys/default",
        "keys/UTC--2026-10-29T00-00-00.000Z--1c8970117ad600590a21a4d0651865790f7a7253",  # an Ethereum keystore file
    ):
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("x")
        with pytest.raises(ValueError, match=name):
            package(d)
        (d / name).unlink()
    assert package(d) == clean
    # a PEM file is refused when it holds a private key, by its content and whatever its name
    (d / "lib" / "hot.pem").write_text(f"-----BEGIN EC PRIVATE KEY-----\n{BODY}\n-----END EC PRIVATE KEY-----\n")
    (d / "lib" / "notes.txt").write_text(f"-----BEGIN PRIVATE KEY-----\n{BODY}\n-----END PRIVATE KEY-----\n")
    with pytest.raises(ValueError, match="lib/hot.pem") as refused:
        package(d)
    assert "lib/notes.txt" in str(refused.value)
    (d / "lib" / "hot.pem").unlink()
    (d / "lib" / "notes.txt").unlink()
    # other kinds of keys by content or name
    for name, text in (
        (
            "lib/backup.asc",
            f"-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: GnuPG v1.4.11 (GNU/Linux)\nComment: https://example.org\n\n"
            f"{BODY}\n=AbCd\n-----END PGP PRIVATE KEY BLOCK-----\n",
        ),
        (
            "deploy/service-account.json",
            f'{{"private_key": "-----BEGIN PRIVATE KEY-----\\n{BODY}\\n-----END PRIVATE KEY-----\\n"}}',
        ),
        (
            "deploy/values.yaml",
            f"tls:\n  key: |\n    -----BEGIN RSA PRIVATE KEY-----\n    {BODY}\n    -----END RSA PRIVATE KEY-----\n",
        ),
        ("lib/store.json", '{"crypto": {"ciphertext": "ab", "kdf": "scrypt"}}'),
        ("lib/hk.json", '{"secretPhrase": "x", "accountId": "0x"}'),
        ("lib/server.ppk", "PuTTY-User-Key-File-3: ssh-ed25519\n"),
    ):
        (d / name).write_text(text)
        with pytest.raises(ValueError, match=name):
            package(d)
        (d / name).unlink()
    # a key parser that only names the header in its code is packaged
    (d / "lib" / "ssh.py").write_text(
        '_SK_START = b"-----BEGIN OPENSSH PRIVATE KEY-----"\n_SK_END = b"-----END OPENSSH PRIVATE KEY-----"\n'
    )
    # a key-file library that only names the fields is packaged too
    (d / "lib" / "keyfile.py").write_text('def decode(k):\n    return k["crypto"]["ciphertext"], k["crypto"]["kdf"]\n')
    names = zipfile.ZipFile(io.BytesIO(package(d))).namelist()
    assert "lib/ssh.py" in names and "lib/keyfile.py" in names
    (d / "lib" / "ssh.py").unlink()
    (d / "lib" / "keyfile.py").unlink()
    # more wrappings of a real key, and files that only mention the header
    escaped = BODY.replace("/", "\\/")
    for name, text in (
        ("deploy/php.json", f'{{"key": "-----BEGIN PRIVATE KEY-----\\n{escaped}\\n-----END PRIVATE KEY-----"}}'),
        ("deploy/key.env", f"KEY=-----BEGIN RSA PRIVATE KEY----- {BODY} {BODY} -----END RSA PRIVATE KEY-----\n"),
        ("deploy/bom.json", '\ufeff{"crypto": {"ciphertext": "ab", "kdf": "scrypt"}}'),
        ("deploy/twice.json", f'"-----BEGIN PRIVATE KEY-----\\\\n{BODY}\\\\n-----END PRIVATE KEY-----"'),
        (
            "deploy/narrow.pem",
            "-----BEGIN PRIVATE KEY-----\n" + "\n".join(BODY[i : i + 16] for i in range(0, 64, 16)) + "\n",
        ),
        (
            "deploy/oneline.env",
            "KEY=-----BEGIN RSA PRIVATE KEY----- Proc-Type: 4,ENCRYPTED DEK-Info: AES-128-CBC,0A1B2C3D "
            f"{BODY} -----END",
        ),
    ):
        (d / name).write_text(text)
        with pytest.raises(ValueError, match=name):
            package(d)
        (d / name).unlink()
    (d / "lib" / "test_ckeygen.py").write_text(
        'KEY = b"-----BEGIN OPENSSH PRIVATE KEY-----\\n" in testDisplayPublicKeyEncryptedPassphrasePromptCase\n'
        "    def test_displayPublicKeyEncryptedPassphrasePrompt(self):\n        pass\n"
    )
    (d / "lib" / "deep.json").write_text("{" + '"a":' + "[" * 20000 + "]" * 20000 + "}")
    (d / "lib" / "digest.py").write_text('HEADER = "-----BEGIN PRIVATE KEY-----"\nSHA = "' + "ab" * 32 + '"\n')
    names = zipfile.ZipFile(io.BytesIO(package(d))).namelist()
    assert {"lib/test_ckeygen.py", "lib/deep.json", "lib/digest.py"} <= set(names)
    for name in ("test_ckeygen.py", "deep.json", "digest.py"):
        (d / "lib" / name).unlink()
    # many headers with no key after them are checked quickly
    (d / "lib" / "many.txt").write_text("-----BEGIN RSA PRIVATE KEY-----\n" * 8000)
    started = time.monotonic()
    package(d)
    assert time.monotonic() - started < 2
    (d / "lib" / "many.txt").unlink()
    # a vendored package's certificate bundle is packaged
    (d / "certifi").mkdir()
    (d / "certifi" / "cacert.pem").write_text("-----BEGIN CERTIFICATE-----\nMIID\n-----END CERTIFICATE-----\n")
    assert "certifi/cacert.pem" in zipfile.ZipFile(io.BytesIO(package(d))).namelist()


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
    from fastapi.testclient import TestClient
    from uploads import upload

    from kokoah_subnet.agent.service import create_app

    svc = TestClient(create_app(tmp_path / "svc", verify=lambda m, s, h: True, images=["sha256:aa"]))
    plain = package(agent_dir(tmp_path))
    r = upload(svc, "h1", plain, "00")
    assert r.status_code == 200 and r.json()["code_sha256"] == sha256_hex(plain)
    assert svc.get(f"/agents/{sha256_hex(plain)}/bundle").content == plain
    assert svc.get("/images").json() == {"images": ["sha256:aa"]}
    assert upload(svc, "h2", b"not a zip", "00").status_code == 400
    assert svc.get("/agents/" + "0" * 64 + "/bundle").status_code == 404
    assert svc.get("/agents/../../etc/bundle").status_code in (400, 404)
