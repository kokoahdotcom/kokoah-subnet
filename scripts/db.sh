#!/usr/bin/env bash
# The subnet's own PostgreSQL (Docker), the record of everything the validator sees and decides.
#
#   scripts/db.sh start      start (or create) the container and its read-only role; writes both URLs to the env file
#   scripts/db.sh stop       stop the container (data stays in the volume)
#   scripts/db.sh status     container state
#   scripts/db.sh psql       open psql on the database
#   scripts/db.sh url        print the connection URL (from the env file); `url reader` the read-only one
#   scripts/db.sh backup F [DB]   pg_dump (custom format) of the database (default kokoah_subnet) to file F
#   scripts/db.sh create-db DB    create another database in the same container (same user), if missing
#   scripts/db.sh owner-role ROLE DB   a login role that owns database DB and its tables, and nothing else: for a
#                                 service that must not connect as the container's superuser (the agent service);
#                                 its URL goes in the env file as KOKOAH_SUBNET_ROLE_<ROLE>_URL
#   scripts/db.sh burn-role       the burn's login (kokoah_burner): adds rows to the record's burns table and reads
#                                 its schema version, nothing else; run once the validator has created the record;
#                                 its URL goes in the env file as KOKOAH_SUBNET_DB_BURNER_URL
#
# Listens on 127.0.0.1:48110 only. The passwords are generated once and kept in the env file (outside the repo):
# $KOKOAH_SUBNET_DB_ENV, default ~/.local/share/kokoah/subnet-db.env, as KOKOAH_SUBNET_DB_URL=... for the validator
# and KOKOAH_SUBNET_DB_READER_URL=... (role kokoah_reader, read-only) for the interface, the health check and Grafana.
set -euo pipefail

NAME="${KOKOAH_SUBNET_DB_NAME:-kokoah-subnet-postgres}"
IMAGE="${KOKOAH_SUBNET_DB_IMAGE:-postgres:17.6@sha256:00bc86618629af00d2937fdc5a5d63db3ff8450acf52f0636ec813c7f4902929}"
VOLUME="${KOKOAH_SUBNET_DB_VOLUME:-kokoah-subnet-postgres-data}"
PORT="${KOKOAH_SUBNET_DB_PORT:-48110}"
ENV_FILE="${KOKOAH_SUBNET_DB_ENV:-$HOME/.local/share/kokoah/subnet-db.env}"
DB="kokoah_subnet"
USER="kokoah"

READER="kokoah_reader"
BURNER="kokoah_burner"

new_password() { head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32; }

ensure_env() {
  mkdir -p "$(dirname "$ENV_FILE")"
  umask 077
  if [ ! -f "$ENV_FILE" ]; then
    pw="$(new_password)"
    printf 'KOKOAH_SUBNET_DB_PASSWORD=%s\nKOKOAH_SUBNET_DB_URL=postgresql+psycopg://%s:%s@127.0.0.1:%s/%s\n' "$pw" "$USER" "$pw" "$PORT" "$DB" > "$ENV_FILE"
  fi
  if ! grep -q '^KOKOAH_SUBNET_DB_READER_URL=' "$ENV_FILE"; then
    pw="$(new_password)"
    printf 'KOKOAH_SUBNET_DB_READER_PASSWORD=%s\nKOKOAH_SUBNET_DB_READER_URL=postgresql+psycopg://%s:%s@127.0.0.1:%s/%s\n' "$pw" "$READER" "$pw" "$PORT" "$DB" >> "$ENV_FILE"
  fi
  # shellcheck disable=SC1090
  . "$ENV_FILE"
}

# the read-only role: reads the validator's tables in $DB and nothing else (no other database in the container, such as
# the agent service's, accepts it), can change nothing (the password goes in on stdin, never as an argument)
ensure_reader() {
  docker exec -i "$NAME" psql -U "$USER" -d "$DB" -v ON_ERROR_STOP=1 -q >/dev/null <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$READER') THEN CREATE ROLE $READER LOGIN; END IF;
END \$\$;
ALTER ROLE $READER WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$KOKOAH_SUBNET_DB_READER_PASSWORD';
DO \$\$ BEGIN
  IF EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member
             WHERE r.rolname = 'pg_read_all_data' AND u.rolname = '$READER') THEN
    REVOKE pg_read_all_data FROM $READER;
  END IF;
END \$\$;
DO \$\$ DECLARE d text; BEGIN
  FOR d IN SELECT datname FROM pg_database WHERE datallowconn AND datname <> '$DB' LOOP
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC, $READER', d);
  END LOOP;
END \$\$;
REVOKE ALL ON DATABASE $DB FROM PUBLIC;
GRANT CONNECT ON DATABASE $DB TO $READER;
GRANT USAGE ON SCHEMA public TO $READER;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO $READER;
ALTER DEFAULT PRIVILEGES FOR ROLE $USER IN SCHEMA public GRANT SELECT ON TABLES TO $READER;
SQL
}

