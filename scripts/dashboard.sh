#!/usr/bin/env bash
# Grafana on the record (Docker, host network, 127.0.0.1:48120), provisioned with the PostgreSQL datasource and the
# dashboards in dashboards/. Admin password generated once into the env file (outside the repo).
#
#   scripts/dashboard.sh start | stop | status | url
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="${KOKOAH_SUBNET_GRAFANA_NAME:-kokoah-subnet-grafana}"
IMAGE="${KOKOAH_SUBNET_GRAFANA_IMAGE:-grafana/grafana-oss:11.6.0}"
PORT="${KOKOAH_SUBNET_GRAFANA_PORT:-48120}"
DB_ENV="${KOKOAH_SUBNET_DB_ENV:-$HOME/.local/share/kokoah/subnet-db.env}"
ENV_FILE="${KOKOAH_SUBNET_GRAFANA_ENV:-$HOME/.local/share/kokoah/subnet-grafana.env}"
PROV="$HOME/.local/share/kokoah/grafana"
DB_PORT="${KOKOAH_SUBNET_DB_PORT:-48110}"

ensure() {
  # shellcheck disable=SC1090
  . "$DB_ENV"
  mkdir -p "$PROV/datasources" "$PROV/dashboards" "$PROV/data"
  if [ ! -f "$ENV_FILE" ]; then
    umask 077
    printf 'GF_SECURITY_ADMIN_PASSWORD=%s\n' "$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)" > "$ENV_FILE"
  fi
  umask 077
  cat > "$PROV/datasources/postgres.yaml" <<YAML
apiVersion: 1
datasources:
  - name: kokoah-subnet
    uid: kokoah-subnet-pg
    type: grafana-postgresql-datasource
    url: 127.0.0.1:$DB_PORT
    user: kokoah
    database: kokoah_subnet
    isDefault: true
    secureJsonData:
      password: "$KOKOAH_SUBNET_DB_PASSWORD"
    jsonData:
      sslmode: disable
      postgresVersion: 1700
YAML
  cat > "$PROV/dashboards/provider.yaml" <<'YAML'
apiVersion: 1
providers:
  - name: kokoah-subnet
    folder: Kokoah subnet
    type: file
    options:
      path: /var/lib/grafana/dashboards
YAML
  cp dashboards/*.json "$PROV/dashboards/"
  # the container runs as uid 472; the provisioning files (one holds the database password) stay private to it
  chown -R 472:472 "$PROV" 2>/dev/null || true
  chmod 700 "$PROV" "$PROV/datasources" "$PROV/dashboards" && chmod 600 "$PROV/datasources/postgres.yaml"
}

case "${1:-status}" in
  start)
    ensure
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then echo "$NAME is already running" >&2; exit 0; fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --restart unless-stopped --network host \
      --env-file "$ENV_FILE" \
      -e GF_SERVER_HTTP_ADDR=127.0.0.1 -e GF_SERVER_HTTP_PORT="$PORT" -e GF_ANALYTICS_REPORTING_ENABLED=false \
      -e GF_USERS_ALLOW_SIGN_UP=false -e GF_SECURITY_ADMIN_USER=admin \
      -v "$PROV/datasources:/etc/grafana/provisioning/datasources:ro" \
      -v "$PROV/dashboards/provider.yaml:/etc/grafana/provisioning/dashboards/provider.yaml:ro" \
      -v "$PROV/dashboards:/var/lib/grafana/dashboards:ro" \
      -v "$PROV/data:/var/lib/grafana" \
      "$IMAGE" >/dev/null
    for _ in $(seq 1 60); do
      if curl -s -o /dev/null "http://127.0.0.1:$PORT/api/health"; then echo "ready on http://127.0.0.1:$PORT (admin password in $ENV_FILE)" >&2; exit 0; fi
      sleep 1
    done
    echo "grafana did not answer within 60 s" >&2; exit 1 ;;
  stop) docker rm -f "$NAME" >/dev/null 2>&1 && echo "stopped $NAME" >&2 || echo "$NAME was not running" >&2 ;;
  status) docker ps -a --filter "name=^$NAME$" --format '{{.Names}} {{.Status}}' ;;
  url) echo "http://127.0.0.1:$PORT" ;;
  *) sed -n 2,6p "$0" ;;
esac
