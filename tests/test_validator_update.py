"""The validator's updater (src/kokoah_subnet/validator/update.py) against a stand-in Docker and cosign."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from kokoah_subnet.cli import main
from kokoah_subnet.validator import update as u
from kokoah_subnet.validator.store import Store

ROOT = Path(__file__).resolve().parent.parent
OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
OLDER = "sha256:" + "c" * 64


def _signature(digest: str, build: int, repo: str = "index.docker.io/kokoahdotcom/kokoah-subnet") -> dict[str, object]:
    return {
        "critical": {
            "identity": {"docker-reference": repo},
            "image": {"docker-manifest-digest": digest},
            "type": "cosign container image signature",
        },
        "optional": {"build": str(build), "commit": f"{build:040d}", "Subject": u.WORKFLOW_IDENTITY},
    }


class Host:
    """Docker and cosign as the updater sees them: a published tag, signatures, the installed image."""

    def __init__(self, latest: str, signed: dict[str, int], installed: str | None) -> None:
        self.latest = latest
        self.signed = signed  # digest -> build of the release workflow's signature
        self.installed = installed
        self.calls: list[list[str]] = []
        self.foreign: set[str] = set()  # digests whose signature names another repository
        self.validator_fails = False
        self.services = "validator\ninterface\ncheck\nupdater\n"

    def head(self, image: str, tag: str) -> str:
        assert (image, tag) == (u.IMAGE, u.CHANNEL)
        return self.latest

    def run(self, args: list[str], timeout: float) -> str:
        self.calls.append(args)
        if args[:2] == ["cosign", "verify"]:
            assert args[3:] == [
                "--certificate-identity", u.WORKFLOW_IDENTITY, "--certificate-oidc-issuer", u.OIDC_ISSUER
            ]  # fmt: skip
            ref = args[2]
            digest = self.latest if ref == f"{u.IMAGE}:{u.CHANNEL}" else ref.rsplit("@", 1)[1]
            if digest not in self.signed:
                raise u.UpdateError("no matching signatures")
            repo = (
                "index.docker.io/someone/else"
                if digest in self.foreign
                else "index.docker.io/kokoahdotcom/kokoah-subnet"
            )
            return json.dumps([_signature(digest, self.signed[digest], repo)])
        if args[:3] == ["docker", "image", "inspect"]:
            if self.installed is None:
                raise u.UpdateError("no such image")
            return json.dumps([f"kokoahdotcom/kokoah-subnet@{self.installed}"])
        if args[:2] == ["docker", "tag"]:
            if args[2] == u.CURRENT and self.installed is None:
                raise u.UpdateError("no such image")
            if args[3] == u.CURRENT:
                self.installed = args[2].rsplit("@", 1)[1]
            return ""
        if args[:2] == ["docker", "run"]:
            return f"# compose file of {self.installed}\n"
        if args[:2] == ["docker", "compose"]:
            rest = args[6:]
            if rest[:1] == ["config"]:
                return self.services
            if "--wait" in rest and self.validator_fails:
                raise u.UpdateError("container kokoah-subnet-validator is unhealthy")
        return ""

    def commands(self) -> list[str]:
        """The calls in short: compose calls without their project options."""
        out = []
        for c in self.calls:
            out.append(" ".join(["docker", "compose", *c[6:]] if c[:2] == ["docker", "compose"] else c))
        return out


def _updater(tmp_path: Path, host: Host) -> tuple[u.Updater, list[str]]:
    said: list[str] = []
    return u.Updater(tmp_path, run=host.run, head=host.head, say=said.append), said


def test_nothing_happens_while_the_tag_names_the_installed_image(tmp_path: Path) -> None:
    host = Host(latest=OLD, signed={OLD: 4}, installed=OLD)
    updater, _ = _updater(tmp_path, host)
    assert updater.check() == "up to date"
    assert host.commands() == [f"docker image inspect {u.CURRENT} --format {{{{json .RepoDigests}}}}"]


def test_a_new_signed_release_is_installed_validator_first_then_the_rest_then_the_updater(tmp_path: Path) -> None:
    host = Host(latest=NEW, signed={OLD: 4, NEW: 5}, installed=OLD)
    updater, _ = _updater(tmp_path, host)
    assert updater.check() == f"installed build 5 (commit {f'{5:040d}'[:12]}, {NEW[:19]})"
    steps = host.commands()
    ref = f"{u.IMAGE}@{NEW}"
    first = steps.index(f"docker pull --quiet {ref}")
    assert steps[first:] == [
        f"docker pull --quiet {ref}",
        f"docker tag {u.CURRENT} {u.PREVIOUS}",
        f"docker tag {ref} {u.CURRENT}",
        f"docker run --rm --network none --entrypoint cat {u.CURRENT} {u.IMAGE_COMPOSE}",
        f"docker compose up -d --no-deps --wait --wait-timeout {u.VALIDATOR_WAIT_S} validator",
        "docker compose config --services",
        "docker compose up -d --no-deps --remove-orphans interface check",
        f"docker rm -f {u.HOP}",
        f"docker compose run -d --rm --no-deps --name {u.HOP} updater validator update hop --project-dir {tmp_path}",
    ]
    assert (tmp_path / "compose.yml").read_text() == f"# compose file of {NEW}\n"
    assert json.loads((tmp_path / u.STATE_FILE).read_text()) == {"digest": NEW, "build": 5, "commit": f"{5:040d}"}
    # the next look finds it installed
    host.calls.clear()
    assert updater.check() == "up to date"


def test_an_unsigned_image_on_the_tag_is_never_pulled(tmp_path: Path) -> None:
    host = Host(latest=NEW, signed={OLD: 4}, installed=OLD)
    updater, _ = _updater(tmp_path, host)
    with pytest.raises(u.UpdateError, match="no matching signatures"):
        updater.check()
    assert not any(c[:2] == ["docker", "pull"] or c[:2] == ["docker", "tag"] for c in host.calls)
    assert host.installed == OLD


def test_a_signature_made_for_another_repository_is_refused(tmp_path: Path) -> None:
    host = Host(latest=NEW, signed={OLD: 4, NEW: 5}, installed=OLD)
    host.foreign.add(NEW)
    updater, _ = _updater(tmp_path, host)
    with pytest.raises(u.UpdateError, match="no signature of the release workflow"):
        updater.check()
    assert host.installed == OLD


def test_an_older_signed_release_put_back_on_the_tag_is_refused(tmp_path: Path) -> None:
    host = Host(latest=OLDER, signed={OLD: 4, OLDER: 3}, installed=OLD)
    updater, _ = _updater(tmp_path, host)
    assert updater.check().startswith(f"refused {OLDER[:19]} (build 3")
    assert host.installed == OLD and not any(c[:2] == ["docker", "pull"] for c in host.calls)


def test_the_installed_release_file_is_the_floor_even_when_the_old_signature_is_gone(tmp_path: Path) -> None:
    host = Host(latest=OLDER, signed={OLDER: 3}, installed=OLD)  # the installed image's signature was deleted
    (tmp_path / u.STATE_FILE).write_text(json.dumps({"digest": OLD, "build": 4, "commit": ""}))
    updater, _ = _updater(tmp_path, host)
    assert updater.check().startswith("refused")
    assert host.installed == OLD


def test_a_validator_that_is_not_ready_in_time_still_gets_the_rest_started(tmp_path: Path) -> None:
    host = Host(latest=NEW, signed={OLD: 4, NEW: 5}, installed=OLD)
    host.validator_fails = True
    updater, said = _updater(tmp_path, host)
    updater.check()
    assert any("not ready yet" in s for s in said)
    assert "docker compose up -d --no-deps --remove-orphans interface check" in host.commands()
    assert host.commands()[-1].startswith(f"docker compose run -d --rm --no-deps --name {u.HOP} updater")


def test_without_the_interface_profile_only_the_check_follows_the_validator(tmp_path: Path) -> None:
    host = Host(latest=NEW, signed={NEW: 5}, installed=OLD)
    host.services = "validator\ncheck\nupdater\n"
    updater, _ = _updater(tmp_path, host)
    updater.check()
    assert "docker compose up -d --no-deps --remove-orphans check" in host.commands()


def test_the_first_start_verifies_its_own_image_and_starts_the_updater_directly(tmp_path: Path) -> None:
    host = Host(latest=NEW, signed={NEW: 5}, installed=None)
    updater, _ = _updater(tmp_path, host)
    updater.setup(NEW)
    steps = host.commands()
    assert steps[0].startswith(f"cosign verify {u.IMAGE}@{NEW} ")
    assert f"docker tag {u.CURRENT} {u.PREVIOUS}" in steps  # tried; nothing was installed before
    assert steps[-1] == "docker compose up -d --no-deps updater"
    assert not any(" run -d " in s for s in steps)
    assert host.installed == NEW


def test_the_first_start_refuses_a_digest_without_the_workflow_signature(tmp_path: Path) -> None:
    host = Host(latest=NEW, signed={}, installed=None)
    updater, _ = _updater(tmp_path, host)
    with pytest.raises(u.UpdateError):
        updater.setup(NEW)
    assert host.installed is None


def test_a_local_build_tagged_current_is_replaced_by_the_signed_release(tmp_path: Path) -> None:
    host = Host(latest=NEW, signed={NEW: 5}, installed=None)
    updater, _ = _updater(tmp_path, host)
    assert updater.check().startswith("installed build 5")


def test_cosign_output_for_a_digest_must_be_that_digest() -> None:
    def run(args: list[str], timeout: float) -> str:
        return json.dumps([_signature(OLD, 4)])

    with pytest.raises(u.UpdateError, match="another image"):
        u.verify(run, f"{u.IMAGE}@{NEW}")
    assert u.verify(run, f"{u.IMAGE}@{OLD}") == u.Release(OLD, 4, f"{4:040d}")


def test_a_signature_without_a_build_number_does_not_count() -> None:
    def run(args: list[str], timeout: float) -> str:
        item = _signature(OLD, 4)
        item["optional"] = {}
        return json.dumps([item])

    with pytest.raises(u.UpdateError):
        u.verify(run, f"{u.IMAGE}:latest")


def test_the_tag_is_read_with_a_head_request_and_an_anonymous_token() -> None:
    seen: list[tuple[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.url.host == "auth.docker.io":
            assert request.url.params["scope"] == "repository:kokoahdotcom/kokoah-subnet:pull"
            return httpx.Response(200, json={"token": "anon"})
        if request.headers.get("authorization") != "Bearer anon":
            challenge = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",'
            challenge += 'scope="repository:kokoahdotcom/kokoah-subnet:pull"'
            return httpx.Response(401, headers={"www-authenticate": challenge})
        return httpx.Response(200, headers={"docker-content-digest": NEW})

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        assert u.remote_digest(u.IMAGE, "latest", client) == NEW
    assert [m for m, _ in seen] == ["HEAD", "GET", "HEAD"]
    assert seen[0][1] == "https://registry-1.docker.io/v2/kokoahdotcom/kokoah-subnet/manifests/latest"


def test_repository_names_compare_as_docker_and_cosign_print_them() -> None:
    for name in ("kokoahdotcom/kokoah-subnet", "docker.io/kokoahdotcom/kokoah-subnet",
                 "index.docker.io/kokoahdotcom/kokoah-subnet"):  # fmt: skip
        assert u._same_repo(name, u.IMAGE)
    for name in ("kokoahdotcom/kokoah-subnet-agent", "ghcr.io/kokoahdotcom/kokoah-subnet", "evil/kokoah-subnet"):
        assert not u._same_repo(name, u.IMAGE)


def test_ready_says_whether_the_record_is_migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOKOAH_SUBNET_DB_URL", raising=False)
    monkeypatch.delenv("KOKOAH_SUBNET_DB_READER_URL", raising=False)
    assert main(["validator", "ready", "--data-dir", str(tmp_path)]) == 1  # no record yet
    Store(tmp_path / "validator.sqlite").close()
    assert main(["validator", "ready", "--data-dir", str(tmp_path)]) == 0


def test_install_script_and_image_pin_what_the_updater_trusts() -> None:
    install = (ROOT / "deploy/validator/install.sh").read_text()
    dockerfile = (ROOT / "deploy/validator/Dockerfile").read_text()
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert f"image={u.IMAGE}\n" in install
    assert f"identity={u.WORKFLOW_IDENTITY}\n" in install and f"issuer={u.OIDC_ISSUER}\n" in install
    assert f"IMAGE: {u.IMAGE}\n" in workflow
    assert u.WORKFLOW_IDENTITY.endswith(".github/workflows/release.yml@refs/heads/main")
    cosign = re.search(r"^cosign=(\S+)$", install, re.M)
    assert cosign and f"COPY --from={cosign.group(1)} " in dockerfile  # the same cosign verifies on both sides
    assert "cosign-release: " + cosign.group(1).split(":")[1].split("@")[0] in workflow
    assert f"COPY deploy/validator/compose.yml {u.IMAGE_COMPOSE}" in dockerfile
    compose = (ROOT / "deploy/validator/compose.yml").read_text()
    assert f"image: {u.CURRENT} " in compose and str(u.PROJECT_DIR) in compose


def test_the_watch_says_each_outcome_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kokoah_subnet.validator.update.time.sleep", lambda s: None)
    host = Host(latest=OLDER, signed={OLD: 4, OLDER: 3}, installed=OLD)
    (tmp_path / u.STATE_FILE).write_text(json.dumps({"digest": OLD, "build": 4, "commit": ""}))
    updater, said = _updater(tmp_path, host)
    updater.watch(1, rounds=3)
    assert said[0].startswith("updater of commit ") and f"build 4 ({OLD[:19]})" in said[0]
    assert len(said) == 2 and said[1].startswith("refused")
