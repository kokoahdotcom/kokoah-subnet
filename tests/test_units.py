"""The systemd units in deploy/systemd: every service runs from the pinned install, never a working copy; all but
the backup run as a user of their own with the sandbox settings; secrets reach each service only as it needs them."""

import configparser
from pathlib import Path

UNITS = Path(__file__).resolve().parent.parent / "deploy" / "systemd"
SANDBOX = {
    "NoNewPrivileges": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "RestrictSUIDSGID": "yes",
    "CapabilityBoundingSet": "",
}


def service(name: str) -> dict[str, list[str]]:
    """The [Service] keys of a unit, each with every value it is given (systemd allows repeats)."""
    out: dict[str, list[str]] = {}
    section = None
    for line in (UNITS / name).read_text().splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[Service]" and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            out.setdefault(key, []).append(value)
    return out


def services() -> list[str]:
    return sorted(p.name for p in UNITS.glob("*.service"))


def test_every_service_runs_from_the_pinned_install() -> None:
    for name in services():
        for command in [v for k, vs in service(name).items() if k.startswith("Exec") for v in vs]:
            assert "/kokoah/" not in command and ".venv" not in command, (name, command)
        starts = service(name)["ExecStart"]
        assert any("/opt/kokoah-subnet/" in c for c in starts), name


def test_every_service_but_the_backup_has_a_user_and_the_sandbox() -> None:
    for name in services():
        keys = service(name)
        if name == "kokoah-subnet-backup.service":  # root: it dumps the database with docker
            assert "User" not in keys
            continue
        assert keys.get("User", ["root"])[0] not in ("", "root"), name
        for key, value in SANDBOX.items():
            assert keys.get(key) == [value], (name, key)


def test_secrets_reach_only_the_services_that_need_them() -> None:
    reader = {"kokoah-subnet-api.service", "kokoah-subnet-check.service"}
    for name in services():
        files = service(name).get("EnvironmentFile", [])
        if name in reader:  # the interface and the health check read with the read-only role
            assert "/etc/kokoah-subnet/db-reader.env" in files and "/etc/kokoah-subnet/db.env" not in files, name
        if name == "kokoah-subnet-agent-service.service":
            assert "/etc/kokoah-subnet/db.env" not in files
    burn = service("kokoah-subnet-burn.service")
    assert {c.split(":", 1)[0] for c in burn["LoadCredential"]} == {"coldkey", "coldkeypub", "hotkey"}
    assert all(
        p.startswith("/etc/kokoah-subnet/burn-wallet/") for c in burn["LoadCredential"] for p in [c.split(":", 1)[1]]
    )


def test_the_timers_name_their_services() -> None:
    for timer in sorted(UNITS.glob("*.timer")):
        parser = configparser.ConfigParser(strict=False)
        parser.read_string(timer.read_text())
        assert parser.has_option("Timer", "OnCalendar"), timer.name
        assert (UNITS / timer.name.replace(".timer", ".service")).exists(), timer.name


def test_every_shell_command_in_a_unit_is_valid() -> None:
    """A unit's `bash -c '…'` as the shell will see it ($$ and %% unescaped) parses, and holds no number where a
    shell variable belongs (a `$$` expanded when the file was written)."""
    import re
    import subprocess

    found = 0
    for name in services():
        for command in [v for k, vs in service(name).items() if k.startswith("Exec") for v in vs]:
            shell = re.fullmatch(r"/bin/(?:ba)?sh -c '(.*)'", command)
            if not shell:
                continue
            found += 1
            assert "$$" in shell.group(1) or "$" not in shell.group(1), (name, "a variable systemd would expand")
            body = shell.group(1).replace("$$", "$").replace("%%", "%")
            assert not re.search(r"\b\d{4,}(?:\(|day|done|stamp|KOKOAH)", body), (name, body)
            check = subprocess.run(["bash", "-n", "-c", body], capture_output=True, text=True)
            assert check.returncode == 0, (name, check.stderr)
    assert found  # the operator step at least
