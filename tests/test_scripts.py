"""The operator scripts, run against a stand-in `docker` (nothing is started)."""

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

FAKE_DOCKER = """#!/usr/bin/env bash
# answers as if the container were running; records every command and the SQL sent on stdin
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  ps) echo kokoah-subnet-postgres ;;
  exec) if [ "$2" = "-i" ]; then cat >> "$FAKE_LOG"; fi ;;
esac
"""


def _run_db(tmp_path: Path, *args: str) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "KOKOAH_SUBNET_DB_ENV": str(tmp_path / "db.env"),
        "FAKE_LOG": str(log),
    }
    subprocess.run(["bash", str(SCRIPTS / "db.sh"), *args], env=env, check=True, capture_output=True, text=True)
    return log.read_text()


def test_the_read_only_role_reads_the_validator_database_only(tmp_path: Path) -> None:
    sent = _run_db(tmp_path, "start")
    assert "GRANT pg_read_all_data" not in sent and "REVOKE pg_read_all_data FROM kokoah_reader" in sent
    for grant in (
        "REVOKE ALL ON DATABASE kokoah_subnet FROM PUBLIC;",
        "GRANT CONNECT ON DATABASE kokoah_subnet TO kokoah_reader;",
        "GRANT USAGE ON SCHEMA public TO kokoah_reader;",
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO kokoah_reader;",
        "ALTER DEFAULT PRIVILEGES FOR ROLE kokoah IN SCHEMA public GRANT SELECT ON TABLES TO kokoah_reader;",
    ):
        assert grant in sent
    # every other database in the container refuses PUBLIC and the reader
    assert "REVOKE ALL ON DATABASE %I FROM PUBLIC, kokoah_reader" in sent
    assert "REVOKE ALL ON DATABASE kokoah_subnet_agents FROM PUBLIC" in _run_db(
        tmp_path, "create-db", "kokoah_subnet_agents"
    )


def test_a_failed_or_unfinished_archive_is_never_kept_or_counted(tmp_path: Path) -> None:
    """The bundles archive fails half-way: nothing named *.tmp stays behind. A *.tmp file another run is still
    writing is not one of the 14 kept, and one left by a killed run is swept once it is a day old."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "backup.sh").write_text((SCRIPTS / "backup.sh").read_text())
    (scripts / "db.sh").write_text('#!/usr/bin/env bash\n: > "$2"\n')  # `db.sh backup FILE [DB]`
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "tar").write_text('#!/usr/bin/env bash\n: > "$2"\n[ -z "${FAIL_TAR:-}" ]\n')  # `tar czf FILE ...`
    for p in (scripts / "db.sh", bin_dir / "tar"):
        p.chmod(0o755)
    backups, agents = tmp_path / "backups", tmp_path / "agents"
    (agents / "bundles").mkdir(parents=True)
    backups.mkdir()
    for d in range(1, 15):
        (backups / f"kokoah_subnet_agents_bundles-202601{d:02d}.tgz").write_bytes(b"x")
    writing = backups / "kokoah_subnet_agents_bundles-20260120.tgz.tmp"  # another run, still writing
    writing.write_bytes(b"")
    killed = backups / "kokoah_subnet-20260115.dump.tmp.4242"  # a killed run, two days ago
    killed.write_bytes(b"")
    os.utime(killed, (killed.stat().st_mtime - 2 * 86_400,) * 2)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "KOKOAH_SUBNET_BACKUP_DIR": str(backups),
        "KOKOAH_SUBNET_AGENT_DATA": str(agents),
    }

    def run(**extra: str) -> int:
        return subprocess.run(["bash", str(scripts / "backup.sh")], env=env | extra, capture_output=True).returncode

    assert run(FAIL_TAR="1") != 0
    assert sorted(p.name for p in backups.glob("*.tmp*")) == [writing.name]  # its own removed, the killed one swept
    assert run() == 0
    bundles = sorted(p.name for p in backups.glob("kokoah_subnet_agents_bundles-*.tgz"))
    assert len(bundles) == 14 and bundles[0] == "kokoah_subnet_agents_bundles-20260102.tgz" and writing.exists()


def test_a_set_but_missing_agent_folder_fails_the_backup(tmp_path: Path) -> None:
    """KOKOAH_SUBNET_AGENT_DATA set means the host runs the agent service: a wrong path fails the run (the timer shows
    it) instead of quietly leaving the agent database and code out of the backups."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "backup.sh").write_text((SCRIPTS / "backup.sh").read_text())
    (scripts / "db.sh").write_text('#!/usr/bin/env bash\n: > "$2"\n')
    (scripts / "db.sh").chmod(0o755)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "KOKOAH_SUBNET_BACKUP_DIR": str(tmp_path / "b")}
    ok = subprocess.run(["bash", str(scripts / "backup.sh")], env=env, capture_output=True)
    assert ok.returncode == 0  # a validator without the agent service backs up its record only
    missing = env | {"KOKOAH_SUBNET_AGENT_DATA": str(tmp_path / "no-such-folder")}
    failed = subprocess.run(["bash", str(scripts / "backup.sh")], env=missing, capture_output=True, text=True)
    assert failed.returncode != 0 and "no bundles" in failed.stderr


def test_a_service_role_owns_its_database_and_nothing_else(tmp_path: Path) -> None:
    sent = _run_db(tmp_path, "owner-role", "kokoah_agents", "kokoah_subnet_agents")
    assert "ALTER ROLE kokoah_agents WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in sent
    assert "ALTER DATABASE kokoah_subnet_agents OWNER TO kokoah_agents;" in sent
    assert "EXECUTE 'ALTER TABLE ' || t || ' OWNER TO kokoah_agents'" in sent  # the tables already there
    assert "REVOKE ALL ON DATABASE %I FROM PUBLIC, kokoah_agents" in sent  # every other database refuses it
    env = (tmp_path / "db.env").read_text()
    assert "KOKOAH_SUBNET_ROLE_KOKOAH_AGENTS_URL=postgresql+psycopg://kokoah_agents:" in env
    password = next(line for line in env.splitlines() if line.startswith("KOKOAH_SUBNET_ROLE_KOKOAH_AGENTS_PASSWORD="))
    commands = [line for line in sent.splitlines() if line.startswith(("exec ", "ps", "run ", "start "))]
    assert commands and not any(password.split("=", 1)[1] in line for line in commands)  # never an argument
    (tmp_path / "docker.log").write_text("")  # only the second run's commands from here
    again = _run_db(tmp_path, "owner-role", "kokoah_agents", "kokoah_subnet_agents")
    assert (tmp_path / "db.env").read_text() == env  # the same password kept
    assert "ALTER DATABASE kokoah_subnet_agents OWNER TO kokoah_agents;" in again  # and the grants made again


def test_a_service_role_never_takes_a_reserved_or_odd_name(tmp_path: Path) -> None:
    import pytest

    for args in (("kokoah", "kokoah_subnet_agents"), ("kokoah_reader", "x"), ("bad-name", "x"), ("ok", "bad;db")):
        with pytest.raises(subprocess.CalledProcessError):
            _run_db(tmp_path, "owner-role", *args)
