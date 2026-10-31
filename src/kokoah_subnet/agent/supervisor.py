"""The sealed machine's entry point (specification section 16).

Image settings (`/etc/kokoah-agent/image.json`, part of the image and so of its attested digest; never taken
from the environment, because changing them would change what the guarantees mean): Kokoah's builder address and
fee, the owner's key service (an https URL, its certificate checked), and whether this is a development image
(development images are never approved by validators). The key service is part of the image because whoever
starts the machine must not be able to put a relay in its place: the stored copy is encrypted with the miner's own
secret, and a restore hands a copy back.

Start settings (environment, passed by the miner when they start the machine):
  KOKOAH_AGENT_SUBMISSION   the submission id (sha256 of the stored bundle bytes)
  KOKOAH_AGENT_BUNDLE_KEY   hex AES key for a private (encrypted) bundle
  KOKOAH_AGENT_NETUID, KOKOAH_AGENT_HOTKEY   the subnet and hotkey the account is bound to
  KOKOAH_AGENT_MASTER       the miner's own address: the only withdrawal destination, and the only one that can
                            take the key out (which ends the agent bonus for the account)
  KOKOAH_AGENT_SECRET       hex, 32 bytes, kept by the miner (made with `kokoah-subnet agent secret`): encrypts the
                            stored copy of the key; a secret that is clearly not random is refused
  KOKOAH_AGENT_PERMIT       the miner's start permission (`kokoah-subnet agent authorize`): signed by the hotkey,
                            needed to file a new account's key copy; a restore does not need it
  KOKOAH_AGENT_RESTORE      after the machine stopped: the account address to resume on this new machine
  KOKOAH_AGENT_NETWORK      mainnet (default) or testnet; any other value stops the machine

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
import ctypes
import json
import logging
import os
import pwd
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from cryptography.exceptions import InvalidTag
from eth_account import Account

from . import sandbox
from .attest import KEYSTORE_AUDIENCE, LAUNCHER_SOCKET, ConfidentialSpaceAttestor, DevelopmentAttestor
from .bundle import decrypt, is_encrypted, sha256_hex, unpack
from .harness import Harness, HarnessConfig, agent_app, public_app
from .keystore import (
    KeyService,
    Restored,
    check_secret,
    checked_address,
    decrypt_key,
    encrypt_key,
    restore_digest,
    store_digest,
)

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
    service: str = ""  # the owner's key service

    @staticmethod
    def load(path: Path = IMAGE_SETTINGS) -> ImageSettings:
        raw = json.loads(path.read_text())
        return ImageSettings(
            builder=str(raw["builder"]).lower(),
            builder_fee_tenths_bps=int(raw.get("builder_fee_tenths_bps", 25)),
            development=bool(raw.get("development", False)),
            service=str(raw.get("service", "")),
        )


def service_url(image: ImageSettings, dev: bool, environ: Mapping[str, str]) -> str:
    """The key service this machine talks to: the image's own, over https. Only a development image run in
    development mode takes another (`KOKOAH_AGENT_SERVICE`, a local service for tests)."""
    dev = dev and image.development  # a release image never takes another service, whatever the caller says
    if dev and environ.get("KOKOAH_AGENT_SERVICE"):
        return environ["KOKOAH_AGENT_SERVICE"]
    if not image.service.startswith("https://"):
        if dev:
            raise SystemExit("set KOKOAH_AGENT_SERVICE to the local key service for a development run")
        raise SystemExit("the image names no https key service")
    return image.service


def development_mode(image: ImageSettings, environ: Mapping[str, str]) -> bool:
    """A development image asked to run as one (`KOKOAH_AGENT_DEV=1`); a release image never is, whatever is set."""
    return image.development and environ.get("KOKOAH_AGENT_DEV") == "1"


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit(f"{name} is required")
    return value


def address_setting(name: str, value: str) -> str:
    try:
        return checked_address(value)
    except ValueError as exc:
        raise SystemExit(f"{name}: {exc}") from None


def mainnet_setting(value: str) -> bool:
    """KOKOAH_AGENT_NETWORK: exactly mainnet or testnet (anything else stops the machine rather than guess)."""
    if value not in ("mainnet", "testnet"):
        raise SystemExit(f"KOKOAH_AGENT_NETWORK must be mainnet or testnet, not {value!r}")
    return value == "mainnet"


START_TRIES = 4  # each call to the owner's service at start: a passing network or service fault is tried again
START_PAUSE_S = 5.0  # doubled after every failed try
TRANSIENT_STATUS = frozenset({502, 503, 504, 520, 521, 522, 523, 524})  # 52x: Cloudflare could not reach the service


def retried[T](what: str, step: Callable[[], T]) -> T:
    """`step`, tried again after a connection fault or a gateway or unavailable answer; any other refusal is final."""
    for attempt in range(1, START_TRIES + 1):
        try:
            return step()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in TRANSIENT_STATUS or attempt == START_TRIES:
                raise
            reason = f"status {exc.response.status_code}"
        except httpx.TransportError as exc:
            if attempt == START_TRIES:
                raise
            reason = repr(exc)
        pause = START_PAUSE_S * 2 ** (attempt - 1)
        log.warning("%s failed (%s); trying again in %.0f s", what, reason, pause)
        time.sleep(pause)
    raise AssertionError("unreachable")


def fetch_code(service: str, submission: str, bundle_key: str | None, http: httpx.Client) -> tuple[bytes, str]:
    """The submitted bundle, checked against its id and decrypted; returns (plain zip, code fingerprint)."""
    url = f"{service.rstrip('/')}/agents/{submission}/bundle"
    blob = retried("fetching the submission", lambda: http.get(url).raise_for_status()).content
    if sha256_hex(blob) != submission:
        raise SystemExit("the fetched bundle does not match the submission id")
    if is_encrypted(blob):
        if not bundle_key:
            raise SystemExit("the bundle is encrypted: KOKOAH_AGENT_BUNDLE_KEY is required")
        try:
            plain = decrypt(blob, bytes.fromhex(bundle_key))
        except (InvalidTag, ValueError):
            raise SystemExit("KOKOAH_AGENT_BUNDLE_KEY does not open this submission") from None
    else:
        plain = blob
    return plain, sha256_hex(plain)


# What the agent must never reach: the launcher's folder, with the attestation socket (open to every user in
# Confidential Space, and mounted read-only) and the launcher's default token. Only the harness asks it for tokens.
HIDDEN_FROM_AGENT = [Path(LAUNCHER_SOCKET).parent]

# Prints the directories it can still see into; run as the agent, in the agent's sandbox.
_PROBE = (
    "import os, sys\n"
    "def seen(d):\n"
    "    try:\n        return bool(os.listdir(d))\n"
    "    except OSError:\n        return False\n"
    "print('\\n'.join(d for d in sys.argv[1:] if seen(d)))\n"
)


# The sandbox's own environment: fixed, and nothing from the bundle (it runs as root until it drops privileges)
LAUNCHER_ENV = {"PATH": "/usr/bin:/bin"}


def agent_environment(home: Path | None = None) -> dict[str, str]:
    """What the agent's program starts with. The sandbox sets it only at its last step, once the agent's user, view,
    limits and filter are in place: before that, a bundle file named like a module the sandbox imports (or
    `sitecustomize`) would run as root."""
    home = home or AGENT_HOME
    return {
        "KOKOAH_AGENT_SOCKET": str(AGENT_SOCKET),
        "PYTHONPATH": str(home),
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
    }


def agent_command(
    memory_mb: int,
    uid: int | None,
    gid: int | None,
    hide: list[str] | None = None,
    program: list[str] | None = None,
    environment: dict[str, str] | None = None,
) -> list[str]:
    # the sandbox runs as a plain script (standard library only) and isolated (-I: no environment variables, no user
    # or script directory on the import path), so the agent's environment needs no harness code and its bundle is
    # never on the sandbox's import path
    cmd = [sys.executable, "-I", str(Path(sandbox.__file__).resolve()), "--memory-mb", str(memory_mb)]
    if uid is not None:
        cmd += ["--uid", str(uid), "--gid", str(gid if gid is not None else uid)]
    for d in hide or []:
        cmd += ["--hide", d]
    for name, value in (environment or {}).items():
        cmd += ["--setenv", f"{name}={value}"]
    return [*cmd, "--", *(program or [sys.executable, str(AGENT_HOME / "entry.py")])]


def visible_to_agent(directories: list[str], uid: int | None, gid: int | None) -> list[str]:
    """Runs a probe exactly as the agent will run (its user, its sandbox, the same hidden directories) and returns
    the directories it can still see into. A sandbox that cannot start counts as seeing all of them."""
    program = [sys.executable, "-I", "-c", _PROBE, *directories]
    try:
        out = subprocess.run(
            agent_command(1024, uid, gid, directories, program),
            capture_output=True,
            text=True,
            timeout=60,
            env=LAUNCHER_ENV,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"{d} (the sandbox did not start: {exc})" for d in directories]
    if out.returncode != 0:
        return [f"{d} (the sandbox failed: {out.stderr.strip()[-300:]})" for d in directories]
    return [line for line in out.stdout.splitlines() if line]


def _status(pid: int | str) -> dict[bytes, bytes] | None:
    """A process's real user and state from /proc (read as bytes: a process names itself); None once it is gone."""
    try:
        status = Path(f"/proc/{pid}/status").read_bytes()
    except OSError:
        return None
    return dict(line.split(b":", 1) for line in status.splitlines() if line.startswith((b"Uid:", b"State:", b"PPid:")))


