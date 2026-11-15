"""Automatic updates of the Docker setup (`deploy/validator/`, docs/validators.md section 11).

Every change to main that reaches the validator image is checked, built and signed by the repository's release
workflow (`.github/workflows/release.yml`) and published as `kokoahdotcom/kokoah-subnet:latest`. The updater looks
at that tag every few minutes. When it names a new image, the updater installs it only if:

- the image carries a signature made by exactly that workflow on `main`, which GitHub vouches for through Sigstore:
  a leaked Docker Hub login can publish an image, but no updater installs it; and
- its build number (the workflow run, inside the signature) is not below the installed one's: an older signed image
  put back on the tag is refused.

It then tags the image `kokoah-subnet:current`, which the compose file runs (never a name from the registry), installs
the release's own compose file, restarts the validator and waits until the record is at the release's schema version,
restarts the other services (the interface reads only a migrated record), and lastly has a short-lived helper
container restart the updater itself.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

IMAGE = "docker.io/kokoahdotcom/kokoah-subnet"
CHANNEL = "latest"
CURRENT = "kokoah-subnet:current"
PREVIOUS = "kokoah-subnet:previous"
WORKFLOW_IDENTITY = "https://github.com/kokoahdotcom/kokoah-subnet/.github/workflows/release.yml@refs/heads/main"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
PROJECT_DIR = Path("/opt/kokoah-subnet/docker")
IMAGE_COMPOSE = "/opt/kokoah-subnet/compose.yml"  # the release's compose file, inside its image
STATE_FILE = "release.json"  # the last release installed and running well: digest, build, commit, schema, floor
PENDING_FILE = "installing.json"  # the release being switched to, until it is kept or left
REFUSED_FILE = "refused.json"  # releases this host went back from: digest -> why and when
LOCK_FILE = ".update.lock"  # one update at a time on a host
NEW_COMPOSE = ".compose.new.yml"  # a release's compose file while it is checked
VALIDATOR = "kokoah-subnet-validator"  # the containers' names (compose.yml)
UPDATER = "kokoah-subnet-updater"
VALIDATOR_STATE = "{{.State.Running}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{end}}"
HOP = "kokoah-subnet-updater-hop"
INTERVAL_S = 300
VALIDATOR_WAIT_S = 7200  # for the record's migration (the health check's start period in compose.yml matches)
STEADY_S = 120  # a new validator must then run this long without a restart
POLL_S = 10  # how often a starting validator is looked at
RETRY_S = 3600  # a release that could not be tried here is tried again after this long
PULL_RETRY_S = 900  # one that could not be downloaded, after this long
REFUSED_RETRY_S = 6 * 3600  # one this host went back from, after this long (then twice as long each time)
REFUSED_MAX_S = 7 * 86400
DOCKER_DOWN = ("cannot connect to the docker daemon", "error during connect", "is the docker daemon running")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class UpdateError(RuntimeError):
    pass


Runner = Callable[[list[str], float], str]


def run_command(args: list[str], timeout: float) -> str:
    """Run a command and return its output; any failure is an `UpdateError` carrying the command's own words."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"{args[0]} {args[1] if len(args) > 1 else ''}: {exc}") from None
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise UpdateError(f"{' '.join(args[:3])}: {detail[-1] if detail else f'exit {done.returncode}'}")
    return done.stdout


@dataclass(frozen=True)
class Release:
    digest: str
    build: int
    commit: str


def _split(image: str) -> tuple[str, str]:
    """`registry/repository` of an image name without tag: `docker.io/kokoahdotcom/kokoah-subnet` gives
    `("docker.io", "kokoahdotcom/kokoah-subnet")`."""
    registry, _, repo = image.partition("/")
    if not repo or ("." not in registry and ":" not in registry and registry != "localhost"):
        return "docker.io", image
    return registry, repo


def _same_repo(name: str, image: str) -> bool:
    """Whether a reference's repository (as Docker or cosign print it: `kokoahdotcom/x`, `index.docker.io/...`) is
    the image's."""
    registry, repo = _split(image)
    other_registry, other_repo = _split(name)
    docker_hub = {"docker.io", "index.docker.io", "registry-1.docker.io"}
    same_registry = other_registry == registry or (other_registry in docker_hub and registry in docker_hub)
    return same_registry and other_repo == repo


