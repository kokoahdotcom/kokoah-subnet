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