case "${1:-status}" in
  start)
    ensure_env
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then ensure_reader; echo "$NAME is already running" >&2; exit 0; fi
    if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then docker start "$NAME" >/dev/null; else
      docker image inspect "$IMAGE" >/dev/null 2>&1 || { anon="$(mktemp -d)"; DOCKER_CONFIG="$anon" docker pull "$IMAGE" >&2; rm -rf "$anon"; }
      docker volume create "$VOLUME" >/dev/null
      docker run -d --name "$NAME" --restart unless-stopped \
        -p "127.0.0.1:$PORT:5432" -v "$VOLUME:/var/lib/postgresql/data" \
        -e POSTGRES_DB="$DB" -e POSTGRES_USER="$USER" -e POSTGRES_PASSWORD="$KOKOAH_SUBNET_DB_PASSWORD" \
        "$IMAGE" >/dev/null
    fi
    for _ in $(seq 1 60); do
      # over TCP: the image's first-start init server listens only on its socket, so this waits for the real one
      if docker exec "$NAME" pg_isready -h 127.0.0.1 -U "$USER" -d "$DB" >/dev/null 2>&1; then ensure_reader; echo "ready on 127.0.0.1:$PORT (URLs in $ENV_FILE)" >&2; exit 0; fi
      sleep 1
    done
    echo "the database did not answer within 60 s" >&2; exit 1 ;;
  stop) docker stop "$NAME" >/dev/null 2>&1 && echo "stopped $NAME" >&2 || echo "$NAME was not running" >&2 ;;
  status) docker ps -a --filter "name=^$NAME$" --format '{{.Names}} {{.Status}}' ;;
  psql) docker exec -it "$NAME" psql -U "$USER" -d "$DB" ;;
  url) ensure_env; if [ "${2:-}" = reader ]; then echo "$KOKOAH_SUBNET_DB_READER_URL"; else echo "$KOKOAH_SUBNET_DB_URL"; fi ;;
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
    fi
    # only its owner connects: not PUBLIC, so not the read-only role either
    docker exec "$NAME" psql -U "$USER" -d "$DB" -qc "REVOKE ALL ON DATABASE $new FROM PUBLIC" ;;
  owner-role)
    ensure_env; role="${2:?role name}"; db="${3:?database name}"
    [[ "$role" =~ ^[a-z_][a-z0-9_]*$ && "$db" =~ ^[a-z_][a-z0-9_]*$ ]] || { echo "bad role or database name" >&2; exit 1; }
    [ "$role" != "$USER" ] && [ "$role" != "$READER" ] && [ "$role" != "$BURNER" ] || { echo "$role is taken" >&2; exit 1; }
    key="KOKOAH_SUBNET_ROLE_${role^^}"
    if ! grep -q "^${key}_URL=" "$ENV_FILE"; then
      pw="$(new_password)"
      printf '%s_PASSWORD=%s\n%s_URL=postgresql+psycopg://%s:%s@127.0.0.1:%s/%s\n' "$key" "$pw" "$key" "$role" "$pw" "$PORT" "$db" >> "$ENV_FILE"
      # shellcheck disable=SC1090
      . "$ENV_FILE"
    fi
    pwvar="${key}_PASSWORD"
    # the password goes in on stdin, never as an argument; the role may create tables in its database (the service
    # creates its own at start) and owns those already there
    docker exec -i "$NAME" psql -U "$USER" -d "$db" -v ON_ERROR_STOP=1 -q >/dev/null <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$role') THEN CREATE ROLE $role LOGIN; END IF;
END \$\$;
ALTER ROLE $role WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '${!pwvar}';
ALTER DATABASE $db OWNER TO $role;
REVOKE ALL ON DATABASE $db FROM PUBLIC;
DO \$\$ DECLARE t text; BEGIN
  FOR t IN SELECT format('%I.%I', schemaname, tablename) FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE 'ALTER TABLE ' || t || ' OWNER TO $role';
  END LOOP;
END \$\$;
DO \$\$ DECLARE d text; BEGIN
  FOR d IN SELECT datname FROM pg_database WHERE datallowconn AND datname <> '$db' LOOP
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC, $role', d);
  END LOOP;
END \$\$;
SQL
    echo "$role owns $db (URL in $ENV_FILE as ${key}_URL)" >&2 ;;
  burn-role)
    ensure_env
    if ! grep -q '^KOKOAH_SUBNET_DB_BURNER_URL=' "$ENV_FILE"; then
      pw="$(new_password)"
      printf 'KOKOAH_SUBNET_DB_BURNER_PASSWORD=%s\nKOKOAH_SUBNET_DB_BURNER_URL=postgresql+psycopg://%s:%s@127.0.0.1:%s/%s\n' "$pw" "$BURNER" "$pw" "$PORT" "$DB" >> "$ENV_FILE"
      # shellcheck disable=SC1090
      . "$ENV_FILE"
    fi
    # the password goes in on stdin, never as an argument; the inserted row's id comes back to the burn (RETURNING),
    # so that one column is readable too
    docker exec -i "$NAME" psql -U "$USER" -d "$DB" -v ON_ERROR_STOP=1 -q >/dev/null <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$BURNER') THEN CREATE ROLE $BURNER LOGIN; END IF;
END \$\$;
ALTER ROLE $BURNER WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '$KOKOAH_SUBNET_DB_BURNER_PASSWORD';
DO \$\$ DECLARE d text; BEGIN
  FOR d IN SELECT datname FROM pg_database WHERE datallowconn AND datname <> '$DB' LOOP
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC, $BURNER', d);
  END LOOP;
END \$\$;
REVOKE ALL ON DATABASE $DB FROM PUBLIC;
GRANT CONNECT ON DATABASE $DB TO $BURNER;
GRANT USAGE ON SCHEMA public TO $BURNER;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM $BURNER;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM $BURNER;
GRANT SELECT ON meta TO $BURNER;
GRANT INSERT, SELECT (id) ON burns TO $BURNER;
DO \$\$ BEGIN
  EXECUTE format('GRANT USAGE ON SEQUENCE %s TO $BURNER', pg_get_serial_sequence('public.burns', 'id'));
END \$\$;
SQL
    echo "$BURNER adds burns to $DB (URL in $ENV_FILE as KOKOAH_SUBNET_DB_BURNER_URL)" >&2 ;;
  *) sed -n 2,17p "$0" ;;
esac