def _real_uid(fields: dict[bytes, bytes]) -> int | None:
    uids = fields.get(b"Uid", b"").split()
    try:
        return int(uids[0]) if uids else None
    except ValueError:
        return None


def processes_of(uid: int) -> list[int]:
    """The live (not yet exited) processes whose real user is `uid`."""
    found = []
    for entry in os.scandir("/proc"):
        if entry.name.isdigit() and (fields := _status(entry.name)) is not None:
            if _real_uid(fields) == uid and not fields.get(b"State", b"").strip().startswith((b"Z", b"X")):
                found.append(int(entry.name))
    return found


_libc = ctypes.CDLL(None, use_errno=True)
SYS_PIDFD_OPEN, SYS_PIDFD_SEND_SIGNAL = 434, 424  # the same on x86_64 and arm64


def _kill_if_owned(pid: int, uid: int) -> None:
    """SIGKILL `pid` when its real user is still `uid`, through a pidfd: the signal goes to the process that was
    looked at, never to another that got its id since."""
    fd = _libc.syscall(SYS_PIDFD_OPEN, pid, 0)
    if fd < 0:
        return  # gone already
    try:
        fields = _status(pid)
        if fields is not None and _real_uid(fields) == uid:
            _libc.syscall(SYS_PIDFD_SEND_SIGNAL, fd, signal.SIGKILL, None, 0)
    finally:
        os.close(fd)


