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

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
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
STATE_FILE = "release.json"  # the installed release: digest, build, commit
HOP = "kokoah-subnet-updater-hop"
INTERVAL_S = 300
VALIDATOR_WAIT_S = 1800
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
    """One validator host's Docker setup in `project` (its compose file, `.env` and installed-release file)."""

    def __init__(
        self,
        project: Path = PROJECT_DIR,
        run: Runner = run_command,
        head: Callable[[str, str], str] = remote_digest,
        image: str = IMAGE,
        say: Callable[[str], None] = log,
    ) -> None:
        self.project = project
        self.run = run
        self.head = head
        self.image = image
        self.say = say

    # ----- state -------------------------------------------------------------------------------------------

    def installed_digest(self) -> str | None:
        """The registry digest of `kokoah-subnet:current`, or None (not installed, or a local build)."""
        try:
            out = self.run(["docker", "image", "inspect", CURRENT, "--format", "{{json .RepoDigests}}"], 60)
            names = json.loads(out)
        except (UpdateError, ValueError):
            return None
        for name in names if isinstance(names, list) else []:
            repo, _, digest = str(name).partition("@")
            if _same_repo(repo, self.image) and DIGEST.fullmatch(digest):
                return digest
        return None

    def installed_release(self) -> Release | None:
        try:
            data = json.loads((self.project / STATE_FILE).read_text())
            return Release(str(data["digest"]), int(data["build"]), str(data.get("commit", "")))
        except (OSError, ValueError, KeyError, TypeError):
            return None

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

    # ----- docker compose ----------------------------------------------------------------------------------

    def compose(self, *args: str, timeout: float = 600) -> str:
        project = str(self.project)
        return self.run(
            ["docker", "compose", "--project-directory", project, "-f", f"{project}/compose.yml", *args], timeout
        )

    # ----- the steps -----------------------------------------------------------------------------------------

    def check(self) -> str:
        """One look at the published tag; installs a new signed release. Returns what happened."""
        current = self.installed_digest()
        if current is not None and self.head(self.image, CHANNEL) == current:
            return "up to date"
        new = verify(self.run, f"{self.image}:{CHANNEL}", self.image)
        if new.digest == current:
            return "up to date"
        floor = self.installed_release()
        if floor is None and current is not None:
            try:
                floor = verify(self.run, f"{self.image}@{current}", self.image)
            except UpdateError:
                floor = None
        if floor is not None and new.build < floor.build:
            return (
                f"refused {new.digest[:19]} (build {new.build}, commit {new.commit[:12]}): older than the installed "
                f"build {floor.build}"
            )
        self.install(new)
        return f"installed build {new.build} (commit {new.commit[:12]}, {new.digest[:19]})"

    def install(self, release: Release, restart_self: bool = True) -> None:
        ref = f"{self.image}@{release.digest}"
        self.say(f"installing build {release.build} (commit {release.commit[:12]})")
        self.run(["docker", "pull", "--quiet", ref], 1800)
        try:
            self.run(["docker", "tag", CURRENT, PREVIOUS], 60)
        except UpdateError:
            pass  # nothing installed before
        self.run(["docker", "tag", ref, CURRENT], 60)
        compose = self.run(
            ["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", CURRENT, IMAGE_COMPOSE], 300
        )
        if self._write("compose.yml", compose):
            self.say("the release brings a new compose file")
        self._write(STATE_FILE, json.dumps(asdict(release), indent=2) + "\n")
        self.restart(restart_self)

    def restart(self, restart_self: bool = True) -> None:
        """The validator first (it migrates the record), then the rest, then the updater from a helper container."""
        try:
            self.compose(
                "up", "-d", "--no-deps", "--wait", "--wait-timeout", str(VALIDATOR_WAIT_S), "validator",
                timeout=VALIDATOR_WAIT_S + 300,
            )  # fmt: skip
            self.say("the validator runs the new release")
        except UpdateError as exc:
            self.say(f"the validator is not ready yet ({exc}); starting the other services anyway")
        others = [s for s in self.compose("config", "--services").split() if s not in ("validator", "updater")]
        if others:
            self.compose("up", "-d", "--no-deps", "--remove-orphans", *others)
        if not restart_self:
            self.compose("up", "-d", "--no-deps", "updater")
            return
        try:
            self.run(["docker", "rm", "-f", HOP], 60)
        except UpdateError:
            pass
        self.compose(
            "run", "-d", "--rm", "--no-deps", "--name", HOP, "updater",
            "validator", "update", "hop", "--project-dir", str(self.project),
        )  # fmt: skip

    def hop(self) -> None:
        """Run from the helper container: recreate the updater with the new release (the old one is idle)."""
        time.sleep(2)
        self.compose("up", "-d", "--no-deps", "updater")

    def setup(self, digest: str) -> None:
        """First start (`deploy/validator/install.sh`), run from the image it installs."""
        release = verify(self.run, f"{self.image}@{digest}", self.image)
        self.install(release, restart_self=False)

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
            time.sleep(interval)
