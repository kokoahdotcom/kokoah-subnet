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
STATE_FILE = "release.json"  # the last release installed and running well: digest, build, commit
REFUSED_FILE = "refused.json"  # releases this host went back from: digest -> why and when
LOCK_FILE = ".update.lock"  # one update at a time on a host
NEW_COMPOSE = ".compose.new.yml"  # a release's compose file while it is checked
VALIDATOR = "kokoah-subnet-validator"  # the containers' names (compose.yml)
UPDATER = "kokoah-subnet-updater"
VALIDATOR_STATE = "{{.State.Running}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{end}}"
HOP = "kokoah-subnet-updater-hop"
INTERVAL_S = 300
VALIDATOR_WAIT_S = 7200  # for the record's migration
STEADY_S = 120  # a new validator must then run this long without a restart
RETRY_S = 3600  # a release that failed its check here is looked at again after this long
REFUSED_RETRY_S = 6 * 3600  # and one this host went back from, after this long (a passing problem of the host)
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


class Updater:
    """One validator host's Docker setup in `project`: its compose file and `.env`, the release that last installed
    and ran well (`release.json`, the truth every look brings the host back to) and the releases it went back from
    (`refused.json`)."""

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
        self._failed: dict[str, tuple[str, float]] = {}  # releases that failed their check here: why, when

    # ----- what is on the host -----------------------------------------------------------------------------

    def _docker(self, args: list[str], missing: str) -> str | None:
        """A Docker read: None when Docker says the object does not exist, an error for anything else."""
        try:
            return self.run(args, 60).strip()
        except UpdateError as exc:
            if missing in str(exc).lower():
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
        return self._docker(["docker", "inspect", "--format", "{{.Image}}", name], "no such object")

    def installed_release(self) -> Release | None:
        try:
            data = json.loads((self.project / STATE_FILE).read_text())
            return Release(str(data["digest"]), int(data["build"]), str(data.get("commit", "")))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def refused(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads((self.project / REFUSED_FILE).read_text())
        except (OSError, ValueError):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _refuse(self, digest: str, why: str) -> None:
        refused = self.refused()
        refused[digest] = {"why": why, "at": self.wall()}
        self._write(REFUSED_FILE, json.dumps(refused, indent=2) + "\n")

    def _remember(self, digest: str, why: str) -> None:
        self._failed[digest] = (why, self.clock())

    def _write(self, name: str, text: str) -> bool:
        """Write a file of the project atomically; False when it already holds that text."""
        path = self.project / name
        try:
            if path.read_text() == text:
                return False
        except OSError:
            pass
        tmp = path.with_name(f".{name}.tmp")
        tmp.write_text(text)
        tmp.chmod(0o644)
        os.replace(tmp, path)
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

    # ----- docker compose ----------------------------------------------------------------------------------

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

    def _python(self, ref: str, code: str) -> str:
        return self.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", ref, "-c", code], 300)

    # ----- one look ------------------------------------------------------------------------------------------

    def check(self) -> str:
        """One look: bring the host back to the installed release if it drifted, else install a new signed one."""
        with self._locked():
            return self._check()

    def _check(self) -> str:
        good = self.installed_release()
        if good is not None:
            repaired = self.reconcile(good)
            if repaired is not None:
                return repaired
        latest = self.head(self.image, CHANNEL)
        if good is not None and latest == good.digest:
            return "up to date"
        refusal = self.refused().get(latest)
        if refusal is not None and self.wall() - float(str(refusal.get("at", 0))) < REFUSED_RETRY_S:
            return f"refused {latest[:19]}: this host went back from it ({refusal.get('why', '')})"
        failed = self._failed.get(latest)
        if failed is not None and self.clock() - failed[1] < RETRY_S:
            return failed[0]
        try:
            new = verify(self.run, f"{self.image}@{latest}", self.image)
        except UpdateError as exc:
            self._remember(latest, f"not installed: {exc}")
            raise
        floor = good
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
            self._remember(latest, why)
            return why
        return self.install(new)

    def reconcile(self, good: Release) -> str | None:
        """None when the host runs the installed release; else what was done to bring it back (an install or a
        rollback that stopped halfway, a tag changed by hand, an updater that did not restart)."""
        if self.installed_digest() != good.digest:
            return self.restore(good, "the current tag names another image")[0]
        want = self.image_id(CURRENT)
        if self.container_image(VALIDATOR) != want:
            return self.restore(good, "the validator runs another image")[0]
        updater = self.container_image(UPDATER)
        if updater is not None and updater != want:
            self.restart_updater()
            return f"restarting the updater with build {good.build}"
        return None

    def restore(self, good: Release, why: str) -> tuple[str, str | None]:
        """Put the installed release back, with its own compose file: what was done, and the validator's problem if
        it does not run."""
        ref = f"{self.image}@{good.digest}"
        self.say(f"putting back build {good.build}: {why}")
        if self.image_id(ref) is None:
            self.run(["docker", "pull", "--quiet", ref], 1800)  # by digest: the content it was checked with
        self.run(["docker", "tag", ref, CURRENT], 60)
        self._write("compose.yml", self._compose_of(ref))
        problem = self.start_validator()
        self.start_others()
        done = f"put back build {good.build}"
        return (done + (f", but its validator does not run ({problem})" if problem else "")), problem

    def install(self, release: Release, restart_self: bool = True) -> str:
        """Check the release's compose file here, switch to it, and go back if its validator does not run."""
        ref = f"{self.image}@{release.digest}"
        name = f"build {release.build} (commit {release.commit[:12]}, {release.digest[:19]})"
        self.say(f"installing {name}")
        self.run(["docker", "pull", "--quiet", ref], 1800)
        self._write(NEW_COMPOSE, self._compose_of(ref))
        try:
            self.compose("config", "--quiet", file=NEW_COMPOSE)
        except UpdateError as exc:
            self._remember(release.digest, f"not installed: its compose file does not work here: {exc}")
            raise UpdateError(f"{name}: its compose file does not work here, nothing changed: {exc}") from None
        good = self.installed_release()
        if good is not None and self.image_id(f"{self.image}@{good.digest}") is not None:
            self.run(["docker", "tag", f"{self.image}@{good.digest}", PREVIOUS], 60)
        self.run(["docker", "tag", ref, CURRENT], 60)
        os.replace(self.project / NEW_COMPOSE, self.project / "compose.yml")
        problem = self.start_validator()
        if problem is not None:
            return self._go_back(release, name, problem, good)
        self.start_others()
        self._write(STATE_FILE, json.dumps(asdict(release), indent=2) + "\n")
        self.prune()
        if restart_self:
            self.restart_updater()
        return f"installed {name}"

    def _go_back(self, release: Release, name: str, problem: str, good: Release | None) -> str:
        """The new release's validator does not run: go back to the installed release, if its code can work with the
        record as the new release may have left it, and refuse the new one only if the old one then runs."""
        if good is None:
            self._remember(release.digest, f"not installed: its validator does not run ({problem})")
            raise UpdateError(f"{name}: the validator does not run ({problem}), and there is no release to go back to")
        back = f"{self.image}@{good.digest}"
        try:
            floor = int(self._python(f"{self.image}@{release.digest}", ROLLBACK_FLOOR_CODE).strip() or 0)
            schema = int(self._python(back, SCHEMA_CODE).strip())
        except (UpdateError, ValueError):
            floor, schema = 0, 0
        if schema < floor:
            # the record may have gone where the old code cannot follow: this release stays, recorded as installed
            # so that no look puts the old one back, until a fixed release comes
            self.start_others()
            self._write(STATE_FILE, json.dumps(asdict(release), indent=2) + "\n")
            return (
                f"{name} stays although its validator does not run ({problem}): build {good.build} cannot work with "
                f"a record this release may have changed (schema {floor}); a fixed release must follow"
            )
        self.say(f"{name}: the validator does not run ({problem}); going back to build {good.build}")
        done, again = self.restore(good, f"build {release.build} does not run")
        if again is not None:
            self._remember(release.digest, f"{done}; build {release.build} is tried again in an hour")
            return f"{done}: neither release runs, so the host is the problem; build {release.build} is tried again"
        self._refuse(release.digest, problem)
        return f"went back to build {good.build}: build {release.build} {problem}"

    def start_validator(self) -> str | None:
        """Start the validator of `current`: None once its record is at the release's schema version and it then
        runs `STEADY_S` without a restart, else what went wrong with it. Docker itself not answering is an error."""
        try:
            self.compose(
                "up", "-d", "--no-deps", "--wait", "--wait-timeout", str(VALIDATOR_WAIT_S), "validator",
                timeout=VALIDATOR_WAIT_S + 300,
            )  # fmt: skip
        except UpdateError as exc:
            if any(sign in str(exc).lower() for sign in DOCKER_DOWN):
                raise
            return f"not ready: {exc}"
        start = self._validator_state()
        self.sleep(STEADY_S)
        end = self._validator_state()
        if not end[0] or end[2] == "unhealthy" or end[1] > start[1]:
            return "stopped or restarted after it started"
        return None

    def _validator_state(self) -> tuple[bool, int, str]:
        """Whether the validator's container runs, its restart count and its health; asked three times before
        Docker's silence is an error (never a verdict on the release)."""
        problem: Exception | None = None
        for attempt in range(3):
            if attempt:
                self.sleep(5)
            try:
                running, restarts, *health = self.run(
                    ["docker", "inspect", "--format", VALIDATOR_STATE, VALIDATOR], 60
                ).split()
                return running == "true", int(restarts), health[0] if health else ""
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
            "run", "-d", "--rm", "--no-deps", "--name", HOP, "updater",
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

    def setup(self, digest: str, allow_older: bool = False) -> str:
        """First start (`deploy/validator/install.sh`), run from the image it installs. The updater is started
        again whatever happens (install.sh stopped it); an install that does not hold is an error."""
        try:
            with self._locked():
                release = verify(self.run, f"{self.image}@{digest}", self.image)
                good = self.installed_release()
                if good is not None and release.build < good.build and not allow_older:
                    raise UpdateError(
                        f"build {release.build} is older than the installed build {good.build}; "
                        "run install.sh --allow-older to go back on purpose"
                    )
                result = self.install(release, restart_self=False)
        finally:
            if (self.project / "compose.yml").exists():
                try:
                    self.compose("up", "-d", "--no-deps", "updater")
                except UpdateError as exc:
                    self.say(f"the updater did not start: {exc}")
        if self.installed_release() != release:
            raise UpdateError(result)
        return result

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


ROLLBACK_FLOOR_CODE = "from kokoah_subnet.validator import migrations as m; print(getattr(m, 'ROLLBACK_FLOOR', 0))"
SCHEMA_CODE = "from kokoah_subnet.validator.migrations import latest_version; print(latest_version())"
