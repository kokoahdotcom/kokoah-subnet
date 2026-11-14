"""The validator's updater (src/kokoah_subnet/validator/update.py) and install.sh, against a stand-in Docker and
cosign (nothing is started)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from kokoah_subnet.cli import build_parser, main
from kokoah_subnet.validator import migrations
from kokoah_subnet.validator import update as u
from kokoah_subnet.validator.store import Store

ROOT = Path(__file__).resolve().parent.parent
OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
OLDER = "sha256:" + "c" * 64
REPO = "kokoahdotcom/kokoah-subnet"


def _signature(digest: str, build: int, repo: str = f"index.docker.io/{REPO}") -> dict[str, object]:
    return {
        "critical": {
            "identity": {"docker-reference": repo},
            "image": {"docker-manifest-digest": digest},
            "type": "cosign container image signature",
        },
        "optional": {"build": str(build), "commit": f"{build:040d}", "Subject": u.WORKFLOW_IDENTITY},
    }


def _id(digest: str | None) -> str:
    return f"sha256:id-{digest[7:]}" if digest else ""


class Host:
    """Docker and cosign as the updater sees them: the published tag and its signatures, the local images and tags,
    the validator's and the updater's containers, and the ways each step can go wrong."""

    def __init__(self, project: Path, latest: str, signed: dict[str, int]) -> None:
        self.project = project
        self.latest = latest
        self.signed = signed  # digest -> build of the release workflow's signature
        self.foreign: set[str] = set()  # digests whose signature names another repository
        self.pulled: set[str] = set()
        self.others: list[dict[str, str]] = []  # other repositories' images
        self.tags: dict[str, str] = {}  # local tag -> digest
        self.validator: str | None = None  # the release the validator's container runs
        self.updater: str | None = None  # and the updater's
        self.restarts = 0
        self.not_ready: set[str] = set()  # releases whose validator never becomes healthy
        self.crashing: set[str] = set()  # releases whose validator restarts after it started
        self.bad_compose: set[str] = set()  # releases whose compose file does not work on this host
        self.floor: dict[str, int] = {}  # a release's ROLLBACK_FLOOR
        self.schema: dict[str, int] = {}  # a release's schema version
        self.fail_others = 0  # how many times starting the other services fails
        self.fail_tag = 0  # how many times `docker tag` fails
        self.fail_inspect = 0  # how many validator state reads fail
        self.docker_down = False
        self.services = "validator\ninterface\ncheck\nupdater\n"
        self.calls: list[list[str]] = []

    def installed(self, digest: str, build: int) -> None:
        """A host where `digest` was installed as `build` and runs."""
        self.pulled.add(digest)
        self.tags[u.CURRENT] = digest
        self.validator = self.updater = digest
        (self.project / "compose.yml").write_text(f"# compose file of {digest}\n")
        (self.project / u.STATE_FILE).write_text(json.dumps({"digest": digest, "build": build, "commit": ""}))

    def head(self, image: str, tag: str) -> str:
        assert (image, tag) == (u.IMAGE, u.CHANNEL)
        return self.latest

    def _resolve(self, ref: str) -> str:
        if ref in self.tags:
            return self.tags[ref]
        if "@" in ref and ref.rsplit("@", 1)[1] in self.pulled:
            return ref.rsplit("@", 1)[1]
        raise u.UpdateError(f"Error response from daemon: No such image: {ref}")

    def run(self, args: list[str], timeout: float) -> str:
        self.calls.append(args)
        if args[0] == "docker" and self.docker_down:
            raise u.UpdateError("Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
        if args[:2] == ["cosign", "verify"]:
            assert args[3:] == [
                "--certificate-identity", u.WORKFLOW_IDENTITY, "--certificate-oidc-issuer", u.OIDC_ISSUER
            ]  # fmt: skip
            assert "@" in args[2], "always verified by digest"
            digest = args[2].rsplit("@", 1)[1]
            if digest not in self.signed:
                raise u.UpdateError("no matching signatures")
            repo = "index.docker.io/someone/else" if digest in self.foreign else f"index.docker.io/{REPO}"
            return json.dumps([_signature(digest, self.signed[digest], repo)])
        if args[:3] == ["docker", "image", "inspect"]:
            digest = self._resolve(args[3])
            return json.dumps([f"{REPO}@{digest}"]) if "RepoDigests" in args[5] else _id(digest) + "\n"
        if args[:3] == ["docker", "inspect", "--format"] and args[3] == "{{.Image}}":
            running = {u.VALIDATOR: self.validator, u.UPDATER: self.updater}[args[4]]
            if running is None:
                raise u.UpdateError(f"Error: No such object: {args[4]}")
            return _id(running) + "\n"
        if args[:3] == ["docker", "inspect", "--format"]:
            if self.fail_inspect:
                self.fail_inspect -= 1
                raise u.UpdateError("Error response from daemon: i/o timeout")
            if self.validator in self.crashing:
                self.restarts += 1
            return f"true {self.restarts} healthy\n"
        if args[:2] == ["docker", "pull"]:
            self.pulled.add(args[3].rsplit("@", 1)[1])
            return ""
        if args[:2] == ["docker", "tag"]:
            if self.fail_tag:
                self.fail_tag -= 1
                raise u.UpdateError("Error response from daemon: disk full")
            self.tags[args[3]] = self._resolve(args[2])
            return ""
        if args[:2] == ["docker", "run"]:
            digest = self._resolve(args[7])
            if args[6] == "cat":
                return f"# compose file of {digest}\n"
            return f"{self.floor.get(digest, 0) if 'ROLLBACK_FLOOR' in args[9] else self.schema.get(digest, 15)}\n"
        if args[:3] == ["docker", "image", "ls"]:
            rows = [{"Repository": REPO, "Digest": d, "ID": _id(d)} for d in sorted(self.pulled)] + self.others
            return "\n".join(json.dumps(r) for r in rows) + "\n"
        if args[:3] == ["docker", "image", "rm"]:
            self.pulled.discard(args[3].rsplit("@", 1)[1])
            return ""
        if args[:2] == ["docker", "compose"]:
            file, rest = args[5], args[6:]
            if rest[:2] == ["config", "--quiet"]:
                if any(d in Path(file).read_text() for d in self.bad_compose):
                    raise u.UpdateError("env file /etc/kokoah-subnet/new.env not found")
            elif rest[:2] == ["config", "--services"]:
                return self.services
            elif "--wait" in rest:
                self.validator = self.tags[u.CURRENT]
                if self.validator in self.not_ready:
                    raise u.UpdateError(f"container {u.VALIDATOR} is unhealthy")
            elif "--remove-orphans" in rest and self.fail_others:
                self.fail_others -= 1
                raise u.UpdateError("Error response from daemon: conflict")
            elif rest == ["up", "-d", "--no-deps", "updater"]:
                self.updater = self.tags[u.CURRENT]
        return ""

    def commands(self) -> list[str]:
        """The calls in short: compose calls without their project options, except a file other than compose.yml."""
        out = []
        for c in self.calls:
            if c[:2] == ["docker", "compose"]:
                name = Path(c[5]).name
                out.append(" ".join(["docker", "compose", *([f"-f {name}"] if name != "compose.yml" else []), *c[6:]]))
            else:
                out.append(" ".join(c))
        return out

    def count(self, prefix: str) -> int:
        return sum(c.startswith(prefix) for c in self.commands())


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def __call__(self) -> float:
        return self.now


def _setup(tmp_path: Path, latest: str, signed: dict[str, int]) -> tuple[Host, u.Updater, list[str], Clock]:
    host = Host(tmp_path, latest, signed)
    said: list[str] = []
    clock = Clock()
    updater = u.Updater(
        tmp_path, run=host.run, head=host.head, say=said.append, sleep=clock.sleep, clock=clock, wall=clock
    )
    return host, updater, said, clock


def _state(tmp_path: Path) -> dict[str, object]:
    data = json.loads((tmp_path / u.STATE_FILE).read_text())
    assert isinstance(data, dict)
    return data


RECONCILE = [
    f"docker image inspect {u.CURRENT} --format {{{{json .RepoDigests}}}}",
    f"docker image inspect {u.CURRENT} --format {{{{.Id}}}}",
    f"docker inspect --format {{{{.Image}}}} {u.VALIDATOR}",
    f"docker inspect --format {{{{.Image}}}} {u.UPDATER}",
]


def test_nothing_happens_while_the_host_runs_the_release_the_tag_names(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=OLD, signed={OLD: 4})
    host.installed(OLD, 4)
    assert updater.check() == "up to date"
    assert host.commands() == RECONCILE


def test_a_new_signed_release_is_checked_installed_and_kept_only_if_its_validator_runs(tmp_path: Path) -> None:
    host, updater, _, clock = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.pulled.add(OLDER)  # a release before that, still on disk
    host.others.append({"Repository": f"{REPO}-agent", "Digest": OLDER, "ID": "sha256:other"})
    assert updater.check() == f"installed build 5 (commit {f'{5:040d}'[:12]}, {NEW[:19]})"
    ref = f"{u.IMAGE}@{NEW}"
    steps = host.commands()
    assert steps[: len(RECONCILE)] == RECONCILE
    assert steps[len(RECONCILE) :] == [
        f"cosign verify {ref} --certificate-identity {u.WORKFLOW_IDENTITY} --certificate-oidc-issuer {u.OIDC_ISSUER}",
        f"docker pull --quiet {ref}",
        f"docker run --rm --network none --entrypoint cat {ref} {u.IMAGE_COMPOSE}",
        f"docker compose -f {u.NEW_COMPOSE} config --quiet",
        f"docker image inspect {u.IMAGE}@{OLD} --format {{{{.Id}}}}",
        f"docker tag {u.IMAGE}@{OLD} {u.PREVIOUS}",
        f"docker tag {ref} {u.CURRENT}",
        f"docker compose up -d --no-deps --wait --wait-timeout {u.VALIDATOR_WAIT_S} validator",
        *[f"docker inspect --format {u.VALIDATOR_STATE} {u.VALIDATOR}"] * 2,
        "docker compose config --services",
        "docker compose up -d --no-deps --remove-orphans interface check",
        f"docker image inspect {u.CURRENT} --format {{{{.Id}}}}",
        f"docker image inspect {u.PREVIOUS} --format {{{{.Id}}}}",
        "docker image ls --digests --no-trunc --format {{json .}}",
        f"docker image rm {REPO}@{OLDER}",  # this repository's only; the agent image stays
        f"docker rm -f {u.HOP}",
        f"docker compose run -d --rm --no-deps --name {u.HOP} updater validator update hop --project-dir {tmp_path}",
    ]
    assert clock.slept == [u.STEADY_S]
    assert (tmp_path / "compose.yml").read_text() == f"# compose file of {NEW}\n"
    assert not (tmp_path / u.NEW_COMPOSE).exists()
    assert _state(tmp_path) == {"digest": NEW, "build": 5, "commit": f"{5:040d}"}
    assert host.tags == {u.CURRENT: NEW, u.PREVIOUS: OLD} and host.pulled == {OLD, NEW}
    # the helper container's `hop` recreates the updater; then the host is up to date
    updater.hop()
    assert host.updater == NEW and updater.check() == "up to date"


def test_a_validator_that_never_gets_ready_is_rolled_back_and_its_release_refused(tmp_path: Path) -> None:
    host, updater, said, clock = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.not_ready.add(NEW)
    reason = f"not ready: docker compose up: container {u.VALIDATOR} is unhealthy"
    assert updater.check() == f"went back to build 4: build 5 {reason}"
    assert host.tags[u.CURRENT] == OLD and host.validator == OLD
    assert (tmp_path / "compose.yml").read_text() == f"# compose file of {OLD}\n"  # from the old release's image
    assert _state(tmp_path)["digest"] == OLD
    assert json.loads((tmp_path / u.REFUSED_FILE).read_text())[NEW]["why"] == reason
    assert host.count("docker compose run") == 0  # the updater it is stays
    assert any("going back" in s for s in said)
    # not tried again, not even checked again, for six hours; then once more
    host.calls.clear()
    assert updater.check().startswith(f"refused {NEW[:19]}: this host went back from it")
    assert host.count("cosign") == 0
    clock.now += u.REFUSED_RETRY_S
    host.not_ready.clear()
    assert updater.check().startswith("installed build 5")


def test_a_validator_that_restarts_after_it_started_is_rolled_back(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.crashing.add(NEW)
    assert updater.check() == "went back to build 4: build 5 stopped or restarted after it started"
    assert host.tags[u.CURRENT] == OLD and _state(tmp_path)["digest"] == OLD


def test_a_rollback_that_stops_halfway_is_finished_by_the_next_look(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.not_ready.add(NEW)
    original = host.run

    def tag_fails_on_the_way_back(args: list[str], timeout: float) -> str:
        if args[:2] == ["docker", "tag"] and args[2] == f"{u.IMAGE}@{OLD}" and args[3] == u.CURRENT:
            host.fail_tag = 1
        return original(args, timeout)

    updater.run = tag_fails_on_the_way_back
    with pytest.raises(u.UpdateError, match="disk full"):
        updater.check()
    assert host.tags[u.CURRENT] == NEW and not (tmp_path / u.REFUSED_FILE).exists()
    # the next look sees current away from the installed release and puts it back before anything else
    updater.run = original
    assert updater.check() == "put back build 4"
    assert host.tags[u.CURRENT] == OLD and host.validator == OLD
    assert (tmp_path / "compose.yml").read_text() == f"# compose file of {OLD}\n"


def test_a_validator_left_on_another_image_is_put_back(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=OLD, signed={OLD: 4})
    host.installed(OLD, 4)
    host.pulled.add(NEW)
    host.validator = NEW  # stopped after the tag went back, before the container did
    assert updater.check() == "put back build 4"
    assert host.validator == OLD


def test_an_updater_that_did_not_restart_is_restarted(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={NEW: 5})
    host.installed(NEW, 5)
    host.pulled.add(OLD)
    host.updater = OLD  # its helper container failed
    assert updater.check() == "restarting the updater with build 5"
    assert host.count("docker compose run -d --rm --no-deps --name") == 1


def test_docker_silent_during_the_steady_minutes_refuses_nothing(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.fail_inspect = 3
    with pytest.raises(u.UpdateError, match="i/o timeout"):
        updater.check()
    assert not (tmp_path / u.REFUSED_FILE).exists()
    assert updater.check() == "put back build 4"  # back to the recorded release ...
    assert updater.check().startswith("installed build 5")  # ... and the new one again


def test_docker_down_while_starting_is_an_error_not_a_verdict(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    original = host.run

    def down_at_the_start(args: list[str], timeout: float) -> str:
        if "--wait" in args:
            host.docker_down = True
        return original(args, timeout)

    updater.run = down_at_the_start
    with pytest.raises(u.UpdateError, match="Cannot connect"):
        updater.check()
    assert not (tmp_path / u.REFUSED_FILE).exists()


def test_when_neither_release_runs_the_host_is_the_problem_and_nothing_is_refused(tmp_path: Path) -> None:
    host, updater, _, clock = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.not_ready.update({NEW, OLD})  # the record's database is down, say
    assert "neither release runs" in updater.check()
    assert not (tmp_path / u.REFUSED_FILE).exists() and host.tags[u.CURRENT] == OLD
    host.not_ready.clear()
    clock.now += u.RETRY_S
    assert updater.check().startswith("installed build 5")


def test_no_rollback_onto_a_record_the_old_code_cannot_read(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.not_ready.add(NEW)
    host.floor[NEW], host.schema[OLD] = 16, 15  # the new release changed the record in a way the old cannot follow
    assert "stays although its validator does not run" in updater.check()
    assert host.tags[u.CURRENT] == NEW and _state(tmp_path)["digest"] == NEW
    # no look puts the old one back; the updater follows the release that stays
    assert updater.check() == "restarting the updater with build 5"
    updater.hop()
    assert updater.check() == "up to date" and host.tags[u.CURRENT] == NEW


def test_a_compose_file_that_does_not_work_here_changes_nothing(tmp_path: Path) -> None:
    host, updater, _, clock = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.bad_compose.add(NEW)
    with pytest.raises(u.UpdateError, match="nothing changed: docker compose config: env file"):
        updater.check()
    assert host.tags == {u.CURRENT: OLD} and host.validator == OLD
    assert (tmp_path / "compose.yml").read_text() == f"# compose file of {OLD}\n"
    assert host.count("docker tag") == 0 and host.count("docker compose up") == 0
    # looked at again only after an hour (a fixed host or a new release)
    host.calls.clear()
    assert updater.check().startswith("not installed: its compose file does not work here")
    assert host.count("cosign") == 0
    clock.now += u.RETRY_S
    host.bad_compose.clear()
    assert updater.check().startswith("installed build 5")


def test_an_install_stopped_halfway_goes_back_then_installs_again_with_the_good_previous(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.fail_others = 1
    with pytest.raises(u.UpdateError, match="conflict"):
        updater.check()
    assert host.tags[u.CURRENT] == NEW and _state(tmp_path)["digest"] == OLD  # not recorded as installed
    assert updater.check() == "put back build 4"
    assert updater.check().startswith("installed build 5")
    assert host.tags == {u.CURRENT: NEW, u.PREVIOUS: OLD} and _state(tmp_path)["digest"] == NEW


def test_an_unsigned_image_on_the_tag_is_never_pulled_and_not_checked_every_look(tmp_path: Path) -> None:
    host, updater, _, clock = _setup(tmp_path, latest=NEW, signed={OLD: 4})
    host.installed(OLD, 4)
    with pytest.raises(u.UpdateError, match="no matching signatures"):
        updater.check()
    assert updater.check() == "not installed: no matching signatures"
    assert host.count("cosign") == 1
    assert host.count("docker pull") == 0 and host.count("docker tag") == 0
    clock.now += u.RETRY_S
    with pytest.raises(u.UpdateError):
        updater.check()
    assert host.count("cosign") == 2


def test_a_signature_made_for_another_repository_is_refused(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.foreign.add(NEW)
    with pytest.raises(u.UpdateError, match="no signature of the release workflow"):
        updater.check()
    assert host.tags == {u.CURRENT: OLD}


def test_an_older_signed_release_put_back_on_the_tag_is_refused(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=OLDER, signed={OLD: 4, OLDER: 3})
    host.installed(OLD, 4)
    assert updater.check().startswith(f"refused {OLDER[:19]} (build 3")
    assert updater.check().startswith(f"refused {OLDER[:19]} (build 3")
    assert host.count("cosign") == 1 and host.count("docker pull") == 0
    assert host.tags == {u.CURRENT: OLD}


def test_the_installed_release_file_is_the_floor_even_when_the_old_signature_is_gone(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=OLDER, signed={OLDER: 3})  # the installed one's was deleted
    host.installed(OLD, 4)
    assert updater.check().startswith("refused")
    assert host.tags == {u.CURRENT: OLD}


def test_docker_not_answering_is_an_error_not_a_reinstall(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=OLD, signed={OLD: 4})
    host.installed(OLD, 4)
    host.docker_down = True
    with pytest.raises(u.UpdateError, match="Cannot connect"):
        updater.check()
    assert host.count("cosign") == 0 and host.count("docker pull") == 0


def test_without_the_interface_profile_only_the_check_follows_the_validator(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.services = "validator\ncheck\nupdater\n"
    updater.check()
    assert "docker compose up -d --no-deps --remove-orphans check" in host.commands()


def test_one_update_at_a_time(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=OLD, signed={OLD: 4})
    host.installed(OLD, 4)
    with updater._locked(), pytest.raises(u.UpdateError, match="another update is running"):
        u.Updater(tmp_path, run=host.run, head=host.head).check()
    assert updater.check() == "up to date"


def test_the_first_start_verifies_its_own_image_and_starts_the_updater(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={NEW: 5})
    assert updater.setup(NEW).startswith("installed build 5")
    steps = host.commands()
    assert steps[0].startswith(f"cosign verify {u.IMAGE}@{NEW} ")
    assert not any(s.startswith("docker tag") and u.PREVIOUS in s for s in steps)  # nothing to keep as previous
    assert steps[-1] == "docker compose up -d --no-deps updater"
    assert host.count("docker compose run") == 0
    assert host.tags == {u.CURRENT: NEW} and host.updater == NEW and _state(tmp_path)["build"] == 5


def test_a_first_start_whose_validator_does_not_run_fails_and_still_starts_the_updater(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={NEW: 5})
    host.not_ready.add(NEW)
    with pytest.raises(u.UpdateError, match="no release to go back to"):
        updater.setup(NEW)
    assert not (tmp_path / u.STATE_FILE).exists() and host.updater == NEW
    # the updater does not try the same release every look
    host.calls.clear()
    assert updater.check().startswith("not installed: its validator does not run")
    assert host.count("docker compose up") == 0


def test_install_again_that_rolls_back_is_an_error(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={OLD: 4, NEW: 5})
    host.installed(OLD, 4)
    host.updater = None  # install.sh stopped it
    host.not_ready.add(NEW)
    with pytest.raises(u.UpdateError, match="went back to build 4"):
        updater.setup(NEW)
    assert host.updater == OLD  # started again, on the release that runs


def test_running_install_again_never_goes_back_unless_asked(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=OLDER, signed={OLD: 4, OLDER: 3})
    host.installed(OLD, 4)
    host.updater = None
    with pytest.raises(u.UpdateError, match="older than the installed build 4"):
        updater.setup(OLDER)
    assert host.tags == {u.CURRENT: OLD} and host.updater == OLD
    assert updater.setup(OLDER, allow_older=True).startswith("installed build 3")
    assert host.tags[u.CURRENT] == OLDER


def test_the_first_start_refuses_a_digest_without_the_workflow_signature(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={})
    with pytest.raises(u.UpdateError):
        updater.setup(NEW)
    assert host.tags == {}


def test_a_local_build_tagged_current_is_replaced_by_the_signed_release(tmp_path: Path) -> None:
    host, updater, _, _ = _setup(tmp_path, latest=NEW, signed={NEW: 5})
    assert updater.check().startswith("installed build 5")


def test_cosign_output_for_a_digest_must_be_that_digest() -> None:
    def run(args: list[str], timeout: float) -> str:
        return json.dumps([_signature(OLD, 4)])

    with pytest.raises(u.UpdateError, match="another image"):
        u.verify(run, f"{u.IMAGE}@{NEW}")
    assert u.verify(run, f"{u.IMAGE}@{OLD}") == u.Release(OLD, 4, f"{4:040d}")


def test_of_several_signatures_the_highest_build_counts_and_two_images_are_refused() -> None:
    def run_with(items: list[dict[str, object]]) -> u.Runner:
        return lambda args, timeout: json.dumps(items)

    assert u.verify(run_with([_signature(OLD, 4), _signature(OLD, 6)]), f"{u.IMAGE}@{OLD}").build == 6
    with pytest.raises(u.UpdateError, match="different images"):
        u.verify(run_with([_signature(OLD, 4), _signature(NEW, 5)]), f"{u.IMAGE}:latest")


def test_a_signature_without_a_build_number_does_not_count() -> None:
    def run(args: list[str], timeout: float) -> str:
        item = _signature(OLD, 4)
        item["optional"] = {}
        return json.dumps([item])

    with pytest.raises(u.UpdateError):
        u.verify(run, f"{u.IMAGE}@{OLD}")


def test_the_installed_digest_is_this_repositorys_among_the_names_docker_lists(tmp_path: Path) -> None:
    names = [f"kokoah-subnet@{OLDER}", f"docker.io/{REPO}-agent@{OLD}", f"docker.io/{REPO}@{NEW}"]
    updater = u.Updater(tmp_path, run=lambda args, timeout: json.dumps(names))
    assert updater.installed_digest() == NEW


def _registry(
    answer: httpx.Response | None = None, token: dict[str, str] | None = None, realm: str = ""
) -> tuple[httpx.Client, list[tuple[str, str]]]:
    seen: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.url.host == "auth.docker.io":
            assert request.url.params["scope"] == f"repository:{REPO}:pull"
            return httpx.Response(200, json=token or {"token": "anon"})
        if request.headers.get("authorization") not in ("Bearer anon", "Bearer other"):
            challenge = f'Bearer realm="{realm or "https://auth.docker.io/token"}",service="registry.docker.io",'
            return httpx.Response(401, headers={"www-authenticate": challenge + f'scope="repository:{REPO}:pull"'})
        return answer or httpx.Response(200, headers={"docker-content-digest": NEW})

    return httpx.Client(transport=httpx.MockTransport(handle)), seen


def test_the_tag_is_read_with_a_head_request_and_an_anonymous_token() -> None:
    client, seen = _registry()
    with client:
        assert u.remote_digest(u.IMAGE, "latest", client) == NEW
    assert [m for m, _ in seen] == ["HEAD", "GET", "HEAD"]
    assert seen[0][1] == f"https://registry-1.docker.io/v2/{REPO}/manifests/latest"
    client, _ = _registry(token={"access_token": "other"})
    with client:
        assert u.remote_digest(u.IMAGE, "latest", client) == NEW


@pytest.mark.parametrize(
    ("answer", "realm", "error"),
    [
        (httpx.Response(404), "", "answered 404"),
        (httpx.Response(200), "", "gave no digest"),
        (httpx.Response(200, headers={"docker-content-digest": "sha256:short"}), "", "gave no digest"),
        (None, "http://auth.example/token", "unknown login"),
    ],
)
def test_registry_answers_that_name_no_digest_are_errors(answer: httpx.Response | None, realm: str, error: str) -> None:
    client, _ = _registry(answer, realm=realm)
    with client, pytest.raises(u.UpdateError, match=error):
        u.remote_digest(u.IMAGE, "latest", client)


def test_repository_names_compare_as_docker_and_cosign_print_them() -> None:
    for name in (REPO, f"docker.io/{REPO}", f"index.docker.io/{REPO}"):
        assert u._same_repo(name, u.IMAGE)
    for name in (f"{REPO}-agent", "ghcr.io/kokoahdotcom/kokoah-subnet", "evil/kokoah-subnet", "kokoah-subnet"):
        assert not u._same_repo(name, u.IMAGE)


def test_ready_says_whether_the_record_is_migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOKOAH_SUBNET_DB_URL", raising=False)
    monkeypatch.delenv("KOKOAH_SUBNET_DB_READER_URL", raising=False)
    assert main(["validator", "ready", "--data-dir", str(tmp_path)]) == 1  # no record yet
    Store(tmp_path / "validator.sqlite").close()
    assert main(["validator", "ready", "--data-dir", str(tmp_path)]) == 0


def test_the_rollback_floor_names_a_step_and_the_updater_reads_it_as_it_is_written() -> None:
    assert 0 <= migrations.ROLLBACK_FLOOR <= migrations.latest_version()
    for code, expected in (
        (u.ROLLBACK_FLOOR_CODE, migrations.ROLLBACK_FLOOR),
        (u.SCHEMA_CODE, migrations.latest_version()),
    ):
        done = subprocess.run([shutil.which("python") or "python3", "-c", code], capture_output=True, text=True)
        assert done.returncode == 0 and done.stdout.strip() == str(expected)


def test_the_commands_an_older_updater_calls_in_a_newer_image_keep_working() -> None:
    """The old updater runs `hop` in the new image, and install.sh runs `setup`: renaming them would stop updates."""
    hop = build_parser().parse_args(["validator", "update", "hop", "--project-dir", "/x"])
    assert (hop.validator_command, hop.update_command, str(hop.project_dir)) == ("update", "hop", "/x")
    setup = build_parser().parse_args(["validator", "update", "setup", "--digest", NEW, "--allow-older"])
    assert setup.allow_older and setup.digest == NEW
    watch = build_parser().parse_args(["validator", "update", "watch", "--project-dir", "/x"])
    assert watch.interval == u.INTERVAL_S


def test_the_compose_file_keeps_the_names_the_updater_and_older_hosts_rely_on() -> None:
    compose = (ROOT / "deploy/validator/compose.yml").read_text()
    # a renamed project or container would leave the old validator running beside the new one
    assert compose.count("\nname: kokoah-subnet\n") == 1
    for service, container in (("validator", u.VALIDATOR), ("interface", "kokoah-subnet-api"),
                               ("check", "kokoah-subnet-check"), ("updater", u.UPDATER)):  # fmt: skip
        assert re.search(rf"\n  {service}:\n    <<: \*service\n    container_name: {container}\n", compose)
    assert f"image: {u.CURRENT} " in compose and "pull_policy: never" in compose
    assert f'"--project-dir", "{u.PROJECT_DIR}"' in compose


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker compose")
def test_the_compose_file_is_valid(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("KOKOAH_UID=998\nKOKOAH_GID=998\nCOMPOSE_PROFILES=interface\n")
    args = ["docker", "compose", "--project-directory", str(tmp_path), "-f", str(ROOT / "deploy/validator/compose.yml")]
    if subprocess.run([*args[:2], "version"], capture_output=True).returncode != 0:
        pytest.skip("needs docker compose")
    subprocess.run([*args, "config", "--quiet", "--no-env-resolution"], check=True, capture_output=True)
    services = subprocess.run(
        [*args, "config", "--services", "--no-env-resolution"], check=True, capture_output=True, text=True
    )
    assert set(services.stdout.split()) == {"validator", "interface", "check", "updater"}


def test_install_script_and_image_pin_what_the_updater_trusts() -> None:
    install = (ROOT / "deploy/validator/install.sh").read_text()
    dockerfile = (ROOT / "deploy/validator/Dockerfile").read_text()
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert f"image={u.IMAGE}\n" in install
    assert f"identity={u.WORKFLOW_IDENTITY}\n" in install and f"issuer={u.OIDC_ISSUER}\n" in install
    assert f"IMAGE: {u.IMAGE}\n" in workflow
    assert f'--certificate-identity "{u.WORKFLOW_IDENTITY}"' in workflow  # it checks its own signature
    assert u.WORKFLOW_IDENTITY.endswith(".github/workflows/release.yml@refs/heads/main")
    cosign = re.search(r"^cosign=(\S+)$", install, re.M)
    assert cosign and f"COPY --from={cosign.group(1)} " in dockerfile  # the same cosign verifies on both sides
    assert "cosign-release: " + cosign.group(1).split(":")[1].split("@")[0] in workflow
    assert f"COPY deploy/validator/compose.yml {u.IMAGE_COMPOSE}" in dockerfile


FAKE_SYSTEMCTL = """#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >> "$FAKE_LOG"
if [ "$1" = list-units ]; then printf '%s' "${FAKE_UNITS:-}"; fi
"""

FAKE_DOCKER = """#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  version) echo "${FAKE_API:-1.51}" ;;
  run) case " $* " in
         *" verify "*) echo '[{"critical":{"image":{"docker-manifest-digest":"DIGEST"}}}]' ;;
         *" setup "*) exit "${FAKE_SETUP_EXIT:-0}" ;;
       esac ;;
