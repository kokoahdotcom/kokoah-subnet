"""Agent bundles: the miner's code as one reproducible zip, its fingerprint, and optional encryption.

- `package(dir)` zips the directory deterministically (sorted paths, fixed timestamps and permissions), so the
  same files always give the same bytes and the same fingerprint. The directory needs an `entry.py`; packages the
  agent imports go inside it (the agent has no network, so nothing is installed at run time). Version-control and
  cache folders are left out, and a file that looks like a key or a secret is refused, never packaged.
- The **code fingerprint** is the sha256 of that zip. The miner commits it on chain; the harness computes it from
  what it actually runs and puts it in the attestation. Private code reveals nothing through it.
- `encrypt(zip, key)` for private code: AES-256-GCM with a fresh 32-byte key that only the miner holds and passes
  to the sealed machine at start. The owner's service stores only the encrypted blob.
- `unpack(zip, dir)` refuses absolute paths, `..`, links, and archives above the size limits.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"KAB1"  # Kokoah agent bundle, encrypted, version 1
MAX_BUNDLE_BYTES = 20 * 2**20
MAX_UNPACKED_BYTES = 200 * 2**20
MAX_FILES = 5000
_EPOCH = (1980, 1, 1, 0, 0, 0)
LEFT_OUT = frozenset({".git", ".hg", ".svn", "__pycache__"})  # folders that are never the agent's code
SECRET_FILES = (
    ".env",
    ".env.*",
    "*.key",
    "*.mnemonic",
    "*.seed",
    "*.p12",
    "*.ppk",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "secret.hex",
    "key.txt",
    "coldkey",  # a Bittensor wallet copied without its wallets folder
    "coldkeypub.txt",
    "utc--*",  # an Ethereum keystore file
)
SECRET_FOLDERS = frozenset({"wallets", ".bittensor", "hotkeys", ".ssh"})  # a wallet's hotkeys are not encrypted
# a private key in PEM or PGP armor, whatever its kind: the header, then the key's base64 within a short window (past
# armor header lines, indentation, quotes or escaped line breaks), before anything else that starts with dashes. A
# library that only names the header in its code (as key parsers do) has no key text after it, so it is packaged.
PRIVATE_KEY_HEADER = re.compile(rb"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----")
KEY_TEXT = re.compile(rb"[A-Za-z0-9+/]{32,}")
KEY_WINDOW = 2048
KEY_FILE_MAX = 64 * 1024  # an encrypted key file is small JSON


def holds_private_key(content: bytes) -> bool:
    for header in PRIVATE_KEY_HEADER.finditer(content):
        window = content[header.end() : header.end() + KEY_WINDOW].replace(b"\\n", b"\n").replace(b"\\r", b"\n")
        end = window.find(b"-----")
        if KEY_TEXT.search(window if end < 0 else window[:end]):
            return True
    return False


def holds_key_file(content: bytes) -> bool:
    """An Ethereum keystore (`crypto` with `ciphertext` and `kdf`) or a Bittensor key file (`secretPhrase` or
    `secretSeed`), whatever its name: judged by its JSON, so code that only names those fields is packaged."""
    if len(content) > KEY_FILE_MAX or content.lstrip()[:1] != b"{":
        return False
    try:
        data = json.loads(content)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    crypto = data.get("crypto", data.get("Crypto"))
    return (isinstance(crypto, dict) and "ciphertext" in crypto and "kdf" in crypto) or bool(
        {"secretPhrase", "secretSeed"} & set(data)
    )


def looks_secret(relative: Path, content: bytes = b"") -> bool:
    """A wallet, a key or a secret by its name, in any case, or a file holding a private key or an encrypted key
    file: the bundle may be published, and the code has no use for one. Certificate files (a vendored package's
    `cacert.pem`) are fine."""
    name = relative.name.lower()
    return (
        any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_FILES)
        or bool(SECRET_FOLDERS & {part.lower() for part in relative.parts[:-1]})
        or holds_private_key(content)
        or holds_key_file(content)
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package(directory: Path) -> bytes:
    root = Path(directory)
    if not (root / "entry.py").is_file():
        raise ValueError("the agent directory needs an entry.py")
    paths = sorted(p for p in root.rglob("*") if p.is_file() and not LEFT_OUT & set(p.relative_to(root).parts))
    secrets = [p.relative_to(root).as_posix() for p in paths if looks_secret(p.relative_to(root), p.read_bytes())]
    if secrets:
        raise ValueError(f"these look like keys or secrets, move them out of the agent directory: {', '.join(secrets)}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            if p.is_symlink():
                raise ValueError(f"links are not allowed: {p}")
            info = zipfile.ZipInfo(p.relative_to(root).as_posix(), date_time=_EPOCH)
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, p.read_bytes())
    data = buf.getvalue()
    if len(data) > MAX_BUNDLE_BYTES:
        raise ValueError(f"bundle is {len(data)} bytes, above the {MAX_BUNDLE_BYTES} limit")
    return data


def new_key() -> bytes:
    return AESGCM.generate_key(256)


def encrypt(plain: bytes, key: bytes) -> bytes:
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plain, MAGIC)


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def decrypt(blob: bytes, key: bytes) -> bytes:
    if not is_encrypted(blob):
        raise ValueError("not an encrypted bundle")
    nonce, ciphertext = blob[4:16], blob[16:]
    return AESGCM(key).decrypt(nonce, ciphertext, MAGIC)


def check_zip(plain: bytes) -> list[zipfile.ZipInfo]:
    """The entries of a bundle zip, after every safety check (raises ValueError)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(plain))
    except zipfile.BadZipFile as exc:
        raise ValueError("not a zip file") from exc
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise ValueError("too many files")
    if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        raise ValueError("unpacked size above the limit")
    names = set()
    for i in infos:
        name = i.filename
        if name.startswith("/") or "\\" in name or any(part == ".." for part in name.split("/")):
            raise ValueError(f"unsafe path: {name}")
        if stat.S_ISLNK(i.external_attr >> 16):
            raise ValueError(f"links are not allowed: {name}")
        names.add(name)
    if "entry.py" not in names:
        raise ValueError("the bundle needs an entry.py at its top level")
    return infos


def unpack(plain: bytes, target: Path) -> None:
    check_zip(plain)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(plain)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            out = (target / info.filename).resolve()
            if target.resolve() not in out.parents:
                raise ValueError(f"unsafe path: {info.filename}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(info))
