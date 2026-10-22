"""The sealed machine's entry point (specification v6, section 16).

Image settings (`/etc/kokoah-agent/image.json`, part of the image and so of its attested digest; never taken
from the environment, because changing them would change what the guarantees mean): Kokoah's builder address and
fee, and whether this is a development image (development images are never approved by validators).

Start settings (environment, passed by the miner when they start the machine):
  KOKOAH_AGENT_SERVICE      the owner's agent service URL
  KOKOAH_AGENT_SUBMISSION   the submission id (sha256 of the stored bundle bytes)
  KOKOAH_AGENT_BUNDLE_KEY   hex AES key for a private (encrypted) bundle
  KOKOAH_AGENT_NETUID, KOKOAH_AGENT_HOTKEY   the subnet and hotkey the account is bound to
  KOKOAH_AGENT_MASTER       the miner's own address: the only withdrawal destination, and the only one that can
                            take the key out (which ends the agent bonus for the account)
  KOKOAH_AGENT_SECRET       hex, at least 32 bytes, kept by the miner: encrypts the stored copy of the key
  KOKOAH_AGENT_RESTORE      after the machine stopped: the account address to resume on this new machine
  KOKOAH_AGENT_NETWORK      mainnet (default) or testnet

The account key lives in memory. A copy encrypted with the miner's secret is stored with the owner's service before
anything else happens (with this machine's attestation), so a machine that stops can be replaced: a new machine
started with the same secret and `KOKOAH_AGENT_RESTORE` proves it is a genuine sealed machine and gets the copy back.

Steps: fetch the submission and check its id; decrypt it inside the machine; compute the code fingerprint;
unpack it for the agent user; create (and store) or restore the account key; start the harness (agent socket and public
interface); move USDC that lands in spot to perpetuals and approve Kokoah's builder fee once the account is funded;
run the agent in the sandbox, restarting it when it exits (the agent restarts, the machine and its key do not).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from eth_account import Account

from . import sandbox
from .attest import ConfidentialSpaceAttestor, DevelopmentAttestor
from .bundle import decrypt, is_encrypted, sha256_hex, unpack
from .harness import Harness, HarnessConfig, agent_app, public_app
from .keystore import KeyService, decrypt_key, encrypt_key, restore_digest, store_digest

log = logging.getLogger("kokoah_subnet.agent")
IMAGE_SETTINGS = Path("/etc/kokoah-agent/image.json")
AGENT_SOCKET = Path("/run/kokoah/agent.sock")
AGENT_HOME = Path("/home/agent/app")
AGENT_USER = "agent"
PUBLIC_PORT = 8080


@dataclass(frozen=True)
class ImageSettings:
    builder: str
    builder_fee_tenths_bps: int
    development: bool = False

    @staticmethod
    def load(path: Path = IMAGE_SETTINGS) -> ImageSettings:
        raw = json.loads(path.read_text())
        return ImageSettings(
            builder=str(raw["builder"]).lower(),
            builder_fee_tenths_bps=int(raw.get("builder_fee_tenths_bps", 25)),
            development=bool(raw.get("development", False)),
        )


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit(f"{name} is required")
    return value


def fetch_code(service: str, submission: str, bundle_key: str | None, http: httpx.Client) -> tuple[bytes, str]:
    """The submitted bundle, checked against its id and decrypted; returns (plain zip, code fingerprint)."""
    r = http.get(f"{service.rstrip('/')}/v1/agents/{submission}/bundle")
    r.raise_for_status()
    blob = r.content
    if sha256_hex(blob) != submission:
        raise SystemExit("the fetched bundle does not match the submission id")
    if is_encrypted(blob):
        if not bundle_key:
            raise SystemExit("the bundle is encrypted: KOKOAH_AGENT_BUNDLE_KEY is required")
        plain = decrypt(blob, bytes.fromhex(bundle_key))
    else:
        plain = blob
    return plain, sha256_hex(plain)


def agent_command(memory_mb: int, uid: int | None, gid: int | None) -> list[str]:
    # the sandbox runs as a plain script (standard library only), so the agent's environment needs no harness code
    cmd = [sys.executable, str(Path(sandbox.__file__).resolve()), "--memory-mb", str(memory_mb)]
    if uid is not None:
        cmd += ["--uid", str(uid), "--gid", str(gid if gid is not None else uid)]
    return [*cmd, "--", sys.executable, str(AGENT_HOME / "entry.py")]


class AgentRunner:
    """Runs the agent in the sandbox and restarts it (with a growing pause) until stopped."""

    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        self.command = command
        self.env = env
        self.stopped = threading.Event()
        self.proc: subprocess.Popen[bytes] | None = None

    def stop(self) -> None:
        self.stopped.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def run(self) -> None:
        pause = 5.0
        while not self.stopped.is_set():
            started = time.time()
            self.proc = subprocess.Popen(self.command, env=self.env, cwd=str(AGENT_HOME), close_fds=True)
            code = self.proc.wait()
            if self.stopped.is_set():
                break
            pause = 5.0 if time.time() - started > 600 else min(pause * 2, 300.0)
            log.warning("agent exited with %s; restarting in %.0f s", code, pause)
            self.stopped.wait(pause)


def funding_loop(h: Harness, stop: threading.Event) -> None:
    """Every minute: move USDC that landed in spot to perpetuals, and approve Kokoah's builder fee until it is."""
    while not stop.is_set():
        try:
            moved = h.fund_perps()
            if moved is not None:
                log.info("spot USDC moved to perpetuals: %s", moved)
        except Exception as exc:  # noqa: BLE001 - retried next minute
            log.info("spot USDC not moved: %s", exc)
        if not h.builder_approved:
            try:
                log.info("builder fee approval: %s", h.approve_builder())
            except Exception as exc:  # noqa: BLE001 - an unfunded account cannot approve yet: retry
                log.info("builder fee approval not possible yet: %s", exc)
        stop.wait(60)