esac
"""


def _install(
    tmp_path: Path, *args: str, units: str = "", wants: str = "", api: str = "1.51", setup_exit: int = 0
) -> tuple[int, str, str]:
    bin_dir, project, systemd = tmp_path / "bin", tmp_path / "opt", tmp_path / "systemd"
    for d in (bin_dir, project, systemd / "multi-user.target.wants"):
        d.mkdir(parents=True, exist_ok=True)
    (project / ".env").write_text("KOKOAH_UID=998\n")
    if wants:
        (systemd / "multi-user.target.wants" / wants).write_text("")
    (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
    (bin_dir / "docker").write_text(FAKE_DOCKER.replace("DIGEST", NEW))
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    log = tmp_path / "calls.log"
    log.unlink(missing_ok=True)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_UNITS": units,
        "FAKE_API": api,
        "FAKE_SETUP_EXIT": str(setup_exit),
        "KOKOAH_DOCKER_DIR": str(project),
        "KOKOAH_SYSTEMD_DIR": str(systemd),
    }
    done = subprocess.run(
        ["bash", str(ROOT / "deploy/validator/install.sh"), *args], env=env, capture_output=True, text=True
    )
    return done.returncode, done.stderr, log.read_text() if log.exists() else ""


def test_install_refuses_while_a_systemd_validator_runs_or_would_start_again(tmp_path: Path) -> None:
    code, err, calls = _install(tmp_path, units="kokoah-subnet-validator@sn39.service loaded activating auto-restart\n")
    assert code == 1 and "systemctl disable --now kokoah-subnet-validator@sn39.service" in err
    assert "docker" not in calls
    code, err, _ = _install(tmp_path / "b", wants="kokoah-subnet-validator@main.service")  # stopped, still enabled
    assert code == 1 and "kokoah-subnet-validator@main.service" in err


def test_install_goes_ahead_past_units_that_are_stopped_or_failed_and_disabled(tmp_path: Path) -> None:
    units = "kokoah-subnet-api.service loaded inactive dead\nkokoah-subnet-check.service loaded failed failed\n"
    code, err, calls = _install(tmp_path, units=units)
    assert code == 0, err
    assert "validator update setup" in calls


def test_install_without_a_systemd_folder_goes_ahead(tmp_path: Path) -> None:
    _install(tmp_path)
    shutil.rmtree(tmp_path / "systemd")
    env_dir = tmp_path / "systemd"
    assert not env_dir.exists()
    bin_dir = tmp_path / "bin"
    done = subprocess.run(
        ["bash", str(ROOT / "deploy/validator/install.sh")],
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_LOG": str(tmp_path / "calls.log"),
            "KOKOAH_DOCKER_DIR": str(tmp_path / "opt"),
            "KOKOAH_SYSTEMD_DIR": str(env_dir),
        },
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


def test_install_takes_only_its_own_option(tmp_path: Path) -> None:
    code, err, calls = _install(tmp_path, "--digest", OLD)
    assert code == 2 and "usage" in err and calls == ""


def test_install_needs_a_recent_docker(tmp_path: Path) -> None:
    code, err, _ = _install(tmp_path, api="1.43")
    assert code == 1 and "Docker Engine 25" in err


def test_install_stops_a_running_updater_and_hands_the_verified_digest_over(tmp_path: Path) -> None:
    code, err, calls = _install(tmp_path, "--allow-older")
    assert code == 0, err
    lines = calls.splitlines()
    stop = lines.index("docker stop kokoah-subnet-updater")
    setup = next(i for i, line in enumerate(lines) if "validator update setup" in line)
    assert stop < setup
    assert lines[setup].endswith(f"{u.IMAGE}@{NEW} validator update setup --digest {NEW} "
                                 f"--project-dir {tmp_path / 'opt'} --allow-older")  # fmt: skip


def test_install_starts_the_updater_again_when_the_setup_fails(tmp_path: Path) -> None:
    code, _, calls = _install(tmp_path, setup_exit=1)
    assert code == 1
    assert calls.splitlines()[-1] == "docker start kokoah-subnet-updater"


def test_the_watch_says_each_outcome_once(tmp_path: Path) -> None:
    host, updater, said, _ = _setup(tmp_path, latest=OLDER, signed={OLD: 4, OLDER: 3})
    host.installed(OLD, 4)
    updater.watch(1, rounds=3)
    assert said[0].startswith("updater of commit ") and f"build 4 ({OLD[:19]})" in said[0]
    assert len(said) == 2 and said[1].startswith("refused")
