"""The sealed machine's entry point (specification v6, section 16).

Image settings (`/etc/kokoah-agent/image.json`, part of the image and so of its attested digest; never taken
from the environment, because changing them would change what the guarantees mean): Kokoah's builder address and
fee, and whether this is a development image (development images are never approved by validators).

Start settings (environment, passed by the miner when they start the machine):
  KOKOAH_AGENT_SERVICE      the owner's agent service URL
  KOKOAH_AGENT_SUBMISSION   the submission id (sha256 of the stored bundle bytes)
  KOKOAH_AGENT_BUNDLE_KEY   hex AES key for a private (encrypted) bundle
  KOKOAH_AGENT_NETUID, KOKOAH_AGENT_HOTKEY   the subnet and hotkey the account is bound to
  KOKOAH_AGENT_MASTER       the miner's own address: the only withdrawal destination
  KOKOAH_AGENT_NETWORK      mainnet (default) or testnet

The account key is created in memory when the machine starts and is never written anywhere: a machine that stops
takes its key with it, and the account can no longer be used (withdraw before stopping).

Steps: fetch the submission and check its id; decrypt it inside the machine; compute the code fingerprint;
unpack it for the agent user; create the account key in memory; start the harness (agent socket and public
interface); approve Kokoah's builder fee once the account is funded; run the agent in the sandbox, restarting it
when it exits (the agent restarts, the machine and its key do not).
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


def approve_builder_loop(h: Harness, stop: threading.Event) -> None:
    while not h.builder_approved and not stop.is_set():
        try:
            result = h.approve_builder()
            log.info("builder fee approval: %s", result)
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
    key = bytes(Account.create().key)  # in memory only, never written anywhere
    config = HarnessConfig(
        netuid=int(env("KOKOAH_AGENT_NETUID")),
        hotkey=hotkey,
        master=env("KOKOAH_AGENT_MASTER").lower(),
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
        threading.Thread(target=approve_builder_loop, args=(harness, runner.stopped), daemon=True).start()
    threading.Thread(target=runner.run, daemon=True).start()
    asyncio.run(serve(harness, uid, gid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
