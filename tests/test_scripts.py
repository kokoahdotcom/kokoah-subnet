"""The operator scripts, run against a stand-in `docker` (nothing is started)."""

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

FAKE_DOCKER = """#!/usr/bin/env bash
# answers as if the container were running; records every command and the SQL sent on stdin. Its volume and
# container are reported only with FAKE_RECORD_EXISTS: by default the env file comes first, as on a new host. A login
# from the host works unless FAKE_BAD_LOGIN; FAKE_NO_DOCKER: the daemon refuses this user; FAKE_ROLE_EXISTS: the
# read-only role is there already
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  info) [ -z "${FAKE_NO_DOCKER:-}" ]; exit $? ;;
  ps) echo kokoah-subnet-postgres ;;
  volume|container) if [ "$2" = inspect ]; then [ -n "${FAKE_RECORD_EXISTS:-}" ]; exit $?; fi ;;
  inspect) echo postgres:test ;;
  run) case " $* " in *" psql "*) [ -n "${FAKE_BAD_LOGIN:-}" ] || echo 1 ;; esac ;;
  exec) if [ "$2" = "-i" ]; then cat >> "$FAKE_LOG"; elif [ -n "${FAKE_ROLE_EXISTS:-}" ]; then
          case " $* " in *pg_roles*) echo 1 ;; esac
        else case " $* " in *pg_roles*) echo "${FAKE_ROLE_COUNT-0}" ;; esac; fi ;;
esac
"""


def _db_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "KOKOAH_SUBNET_DB_ENV": str(tmp_path / "db.env"),
        "FAKE_LOG": str(tmp_path / "docker.log"),
    }


def _run_db(tmp_path: Path, *args: str) -> str:
    env = _db_env(tmp_path)
    subprocess.run(["bash", str(SCRIPTS / "db.sh"), *args], env=env, check=True, capture_output=True, text=True)
    return (tmp_path / "docker.log").read_text()


def test_an_existing_record_never_gets_new_passwords(tmp_path: Path) -> None:
    """A record whose env file is elsewhere (made with sudo, or kept in /etc by the one-command setup): new passwords
    would lock the validator out and reset the read-only role's, so db.sh stops and says where they must come from."""
    env = {**_db_env(tmp_path), "FAKE_RECORD_EXISTS": "1"}
    for args in (["start"], ["url"], ["owner-role", "kokoah_agents", "kokoah_subnet_agents"]):
        run = subprocess.run(["bash", str(SCRIPTS / "db.sh"), *args], env=env, capture_output=True, text=True)
        assert run.returncode == 1 and "passwords are not in" in run.stderr and "KOKOAH_SUBNET_DB_ENV" in run.stderr
        assert not (tmp_path / "db.env").exists()
    assert "ALTER ROLE" not in (tmp_path / "docker.log").read_text()
    # with its env file, the same record starts as before
    (tmp_path / "db.env").write_text(
        "KOKOAH_SUBNET_DB_PASSWORD=p\nKOKOAH_SUBNET_DB_URL=postgresql+psycopg://kokoah:p@127.0.0.1:48110/kokoah_subnet\n"
        "KOKOAH_SUBNET_DB_READER_PASSWORD=r\n"
        "KOKOAH_SUBNET_DB_READER_URL=postgresql+psycopg://kokoah_reader:r@127.0.0.1:48110/kokoah_subnet\n"
    )
    run = subprocess.run(["bash", str(SCRIPTS / "db.sh"), "start"], env=env, capture_output=True, text=True)
    assert run.returncode == 0 and "PASSWORD 'r'" in (tmp_path / "docker.log").read_text()


def test_an_env_file_that_does_not_log_in_changes_nothing(tmp_path: Path) -> None:
    """A stale env file (its passwords never applied): start stops before the read-only role's password is set, and
    check names the failing login. The login is made from the host, as the validator makes it, with the password in
    the environment only."""
    env = _db_env(tmp_path)
    (tmp_path / "db.env").write_text(
        "KOKOAH_SUBNET_DB_PASSWORD=stale\nKOKOAH_SUBNET_DB_URL=postgresql+psycopg://kokoah:stale@127.0.0.1:48110/kokoah_subnet\n"
        "KOKOAH_SUBNET_DB_READER_PASSWORD=r2\n"
        "KOKOAH_SUBNET_DB_READER_URL=postgresql+psycopg://kokoah_reader:r2@127.0.0.1:48110/kokoah_subnet\n"
    )
    bad = {**env, "FAKE_BAD_LOGIN": "1"}
    for args, says in ((["start"], "is not this database's"), (["check"], "does not log in")):
        run = subprocess.run(["bash", str(SCRIPTS / "db.sh"), *args], env=bad, capture_output=True, text=True)
        assert run.returncode == 1 and says in run.stderr
    sent = (tmp_path / "docker.log").read_text()
    assert "ALTER ROLE" not in sent and "stale" not in sent and "r2" not in sent
    assert (
        "run --rm --network host --userns host -e PGPASSWORD postgres:test psql -h 127.0.0.1 -p 48110 -U kokoah" in sent
    )
    ok = subprocess.run(["bash", str(SCRIPTS / "db.sh"), "check"], env=env, capture_output=True, text=True)
    assert ok.returncode == 0 and "-U kokoah_reader" in (tmp_path / "docker.log").read_text()


