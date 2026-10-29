#!/usr/bin/env bash
# The subnet's own PostgreSQL (Docker), the record of everything the validator sees and decides.
#
#   scripts/db.sh start      start (or create) the container; writes the connection URL to the env file
#   scripts/db.sh stop       stop the container (data stays in the volume)
#   scripts/db.sh status     container state
#   scripts/db.sh psql       open psql on the database
#   scripts/db.sh url        print the connection URL (from the env file)
#   scripts/db.sh backup F [DB]   pg_dump (custom format) of the database (default kokoah_subnet) to file F
#   scripts/db.sh create-db DB    create another database in the same container (same user), if missing
#
# Listens on 127.0.0.1:48110 only. The password is generated once and kept in the env file (outside the repo):
# $KOKOAH_SUBNET_DB_ENV, default ~/.local/share/kokoah/subnet-db.env, as KOKOAH_SUBNET_DB_URL=... for the validator.
set -euo pipefail

NAME="${KOKOAH_SUBNET_DB_NAME:-kokoah-subnet-postgres}"
IMAGE="${KOKOAH_SUBNET_DB_IMAGE:-postgres:17.6@sha256:00bc86618629af00d2937fdc5a5d63db3ff8450acf52f0636ec813c7f4902929}"
VOLUME="${KOKOAH_SUBNET_DB_VOLUME:-kokoah-subnet-postgres-data}"
PORT="${KOKOAH_SUBNET_DB_PORT:-48110}"
ENV_FILE="${KOKOAH_SUBNET_DB_ENV:-$HOME/.local/share/kokoah/subnet-db.env}"
DB="kokoah_subnet"
USER="kokoah"

ensure_env() {
  mkdir -p "$(dirname "$ENV_FILE")"
  if [ ! -f "$ENV_FILE" ]; then
    pw="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
    umask 077
    printf 'KOKOAH_SUBNET_DB_PASSWORD=%s\nKOKOAH_SUBNET_DB_URL=postgresql+psycopg://%s:%s@127.0.0.1:%s/%s\n' "$pw" "$USER" "$pw" "$PORT" "$DB" > "$ENV_FILE"
  fi
  # shellcheck disable=SC1090
  . "$ENV_FILE"
}

case "${1:-status}" in
  start)
    ensure_env
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then echo "$NAME is already running" >&2; exit 0; fi
    if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then docker start "$NAME" >/dev/null; else
      docker image inspect "$IMAGE" >/dev/null 2>&1 || { anon="$(mktemp -d)"; DOCKER_CONFIG="$anon" docker pull "$IMAGE" >&2; rm -rf "$anon"; }
      docker volume create "$VOLUME" >/dev/null
      docker run -d --name "$NAME" --restart unless-stopped \
        -p "127.0.0.1:$PORT:5432" -v "$VOLUME:/var/lib/postgresql/data" \
        -e POSTGRES_DB="$DB" -e POSTGRES_USER="$USER" -e POSTGRES_PASSWORD="$KOKOAH_SUBNET_DB_PASSWORD" \
        "$IMAGE" >/dev/null
    fi
    for _ in $(seq 1 60); do
      if docker exec "$NAME" pg_isready -U "$USER" -d "$DB" >/dev/null 2>&1; then echo "ready on 127.0.0.1:$PORT (URL in $ENV_FILE)" >&2; exit 0; fi
      sleep 1
    done
    echo "the database did not answer within 60 s" >&2; exit 1 ;;
  stop) docker stop "$NAME" >/dev/null 2>&1 && echo "stopped $NAME" >&2 || echo "$NAME was not running" >&2 ;;
  status) docker ps -a --filter "name=^$NAME$" --format '{{.Names}} {{.Status}}' ;;
  psql) docker exec -it "$NAME" psql -U "$USER" -d "$DB" ;;
  url) ensure_env; echo "$KOKOAH_SUBNET_DB_URL" ;;
  backup)
    # written beside the target and renamed once complete, so a failed dump never replaces or poses as a good one
    out="${2:?output file}"; tmp="$out.tmp.$$"
    trap 'rm -f "$tmp"' EXIT
    docker exec "$NAME" pg_dump -U "$USER" -d "${3:-$DB}" -Fc > "$tmp"
    mv -f "$tmp" "$out"; trap - EXIT
    echo "wrote $out" >&2 ;;
  create-db)
    ensure_env; new="${2:?database name}"
    [[ "$new" =~ ^[a-z_][a-z0-9_]*$ ]] || { echo "bad database name" >&2; exit 1; }
    if docker exec "$NAME" psql -U "$USER" -d "$DB" -tAc "SELECT 1 FROM pg_database WHERE datname = '$new'" | grep -q 1; then
      echo "$new exists" >&2
    else
      docker exec "$NAME" psql -U "$USER" -d "$DB" -qc "CREATE DATABASE $new" && echo "created $new" >&2
    fi ;;
  *) sed -n 2,12p "$0" ;;
esac