async def serve(h: Harness, uid: int | None, gid: int | None) -> None:
    AGENT_SOCKET.parent.mkdir(parents=True, exist_ok=True)
    if AGENT_SOCKET.exists():
        AGENT_SOCKET.unlink()
    agent_server = uvicorn.Server(uvicorn.Config(agent_app(h), uds=str(AGENT_SOCKET), log_level="warning"))
    public_server = uvicorn.Server(
        uvicorn.Config(public_app(h), host="0.0.0.0", port=PUBLIC_PORT, log_level="warning")  # noqa: S104
    )

    async def fix_socket_permissions() -> None:
        while not AGENT_SOCKET.exists():
            await asyncio.sleep(0.05)
        if gid is not None:
            os.chown(AGENT_SOCKET, 0, gid)
        os.chmod(AGENT_SOCKET, 0o660)

    await asyncio.gather(agent_server.serve(), public_server.serve(), fix_socket_permissions())


def account_key(keys: KeyService, attestor: Any, secret: bytes, master: str, hotkey: str, restore: str | None) -> bytes:
    """A restored key (`restore` names the account) or a new one whose encrypted copy is stored before use."""
    if restore:
        address = restore.lower()
        challenge = keys.challenge(address)
        blob = keys.restore(address, attestor.token([restore_digest(address, challenge)]))
        key = decrypt_key(blob, address, secret)
        log.info("restored account %s", address)
        return key
    account = Account.create()
    address = account.address.lower()
    key = bytes(account.key)
    blob = encrypt_key(key, address, secret)
    keys.store(address, master, hotkey, blob, attestor.token([store_digest(address, master, blob)]))
    log.info("created account %s; its encrypted copy is stored", address)
    return key


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    image = ImageSettings.load()
    dev = image.development and os.environ.get("KOKOAH_AGENT_DEV") == "1"
    http = httpx.Client(timeout=60)
    service = env("KOKOAH_AGENT_SERVICE")
    plain, code = fetch_code(service, env("KOKOAH_AGENT_SUBMISSION"), os.environ.get("KOKOAH_AGENT_BUNDLE_KEY"), http)
    uid: int | None
    gid: int | None
    try:
        user: Any = pwd.getpwnam(AGENT_USER)
        uid, gid = int(user.pw_uid), int(user.pw_gid)
    except KeyError:
        if not dev:
            raise SystemExit("the agent user is missing from the image") from None
        uid = gid = None
    unpack(plain, AGENT_HOME)
    if uid is not None and gid is not None:
        for p in [AGENT_HOME, *AGENT_HOME.rglob("*")]:
            os.chown(p, uid, gid)
    if dev:
        from cryptography.hazmat.primitives.asymmetric import rsa

        attestor: Any = DevelopmentAttestor(rsa.generate_private_key(public_exponent=65537, key_size=2048), "dev")
    else:
        attestor = ConfidentialSpaceAttestor()
    hotkey = env("KOKOAH_AGENT_HOTKEY")
    master = env("KOKOAH_AGENT_MASTER").lower()
    key = account_key(
        KeyService(service, http),
        attestor,
        bytes.fromhex(env("KOKOAH_AGENT_SECRET")),
        master,
        hotkey,
        os.environ.get("KOKOAH_AGENT_RESTORE") or None,
    )
    config = HarnessConfig(
        netuid=int(env("KOKOAH_AGENT_NETUID")),
        hotkey=hotkey,
        master=master,
        code_sha256=code,
        builder=image.builder,
        builder_fee_tenths_bps=image.builder_fee_tenths_bps,
        mainnet=env("KOKOAH_AGENT_NETWORK", "mainnet") != "testnet",
    )
    runner = AgentRunner(
        agent_command(1024, uid, gid),
        {
            "KOKOAH_AGENT_SOCKET": str(AGENT_SOCKET),
            "PYTHONPATH": str(AGENT_HOME),
            "PATH": "/usr/bin:/bin",
            "HOME": str(AGENT_HOME),
        },
    )
    writes = not (dev and os.environ.get("KOKOAH_AGENT_NO_WRITES") == "1")
    harness = Harness(config, key, attestor, http=httpx.Client(timeout=20), on_stop=runner.stop, allow_writes=writes)
    del key
    log.info("account %s, code %s, harness for netuid %d", harness.address, code, config.netuid)
    if writes:
        threading.Thread(target=funding_loop, args=(harness, runner.stopped), daemon=True).start()
    threading.Thread(target=runner.run, daemon=True).start()
    asyncio.run(serve(harness, uid, gid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