def kill_user_processes(uid: int, rounds: int = 1000) -> None:
    """Every process of the agent's user ends, whatever it names itself or whichever group it moved to. The
    supervisor kills them itself (root: nothing the agent's user does can stop it), round after round until none is
    left: a process forked meanwhile is caught on the next round."""
    for _ in range(rounds):
        pids = processes_of(uid)
        if not pids:
            return
        for pid in pids:
            _kill_if_owned(pid, uid)
        time.sleep(0.005)
    log.warning("processes of uid %d still alive after %d rounds", uid, rounds)


class AgentRunner:
    """Runs the agent in the sandbox and restarts it (with a growing pause) until stopped. The sandbox starts from
    `LAUNCHER_ENV` in the agent's directory; the command carries the agent's own environment (`agent_command`).
    Whatever the agent started ends with it: before every start, after every exit and on stop, the sandbox itself is
    killed while it runs (it is root until it drops to the agent's user) and every process of the agent's user is
    killed (without a user of its own, a development run: the agent's process group), so nothing it forked keeps
    trading through the harness socket or is left behind when the next one starts. Exited orphans (the supervisor is
    the container's first process) are collected every `reap_s`."""

    def __init__(
        self, command: list[str], home: Path | None = None, uid: int | None = None, gid: int | None = None
    ) -> None:
        self.command = command
        self.home = home or AGENT_HOME
        self.uid, self.gid = uid, gid
        self.stopped = threading.Event()
        self.proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()  # starting, stopping and collecting orphans never overlap

    def _end_all(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:  # not collected yet: its id (and group) is still its own
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        if self.uid is not None:
            kill_user_processes(self.uid)
        reap_orphans(keep=proc.pid if proc is not None and proc.returncode is None else None)

    def end_all(self) -> None:
        with self._lock:
            self._end_all()

    def stop(self) -> None:
        self.stopped.set()
        self.end_all()

    def start(self) -> subprocess.Popen[bytes] | None:
        """A fresh agent, or None once stopped."""
        with self._lock:
            if self.stopped.is_set():
                return None
            self._end_all()
            self.proc = subprocess.Popen(
                self.command, env=LAUNCHER_ENV, cwd=str(self.home), close_fds=True, start_new_session=True
            )
            return self.proc

    def _exited(self, proc: subprocess.Popen[bytes]) -> None:
        """Right after the agent was collected: its group ends at once (a development run), before its id can be
        given to anything else; then everything of the agent's user."""
        with self._lock:
            if self.uid is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            self._end_all()

    def reap_loop(self, every_s: float = 30.0) -> None:
        while not self.stopped.wait(every_s):
            with self._lock:
                proc = self.proc
                reap_orphans(keep=proc.pid if proc is not None and proc.returncode is None else None)

    def run(self) -> None:
        threading.Thread(target=self.reap_loop, daemon=True).start()
        pause = 5.0
        while not self.stopped.is_set():
            started = time.time()
            proc = self.start()
            if proc is None:
                break
            code = proc.wait()
            self._exited(proc)
            if self.stopped.is_set():
                break
            pause = 5.0 if time.time() - started > 600 else min(pause * 2, 300.0)
            log.warning("agent exited with %s; restarting in %.0f s", code, pause)
            self.stopped.wait(pause)


def reap_orphans(keep: int | None = None) -> None:
    """In the sealed machine the supervisor is the container's first process, so the agent's orphans become its
    children: collect the ones that exited, by their own ids, never `keep` (the agent, which its Popen collects)."""
    me = str(os.getpid()).encode()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == keep:
            continue
        fields = _status(entry.name)
        if fields and fields.get(b"PPid", b"").strip() == me and fields.get(b"State", b"").strip().startswith(b"Z"):
            try:
                os.waitpid(int(entry.name), os.WNOHANG)
            except ChildProcessError:
                pass


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


def account_key(
    keys: KeyService,
    attestor: Any,
    secret: bytes,
    master: str,
    hotkey: str,
    restore: str | None,
    netuid: int = 0,
    submission: str = "",
    permit: str | None = None,
) -> bytes:
    """A restored key (`restore` names the account) or a new one whose encrypted copy is stored before use (with
    the miner's start permission)."""
    if restore:
        address = restore.lower()

        def restored() -> Restored:  # a fresh token for every try: a token is accepted once
            challenge = keys.challenge(address)
            return keys.restore(address, attestor.token([restore_digest(address, challenge)], KEYSTORE_AUDIENCE))

        copy = retried("restoring the key copy", restored)
        # the account resumes only for the master and hotkey it was filed for: a secret alone must not let anyone
        # restart it with withdrawals going to their own address
        if not copy.master or not copy.hotkey:
            raise SystemExit("the key service did not say whom the copy was filed for: the account is not resumed")
        if copy.master != master.lower() or copy.hotkey != hotkey:
            raise SystemExit(
                f"the account {address} was filed for master {copy.master} and hotkey {copy.hotkey}: start the machine "
                "with KOKOAH_AGENT_MASTER and KOKOAH_AGENT_HOTKEY set to those"
            )
        key = decrypt_key(copy.blob, address, secret)
        log.info("restored account %s", address)
        return key
    if not permit:
        raise SystemExit(
            "KOKOAH_AGENT_PERMIT is required for a new account: make it with `kokoah-subnet agent authorize`"
        )
    account = Account.create()
    address = account.address.lower()
    key = bytes(account.key)
    blob = encrypt_key(key, address, secret)
    token = attestor.token([store_digest(address, master, blob, hotkey, submission, permit)], KEYSTORE_AUDIENCE)
    tried = False

    def stored() -> None:
        nonlocal tried
        try:
            keys.store(address, master, hotkey, blob, token, netuid, submission, permit)
        except httpx.HTTPStatusError as exc:
            # an earlier try can have been filed with its answer lost; then the copy is kept under this new address,
            # which only this machine can file under
            if not (tried and exc.response.status_code == 409 and keys.kept(address)):
                raise
        finally:
            tried = True

    retried("storing the key copy", stored)
    log.info("created account %s; its encrypted copy is stored", address)
    return key


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    image = ImageSettings.load()
    dev = development_mode(image, os.environ)
    # certificates are checked and no proxy or certificate setting from the environment is read: the service is
    # reached as itself or not at all
    http = httpx.Client(timeout=60, trust_env=False)
    service = service_url(image, dev, os.environ)
    submission = env("KOKOAH_AGENT_SUBMISSION")
    plain, code = fetch_code(service, submission, os.environ.get("KOKOAH_AGENT_BUNDLE_KEY"), http)
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
    hidden = [str(d) for d in HIDDEN_FROM_AGENT if d.exists()]
    if uid is not None and hidden:  # the agent never reaches the attestation socket (only the harness asks it)
        seen = visible_to_agent(hidden, uid, gid)
        if seen:
            raise SystemExit(f"cannot hide the attestation socket from the agent: {seen}")
    if uid is not None and gid is not None:
        for p in [AGENT_HOME, *AGENT_HOME.rglob("*")]:
            os.chown(p, uid, gid)
    if dev:
        from cryptography.hazmat.primitives.asymmetric import rsa

        attestor: Any = DevelopmentAttestor(rsa.generate_private_key(public_exponent=65537, key_size=2048), "dev")
    else:
        attestor = ConfidentialSpaceAttestor()
    hotkey = env("KOKOAH_AGENT_HOTKEY")
    master = address_setting("KOKOAH_AGENT_MASTER", env("KOKOAH_AGENT_MASTER"))
    restore = os.environ.get("KOKOAH_AGENT_RESTORE") or None
    if restore is not None:
        restore = address_setting("KOKOAH_AGENT_RESTORE", restore)
    mainnet = mainnet_setting(env("KOKOAH_AGENT_NETWORK", "mainnet"))
    try:
        secret = bytes.fromhex(env("KOKOAH_AGENT_SECRET"))
        check_secret(secret)
    except ValueError as exc:
        raise SystemExit(f"KOKOAH_AGENT_SECRET: {exc}") from None
    key = account_key(
        KeyService(service, http),
        attestor,
        secret,
        master,
        hotkey,
        restore,
        int(env("KOKOAH_AGENT_NETUID")),
        submission,
        os.environ.get("KOKOAH_AGENT_PERMIT") or None,
    )
    config = HarnessConfig(
        netuid=int(env("KOKOAH_AGENT_NETUID")),
        hotkey=hotkey,
        master=master,
        code_sha256=code,
        builder=image.builder,
        builder_fee_tenths_bps=image.builder_fee_tenths_bps,
        mainnet=mainnet,
    )
    runner = AgentRunner(agent_command(1024, uid, gid, hidden, environment=agent_environment()), uid=uid, gid=gid)
    writes = not (dev and os.environ.get("KOKOAH_AGENT_NO_WRITES") == "1")
    harness = Harness(
        config, key, attestor, http=httpx.Client(timeout=20, trust_env=False), on_stop=runner.stop, allow_writes=writes
    )
    del key
    log.info("account %s, code %s, harness for netuid %d", harness.address, code, config.netuid)
    if writes:
        threading.Thread(target=funding_loop, args=(harness, runner.stopped), daemon=True).start()
    threading.Thread(target=runner.run, daemon=True).start()
    asyncio.run(serve(harness, uid, gid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