def test_a_read_only_role_that_exists_keeps_its_password(tmp_path: Path) -> None:
    """An env file without the read-only role's lines (made before the role existed, or not this database's): start
    adds new ones only when the role is new; a role that exists keeps its password, and the file is left as it was."""
    admin = "KOKOAH_SUBNET_DB_PASSWORD=p\nKOKOAH_SUBNET_DB_URL=postgresql+psycopg://kokoah:p@127.0.0.1:48110/kokoah_subnet\n"
    env = _db_env(tmp_path)
    (tmp_path / "db.env").write_text(admin)
    for extra, says in (
        ({"FAKE_ROLE_EXISTS": "1"}, "exists, but its password is not in"),
        ({"FAKE_ROLE_COUNT": ""}, "exists, but its password is not in"),  # an unclear answer is never "new"
        ({"FAKE_BAD_LOGIN": "1"}, ""),
    ):
        run = subprocess.run(
            ["bash", str(SCRIPTS / "db.sh"), "start"], env={**env, **extra}, capture_output=True, text=True
        )
        assert run.returncode == 1 and says in run.stderr and (tmp_path / "db.env").read_text() == admin
    assert "ALTER ROLE" not in (tmp_path / "docker.log").read_text()
    for args in (["check"], ["url", "reader"]):
        run = subprocess.run(["bash", str(SCRIPTS / "db.sh"), *args], env=env, capture_output=True, text=True)
        assert run.returncode == 1 and "run scripts/db.sh start first" in run.stderr
    # a new role: its lines are written, then its password set
    run = subprocess.run(["bash", str(SCRIPTS / "db.sh"), "start"], env=env, capture_output=True, text=True)
    written = (tmp_path / "db.env").read_text()
    assert run.returncode == 0 and written.startswith(admin) and "KOKOAH_SUBNET_DB_READER_URL=" in written
    reader = next(
        line.split("=", 1)[1] for line in written.splitlines() if line.startswith("KOKOAH_SUBNET_DB_READER_PASSWORD=")
    )
    assert f"PASSWORD '{reader}'" in (tmp_path / "docker.log").read_text()


def test_check_never_makes_an_env_file(tmp_path: Path) -> None:
    run = subprocess.run(
        ["bash", str(SCRIPTS / "db.sh"), "check"], env=_db_env(tmp_path), capture_output=True, text=True
    )
    assert run.returncode == 1 and "no " in run.stderr and not (tmp_path / "db.env").exists()


def test_no_new_passwords_while_docker_does_not_answer(tmp_path: Path) -> None:
    """A user Docker refuses can't tell whether the record exists: no env file is made (it would hold passwords no
    database has, and a later start would reset the read-only role's)."""
    env = {**_db_env(tmp_path), "FAKE_NO_DOCKER": "1"}
    for args in (["start"], ["url"]):
        run = subprocess.run(["bash", str(SCRIPTS / "db.sh"), *args], env=env, capture_output=True, text=True)
        assert run.returncode == 1 and "Docker does not answer" in run.stderr
    assert not (tmp_path / "db.env").exists()


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

    for args in (
        ("kokoah", "kokoah_subnet_agents"),
        ("kokoah_reader", "x"),
        ("kokoah_burner", "x"),
        ("bad-name", "x"),
        ("ok", "bad;db"),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            _run_db(tmp_path, "owner-role", *args)


def test_the_burn_role_adds_burns_and_nothing_else(tmp_path: Path) -> None:
    sent = _run_db(tmp_path, "burn-role")
    assert "ALTER ROLE kokoah_burner WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in sent
    for grant in (
        "GRANT CONNECT ON DATABASE kokoah_subnet TO kokoah_burner;",
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM kokoah_burner;",  # whatever it was given before
        "GRANT SELECT ON meta TO kokoah_burner;",  # the schema version, checked before writing
        "GRANT INSERT, SELECT (id) ON burns TO kokoah_burner;",
    ):
        assert grant in sent
    granted = [line for line in sent.splitlines() if line.startswith("GRANT") and "kokoah_burner" in line]
    assert len(granted) == 4 and not any("ALL TABLES" in line for line in granted)
    assert "REVOKE ALL ON DATABASE %I FROM PUBLIC, kokoah_burner" in sent  # every other database refuses it
    env = (tmp_path / "db.env").read_text()
    assert "KOKOAH_SUBNET_DB_BURNER_URL=postgresql+psycopg://kokoah_burner:" in env
    password = next(line for line in env.splitlines() if line.startswith("KOKOAH_SUBNET_DB_BURNER_PASSWORD="))
    commands = [line for line in sent.splitlines() if line.startswith(("exec ", "ps", "run ", "start "))]
    assert commands and not any(password.split("=", 1)[1] in line for line in commands)  # never an argument
    _run_db(tmp_path, "burn-role")
    assert (tmp_path / "db.env").read_text() == env  # the same password kept