def remote_digest(image: str, tag: str, client: httpx.Client | None = None) -> str:
    """The digest a tag names, from a HEAD request: Docker Hub counts a pull only for a manifest download, so
    checking every few minutes costs nothing against its limits."""
    registry, repo = _split(image)
    if registry == "docker.io":
        base = "https://registry-1.docker.io"
    elif registry.split(":")[0] in ("localhost", "127.0.0.1"):
        base = f"http://{registry}"
    else:
        base = f"https://{registry}"
    url = f"{base}/v2/{repo}/manifests/{tag}"
    own = client is None
    http = client or httpx.Client(timeout=30)
    try:
        headers = {"Accept": MANIFEST_TYPES}
        response = http.head(url, headers=headers)
        if response.status_code == 401:
            headers["Authorization"] = f"Bearer {_token(http, response.headers.get('www-authenticate', ''))}"
            response = http.head(url, headers=headers)
        if response.status_code != 200:
            raise UpdateError(f"{image}:{tag}: the registry answered {response.status_code}")
        digest = str(response.headers.get("docker-content-digest", ""))
    except httpx.HTTPError as exc:
        raise UpdateError(f"{image}:{tag}: {exc}") from None
    finally:
        if own:
            http.close()
    if not DIGEST.fullmatch(digest):
        raise UpdateError(f"{image}:{tag}: the registry gave no digest")
    return digest


def _token(http: httpx.Client, challenge: str) -> str:
    """An anonymous pull token for a registry's `Bearer realm=...,service=...,scope=...` challenge."""
    scheme, _, rest = challenge.partition(" ")
    params = dict(re.findall(r'(\w+)="([^"]*)"', rest))
    if scheme.lower() != "bearer" or not params.get("realm", "").startswith("https://"):
        raise UpdateError(f"the registry asked for an unknown login: {challenge[:80]}")
    realm = params.pop("realm")
    response = http.get(realm, params=params)
    if response.status_code != 200:
        raise UpdateError(f"the registry's token service answered {response.status_code}")
    body = response.json()
    token = body.get("token") or body.get("access_token")
    if not isinstance(token, str) or not token:
        raise UpdateError("the registry's token service gave no token")
    return token


def verify(run: Runner, ref: str, image: str = IMAGE) -> Release:
    """The release a reference names, if the release workflow on main signed it (cosign, keyless: the certificate
    must be GitHub's for exactly that workflow and branch)."""
    out = run(
        [
            "cosign",
            "verify",
            ref,
            "--certificate-identity",
            WORKFLOW_IDENTITY,
            "--certificate-oidc-issuer",
            OIDC_ISSUER,
        ],
        180,
    )
    try:
        items = json.loads(out)
    except ValueError:
        raise UpdateError(f"{ref}: cosign printed no signatures") from None
    found: list[Release] = []
    for item in items if isinstance(items, list) else []:
        critical = item.get("critical") or {}
        optional = item.get("optional") or {}
        digest = (critical.get("image") or {}).get("docker-manifest-digest", "")
        signed_for = (critical.get("identity") or {}).get("docker-reference", "")
        build = str(optional.get("build", ""))
        if DIGEST.fullmatch(digest) and _same_repo(signed_for, image) and build.isdigit():
            found.append(Release(digest, int(build), str(optional.get("commit", ""))))
    if not found:
        raise UpdateError(f"{ref}: no signature of the release workflow for {image}")
    if len({r.digest for r in found}) != 1:
        raise UpdateError(f"{ref}: signatures for different images")
    if "@" in ref and ref.rsplit("@", 1)[1] != found[0].digest:
        raise UpdateError(f"{ref}: the signature is for another image")
    return max(found, key=lambda r: r.build)


def log(message: str) -> None:
    print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S} {message}", flush=True)


ROLLBACK_FLOOR_CODE = "from kokoah_subnet.validator import migrations as m; print(getattr(m, 'ROLLBACK_FLOOR', 0))"
SCHEMA_CODE = "from kokoah_subnet.validator.migrations import latest_version; print(latest_version())"
RECORD_SCHEMA_CODE = (
    "import os; from sqlalchemy import create_engine, text; "
    "c = create_engine(os.environ['KOKOAH_SUBNET_DB_URL']).connect(); "
    "print(c.execute(text(\"SELECT value FROM meta WHERE key = 'schema_version'\")).scalar() or 0)"
)


@dataclass(frozen=True)
class Installed:
    """A release on this host, with what its code knows of the record: the schema version it brings the record to,
    and its rollback floor (migrations.ROLLBACK_FLOOR). `schema` is None when it was not recorded."""

    release: Release
    schema: int | None
    floor: int


@dataclass(frozen=True)
class Outcome:
    ok: bool  # the release installed and its validator runs
    message: str


class Updater:
    """One validator host's Docker setup in `project`: its compose file and `.env`; the release that last installed
    and ran well (`release.json`, the state every look brings the host back to); the release being switched to
    (`installing.json`, while it is); and the releases held back from this host for a while (`refused.json`)."""

    def __init__(
        self,
        project: Path = PROJECT_DIR,
        run: Runner = run_command,
        head: Callable[[str, str], str] = remote_digest,
        image: str = IMAGE,
        say: Callable[[str], None] = log,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.project = project
        self.run = run
        self.head = head
        self.image = image
        self.say = say
        self.sleep = sleep
        self.clock = clock
        self.wall = wall
        self._failed: dict[str, tuple[str, float]] = {}  # digests that failed here lately: why, until (monotonic)
        self._repair_failed_at: float | None = None
        self._hop_at: float | None = None
        self._others_pending = True  # a new updater makes sure the other services run, once

    # ----- what is on the host -----------------------------------------------------------------------------

    def _docker(self, args: list[str], *missing: str) -> str | None:
        """A Docker read: None when Docker says the object does not exist, an error for anything else."""
        try:
            return self.run(args, 60).strip()
        except UpdateError as exc:
            if any(m in str(exc).lower() for m in missing):
                return None
            raise

    def installed_digest(self) -> str | None:
        """The registry digest of `kokoah-subnet:current`: None when there is none (or it is a local build)."""
        out = self._docker(
            ["docker", "image", "inspect", CURRENT, "--format", "{{json .RepoDigests}}"], "no such image"
        )
        if out is None:
            return None
        try:
            names = json.loads(out)
        except ValueError:
            raise UpdateError(f"docker image inspect {CURRENT}: unreadable output") from None
        for name in names if isinstance(names, list) else []:
            repo, _, digest = str(name).partition("@")
            if _same_repo(repo, self.image) and DIGEST.fullmatch(digest):
                return digest
        return None

    def image_id(self, ref: str) -> str | None:
        return self._docker(["docker", "image", "inspect", ref, "--format", "{{.Id}}"], "no such image")

    def container_image(self, name: str) -> str | None:
        args = ["docker", "inspect", "--type", "container", "--format", "{{.Image}}", name]
        return self._docker(args, "no such container", "no such object")

    def _read(self, name: str) -> Installed | None:
        try:
            data = json.loads((self.project / name).read_text())
            schema = data.get("schema")
            return Installed(
                Release(str(data["digest"]), int(data["build"]), str(data.get("commit", ""))),
                int(schema) if schema is not None else None,
                int(data.get("floor", 0)),
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def installed(self) -> Installed | None:
        return self._read(STATE_FILE)

    def installed_release(self) -> Release | None:
        good = self.installed()
        return good.release if good is not None else None

    def _save(self, name: str, release: Release, schema: int | None, floor: int) -> None:
        self._write(name, json.dumps({**asdict(release), "schema": schema, "floor": floor}, indent=2) + "\n")

    def _record(self, new: Installed) -> None:
        self._save(STATE_FILE, new.release, new.schema, new.floor)
        (self.project / PENDING_FILE).unlink(missing_ok=True)

    # ----- releases held back ------------------------------------------------------------------------------

    def held(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads((self.project / REFUSED_FILE).read_text())
        except (OSError, ValueError):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _held_why(self, digest: str) -> str | None:
        """Why a release is held back from this host now, or None. An entry that cannot be read, or that ends
        further ahead than any hold lasts (the clock went back), holds nothing."""
        entry = self.held().get(digest)
        if entry is None:
            return None
        try:
            until = float(str(entry.get("until")))
        except ValueError:
            return None
        now = self.wall()
        if not now < until <= now + REFUSED_MAX_S:
            return None
        return str(entry.get("why", ""))

    def _hold(self, digest: str, why: str, refused: bool) -> None:
        """Hold a release back: for an hour when it could not be tried; when it failed here, for six hours, twice as
        long each time after (a passing problem of the host would otherwise keep it out)."""
        held = self.held()
        try:
            before = int(str(held.get(digest, {}).get("tries", 0)))
        except ValueError:
            before = 0
        tries = before + 1 if refused else before
        wait = min(REFUSED_RETRY_S * 2 ** (tries - 1), REFUSED_MAX_S) if refused else RETRY_S
        held[digest] = {"why": why, "until": self.wall() + wait, "tries": tries}
        self._write(REFUSED_FILE, json.dumps(held, indent=2) + "\n")

    def _remember(self, digest: str, why: str, for_s: float) -> None:
        self._failed[digest] = (why, self.clock() + for_s)

    # ----- files and locks ---------------------------------------------------------------------------------

    def _write(self, name: str, text: str) -> bool:
        """Write a file of the project atomically; False when it already holds that text."""
        path = self.project / name
        try:
            if path.read_text() == text:
                return False
        except OSError:
            pass
        tmp = path.with_name(f".{name}.tmp")
        with open(tmp, "w") as f:  # on disk before it replaces the old file: a power cut leaves one or the other
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp.chmod(0o644)
        os.replace(tmp, path)
        folder = os.open(self.project, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
        return True

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """One update at a time on the host (the updater, `update once`, install.sh's setup)."""
        fd = os.open(self.project / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpdateError("another update is running on this host") from None
            yield
        finally:
            os.close(fd)

    # ----- docker compose and the images -------------------------------------------------------------------

    def compose(self, *args: str, file: str = "compose.yml", timeout: float = 600) -> str:
        project = str(self.project)
        try:
            return self.run(
                ["docker", "compose", "--project-directory", project, "-f", f"{project}/{file}", *args], timeout
            )
        except UpdateError as exc:
            raise UpdateError(f"docker compose {args[0]}: {str(exc).split(': ', 1)[-1]}") from None

    def _compose_of(self, ref: str) -> str:
        """A release's own compose file, from its image."""
        return self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", ref, IMAGE_COMPOSE], 300)

    def _number(self, ref: str, code: str) -> int:
        """A number a release's code prints (its rollback floor, its schema version)."""
        out = self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", ref, "-c", code], 300)
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise UpdateError(f"{ref}: printed no number") from None

    def _probe_command(self, ref: str, digest: str) -> list[str]:
        """The new release's updater, run once as a dry run, as the updater service runs (its user, its limits, its
        mounts, the project folder read-only), checking the release it is part of."""
        project = str(self.project)
        return [
            "docker", "run", "--rm", "--network", "host", "--user", "0:0", "--read-only", "--tmpfs", "/tmp",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "-e", "HOME=/tmp", "-e", "DOCKER_CONFIG=/tmp/docker", "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{project}:{project}:ro", "-v", "/etc/kokoah-subnet:/etc/kokoah-subnet:ro",
            "--entrypoint", "kokoah-subnet", ref,
            "validator", "update", "probe", "--project-dir", project, "--digest", digest,
        ]  # fmt: skip

    def _record_schema(self) -> int | None:
        """The schema version the record is at now, read with the validator's own settings; None when it cannot be
        read (the database not up yet, say). Docker itself not answering is an error."""
        try:
            out = self.compose(
                "run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "validator", "-c", RECORD_SCHEMA_CODE,
                timeout=300,
            )  # fmt: skip
            return int(out.strip().splitlines()[-1])
        except UpdateError as exc:
            if any(sign in str(exc).lower() for sign in DOCKER_DOWN):
                raise
            return None
        except (ValueError, IndexError):
            return None

    def _past_the_floor(self, new: Installed, back: Installed) -> bool | None:
        """Whether going back from `new` to `back` would put code on a record it cannot work with: `new` marks a
        migration that `back`'s code ends before, its code ran here, and the record went through it. None when the
        record cannot be read: the choice waits, nothing is guessed. A record still below the floor is read again
        with the new validator stopped, so it cannot cross the floor between the reading and going back."""
        if new.floor <= (back.schema if back.schema is not None else 0):
            return False
        if self.container_image(VALIDATOR) != self.image_id(f"{self.image}@{new.release.digest}"):
            return False  # its code never ran here
        record = self._record_schema()
        if record is None or record >= new.floor:
            return None if record is None else True
        self.compose("stop", "validator")
        record = self._record_schema()
        if record is None:
            return None
        return record >= new.floor

    def _keep(self, new: Installed, back: Installed, why: str) -> str:
        """Stay on a release that cannot be rolled back, until a fixed release follows: its validator is started
        first and only then is the release recorded, so a failure leaves the choice to the next look."""
        self.compose("up", "-d", "--no-deps", "validator")
        self._record(new)
        self._others_pending = True
        name = f"build {new.release.build}"
        return (
            f"{name} stays although {why}: build {back.release.build} cannot work with the record it may have "
            f"changed (this release's rollback floor {new.floor}); a fixed release must follow"
        )

    # ----- one look ------------------------------------------------------------------------------------------

    def check(self) -> str:
        """One look: bring the host back to the installed release if it drifted, then install a new signed one."""
        with self._locked():
            return self._check()

    def _check(self) -> str:
        good = self.installed()
        if good is not None:
            done, settled = self.reconcile(good)
            if settled:
                return done or "up to date"
            if done:
                self.say(done)  # a repair that did not hold: a newer release may fix it, so the look goes on
        latest = self.head(self.image, CHANNEL)
        if good is not None and latest == good.release.digest:
            return "up to date"
        why = self._held_why(latest)
        if why is not None:
            return f"held back {latest[:19]}: {why}"
        failed = self._failed.get(latest)
        if failed is not None and self.clock() < failed[1]:
            return failed[0]
        try:
            new = verify(self.run, f"{self.image}@{latest}", self.image)
        except UpdateError as exc:
            self._remember(latest, f"not installed: {exc}", RETRY_S)
            raise
        floor = good.release if good is not None else None
        if floor is None:
            current = self.installed_digest()
            if current is not None:
                try:
                    floor = verify(self.run, f"{self.image}@{current}", self.image)
                except UpdateError:
                    floor = None
        if floor is not None and new.build < floor.build:
            why = (
                f"refused {new.digest[:19]} (build {new.build}, commit {new.commit[:12]}): older than the installed "
                f"build {floor.build}"
            )
            self._remember(latest, why, RETRY_S)
            return why
        return self.install(new).message

    def reconcile(self, good: Installed) -> tuple[str | None, bool]:
        """Bring the host back to the installed release: what was done (None: nothing), and whether the look ends
        there. An install cut short after its code may have moved the record past what the installed release reads
        is kept instead. A repair that fails is tried again an hour later, and the look goes on meanwhile, so a
        fixed release still installs."""
        current = self.installed_digest()
        pending = self._read(PENDING_FILE)
        if pending is not None and current == pending.release.digest != good.release.digest:
            # an install was cut short: whether its release may be left depends on the record, before anything else
            past = self._past_the_floor(pending, good)
            if past:
                return self._keep(pending, good, "its install was cut short"), True
            if past is None:
                self.compose("up", "-d", "--no-deps", "validator")
                return (
                    f"build {pending.release.build} was being installed and the record cannot be read yet: the "
                    f"choice between it and build {good.release.build} waits for a later look"
                ), True
        if current != good.release.digest or self.container_image(VALIDATOR) != self.image_id(CURRENT):
            if self._repair_failed_at is not None and self.clock() - self._repair_failed_at < RETRY_S:
                return None, False
            try:
                done, problem = self.restore(good, "the host does not run it")
            except UpdateError as exc:
                if any(sign in str(exc).lower() for sign in DOCKER_DOWN):
                    raise
                self._repair_failed_at = self.clock()
                return f"could not put back build {good.release.build}: {exc}", False
            (self.project / PENDING_FILE).unlink(missing_ok=True)
            if problem is None:
                self._repair_failed_at = None
                return done, True
            self._repair_failed_at = self.clock()
            return done, False
        if self._others_pending:
            try:
                self.start_others()
                self._others_pending = False
            except UpdateError as exc:
                self.say(f"the other services do not start: {exc}")
        updater = self.container_image(UPDATER)
        if updater is not None and updater != self.image_id(CURRENT):
            if self._hop_at is None or self.clock() - self._hop_at >= RETRY_S:
                self._hop_at = self.clock()
                self.restart_updater()
                return f"restarting the updater with build {good.release.build}", True
        return None, False

    def restore(self, good: Installed, why: str) -> tuple[str, str | None]:
        """Put the installed release back, with its own compose file: what was done, and the validator's problem if
        it does not run."""
        ref = f"{self.image}@{good.release.digest}"
        self.say(f"putting back build {good.release.build}: {why}")
        if self.image_id(ref) is None:
            self.run(["docker", "pull", "--quiet", ref], 1800)  # by digest: the content it was checked with
        self.run(["docker", "tag", ref, CURRENT], 60)
        self._write("compose.yml", self._compose_of(ref))
        problem = self.start_validator()
        try:
            self.start_others()
        except UpdateError as exc:
            self._others_pending = True
            self.say(f"the other services do not start: {exc}")
        done = f"put back build {good.release.build}"
        return (done + (f", but its validator does not run ({problem})" if problem else "")), problem

    def install(self, release: Release, restart_self: bool = True) -> Outcome:
        """Check the release here (its compose file, what it knows of the record, a dry run of its updater), switch
        to it, keep it if its validator runs, else go back (or stay, when the record went where the release before
        cannot follow)."""
        ref = f"{self.image}@{release.digest}"
        name = f"build {release.build} (commit {release.commit[:12]}, {release.digest[:19]})"
        good = self.installed()
        if good is not None and good.release.digest == release.digest:
            done, problem = self.restore(good, "installed again")
            return Outcome(problem is None, done)
        self.say(f"installing {name}")
        try:
            self.run(["docker", "pull", "--quiet", ref], 1800)
        except UpdateError as exc:
            if not any(sign in str(exc).lower() for sign in DOCKER_DOWN):
                self._remember(release.digest, f"not downloaded: {exc}", PULL_RETRY_S)
            raise
        self._write(NEW_COMPOSE, self._compose_of(ref))
        try:
            self.compose("config", "--quiet", file=NEW_COMPOSE)
            new = Installed(release, self._number(ref, SCHEMA_CODE), self._number(ref, ROLLBACK_FLOOR_CODE))
        except UpdateError as exc:
            self._hold(release.digest, f"it could not be checked here: {exc}", refused=False)
            raise UpdateError(f"{name}: not checked here, nothing changed: {exc}") from None
        try:
            self.run(self._probe_command(ref, release.digest), 300)
        except UpdateError as exc:
            if any(sign in str(exc).lower() for sign in DOCKER_DOWN):
                raise
            # held an hour, not refused: a registry or signature service hiccup fails it as surely as a broken one
            self._hold(release.digest, f"its updater's dry run failed here ({exc})", refused=False)
            return Outcome(False, f"{name}: its updater's dry run failed here, nothing changed: {exc}")
        if good is not None and self.image_id(f"{self.image}@{good.release.digest}") is not None:
            self.run(["docker", "tag", f"{self.image}@{good.release.digest}", PREVIOUS], 60)
        self._save(PENDING_FILE, release, new.schema, new.floor)
        self.run(["docker", "tag", ref, CURRENT], 60)
        os.replace(self.project / NEW_COMPOSE, self.project / "compose.yml")
        problem = self.start_validator()
        if problem is not None:
            return self._go_back(new, name, problem, good)
        self._record(new)  # from here on this release is the host's, whatever else fails
        try:
            self.start_others()
        except UpdateError as exc:
            self._others_pending = True
            self.say(f"the other services do not start yet ({exc}); the next looks start them")
        self.prune()
        if restart_self:
            try:
                self.restart_updater()
            except UpdateError as exc:
                self.say(f"the updater did not restart ({exc}); the next looks try again")
        return Outcome(True, f"installed {name}")

    def _go_back(self, new: Installed, name: str, problem: str, good: Installed | None) -> Outcome:
        """The new release's validator does not run: go back to the installed release, unless the record went
        through a step that release's code cannot follow; refuse the new one only if the old one then runs."""
        release = new.release
        if good is None:
            (self.project / PENDING_FILE).unlink(missing_ok=True)
            self._hold(release.digest, f"its validator does not run here ({problem})", refused=False)
            raise UpdateError(f"{name}: the validator does not run ({problem}), and there is no release to go back to")
        past = self._past_the_floor(new, good)
        if past:
            return Outcome(False, self._keep(new, good, f"its validator does not run ({problem})"))
        if past is None:
            raise UpdateError(
                f"{name}: the validator does not run ({problem}) and the record cannot be read: the choice between "
                f"it and build {good.release.build} waits for a later look"
            )
        self.say(f"{name}: the validator does not run ({problem}); going back to build {good.release.build}")
        # held while going back, so that a way back that fails does not bring this release in again at once
        self._hold(release.digest, f"going back from it ({problem})", refused=False)
        done, again = self.restore(good, f"build {release.build} does not run")
        (self.project / PENDING_FILE).unlink(missing_ok=True)
        if again is not None:
            self._hold(
                release.digest, f"neither it nor build {good.release.build} ran: the host is the problem", refused=False
            )
            return Outcome(False, f"{done}: neither release runs, so the host is the problem; build {release.build} "
                                  "is tried again in an hour")  # fmt: skip
        self._hold(release.digest, f"this host went back from it ({problem})", refused=True)
        return Outcome(False, f"went back to build {good.release.build}: build {release.build} {problem}")

    def start_validator(self) -> str | None:
        """Start the validator of `current`: None once its record is at the release's schema version (the health
        check) and it then runs `STEADY_S` without a restart, else what went wrong with it. A restart or a stop
        counts at once, without waiting out the time a migration may take. Docker itself not answering is an
        error, never a verdict on the release."""
        try:
            self.compose("up", "-d", "--no-deps", "validator")
        except UpdateError as exc:
            if any(sign in str(exc).lower() for sign in DOCKER_DOWN):
                raise
            return f"not started: {exc}"
        running, base, health = self._validator_state()
        started = self.clock()
        while True:
            if not running:
                return "stopped before it was ready"
            if health == "unhealthy":
                return "unhealthy before it was ready"
            if health == "healthy":
                break
            if self.clock() - started > VALIDATOR_WAIT_S:
                return f"not ready in {VALIDATOR_WAIT_S // 3600} hours"
            self.sleep(POLL_S)
            running, restarts, health = self._validator_state()
            if restarts > base:
                return "restarted before it was ready"
        self.sleep(STEADY_S)
        running, restarts, health = self._validator_state()
        if not running or restarts > base or health == "unhealthy":
            return "stopped or restarted after it started"
        return None

    def _validator_state(self) -> tuple[bool, int, str]:
        """Whether the validator's container runs, its restart count and its health ("healthy" without a health
        check); asked three times before Docker's silence is an error (never a verdict on the release)."""
        problem: Exception | None = None
        for attempt in range(3):
            if attempt:
                self.sleep(5)
            try:
                running, restarts, *health = self.run(
                    ["docker", "inspect", "--type", "container", "--format", VALIDATOR_STATE, VALIDATOR], 60
                ).split()
                return running == "true", int(restarts), health[0] if health else "healthy"
            except (UpdateError, ValueError) as exc:
                problem = exc
        raise UpdateError(f"docker inspect {VALIDATOR}: {problem}")

    def start_others(self) -> None:
        """Everything but the validator and the updater (the interface reads only a migrated record)."""
        others = [s for s in self.compose("config", "--services").split() if s not in ("validator", "updater")]
        if others:
            self.compose("up", "-d", "--no-deps", "--remove-orphans", *others)

    def restart_updater(self) -> None:
        """From a helper container, which outlives this one: `hop` recreates the updater with the new release."""
        try:
            self.run(["docker", "rm", "-f", HOP], 60)
        except UpdateError:
            pass
        self.compose(
            "run", "-d", "--no-deps", "--name", HOP, "updater",
            "validator", "update", "hop", "--project-dir", str(self.project),
        )  # fmt: skip

    def prune(self) -> None:
        """Remove this image's older releases, keeping `current` and `previous` (Docker keeps what a container
        uses)."""
        try:
            keep = {self.image_id(tag) for tag in (CURRENT, PREVIOUS)} - {None}
            rows = self.run(["docker", "image", "ls", "--digests", "--no-trunc", "--format", "{{json .}}"], 60)
        except UpdateError:
            return  # tidying up is never worth failing an update for
        for line in rows.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            repo, digest, image_id = str(row.get("Repository", "")), str(row.get("Digest", "")), str(row.get("ID", ""))
            if _same_repo(repo, self.image) and DIGEST.fullmatch(digest) and image_id not in keep:
                try:
                    self.run(["docker", "image", "rm", f"{repo}@{digest}"], 120)
                except UpdateError:
                    pass  # in use, or already gone

    def hop(self) -> None:
        """Run from the helper container: recreate the updater with the new release (the old one is idle)."""
        self.sleep(2)
        self.compose("up", "-d", "--no-deps", "updater")

    def probe(self, digest: str | None = None) -> str:
        """A dry run of this release's updater on the host, before the host switches to it: it reads the host's
        files and Docker, checks the release's compose file with its own compose, reads the registry and checks the
        release's signature (`digest`, else the published one's); it changes nothing."""
        good = self.installed()
        self.held()
        self.installed_digest()
        if (self.project / NEW_COMPOSE).exists():
            self.compose("config", "--quiet", file=NEW_COMPOSE)
        latest = self.head(self.image, CHANNEL)
        release = verify(self.run, f"{self.image}@{digest or latest}", self.image)
        return f"the updater runs here (installed: {good.release.build if good else 'none'}, checked: {release.build})"

    def setup(self, digest: str, allow_older: bool = False) -> str:
        """First start (`deploy/validator/install.sh`), run from the image it installs. The updater is started
        whatever happens; an install that does not hold is an error. Going back on purpose (`allow_older`) never
        goes below what the record has been through."""
        try:
            with self._locked():
                release = verify(self.run, f"{self.image}@{digest}", self.image)
                good = self.installed()
                if good is not None and release.build < good.release.build:
                    if not allow_older:
                        raise UpdateError(
                            f"build {release.build} is older than the installed build {good.release.build}; "
                            "run install.sh --allow-older to go back on purpose"
                        )
                    ref = f"{self.image}@{release.digest}"
                    self.run(["docker", "pull", "--quiet", ref], 1800)
                    older = Installed(release, self._number(ref, SCHEMA_CODE), self._number(ref, ROLLBACK_FLOOR_CODE))
                    if good.floor > (older.schema or 0):
                        record = self._record_schema()
                        if record is None or record >= good.floor:
                            raise UpdateError(
                                f"build {release.build} cannot work with the record (schema "
                                f"{record if record is not None else 'unknown'}, past the floor {good.floor} of build "
                                f"{good.release.build}): not installed"
                            )
                outcome = self.install(release, restart_self=False)
        finally:
            if (self.project / "compose.yml").exists():
                try:
                    self.compose("up", "-d", "--no-deps", "updater")
                except UpdateError as exc:
                    self.say(f"the updater did not start: {exc}")
        if not outcome.ok:
            raise UpdateError(outcome.message)
        return outcome.message

    def watch(self, interval: float = INTERVAL_S, rounds: int | None = None) -> None:
        """Look every `interval` seconds (for `rounds` looks, or for ever); says each outcome once, not every look."""
        commit = os.environ.get("KOKOAH_SUBNET_COMMIT", "")[:12] or "unknown"
        installed = self.installed_release()
        where = f"build {installed.build} ({installed.digest[:19]})" if installed else "no recorded release"
        self.say(f"updater of commit {commit}, {where}: checking {self.image}:{CHANNEL} every {interval:.0f} s")
        last = "up to date"
        while rounds is None or rounds > 0:
            try:
                result = self.check()
            except Exception as exc:  # a failed look is retried at the next one
                result = f"update check failed: {exc}"
            if result != last:
                self.say(result)
                last = result
            if rounds is not None:
                rounds -= 1
                if rounds == 0:
                    break
            self.sleep(interval)
