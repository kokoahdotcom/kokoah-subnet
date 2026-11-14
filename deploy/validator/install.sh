#!/usr/bin/env bash
# First start of the validator's Docker setup with automatic updates (docs/validators.md section 6). Checks that the
# published image was signed by the release workflow on main (cosign, from its own pinned image), then hands over to
# that image, which installs itself (`kokoah-subnet validator update setup`, which checks the signature again) and
# starts the validator, the health check, the interface when COMPOSE_PROFILES asks for it, and the updater.
#   sudo deploy/validator/install.sh [--allow-older]   (--allow-older: go back to an older release on purpose)
set -euo pipefail
umask 022

image=docker.io/kokoahdotcom/kokoah-subnet
identity=https://github.com/kokoahdotcom/kokoah-subnet/.github/workflows/release.yml@refs/heads/main
issuer=https://token.actions.githubusercontent.com
cosign=ghcr.io/sigstore/cosign/cosign:v2.6.5@sha256:ad281047f85c5e1fc6ffbc30c2b55be3b07b4032bef715a12122ce5829619aca
dir=${KOKOAH_DOCKER_DIR:-/opt/kokoah-subnet/docker}
systemd=${KOKOAH_SYSTEMD_DIR:-/etc/systemd/system}

older=()
for arg in "$@"; do
    case "$arg" in
        --allow-older) older=(--allow-older) ;;
        *) echo "usage: install.sh [--allow-older]" >&2; exit 2 ;;
    esac
done

[ -f "$dir/.env" ] || { echo "write $dir/.env first (docs/validators.md section 6)" >&2; exit 1; }

# One validator writes the record: refuse while a systemd validator, interface or check runs or is starting, or is
# enabled (it would start again at the next boot). A unit left failed and disabled starts no more.
busy=$({
    systemctl list-units --all --no-legend --plain 'kokoah-subnet-validator@*' kokoah-subnet-api.service \
        kokoah-subnet-check.service kokoah-subnet-check.timer 2>/dev/null \
        | awk '$3 != "inactive" && $3 != "failed" {print $1}' || true
    find "$systemd" -path '*.wants/*' \( -name 'kokoah-subnet-validator@*.service' -o -name kokoah-subnet-api.service \
        -o -name kokoah-subnet-check.timer \) -printf '%f\n' 2>/dev/null || true
} | sort -u | tr '\n' ' ')
if [ -n "${busy// }" ]; then
    echo "stop and disable the systemd services first, so that one validator writes the record:" >&2
    echo "  sudo systemctl disable --now $busy" >&2
    exit 1
fi

# the compose file's health check timing needs Docker Engine 25 (API 1.44)
api=$(docker version --format '{{.Server.APIVersion}}')
printf '1.44\n%s\n' "$api" | sort -V -C || { echo "Docker Engine 25 or newer is needed (API $api)" >&2; exit 1; }

# public images, pulled without any stored login
DOCKER_CONFIG=$(mktemp -d)
export DOCKER_CONFIG
trap 'rm -rf "$DOCKER_CONFIG"' EXIT

docker pull --quiet "$cosign" >/dev/null
digest=$(docker run --rm "$cosign" verify "$image:latest" \
    --certificate-identity "$identity" --certificate-oidc-issuer "$issuer" 2>/dev/null \
    | grep -o '"docker-manifest-digest":"sha256:[0-9a-f]\{64\}"' | head -1 | cut -d'"' -f4 || true)
[ -n "$digest" ] || { echo "$image:latest carries no signature of the release workflow: not installed" >&2; exit 1; }
echo "verified $image@$digest" >&2

# a running updater of an earlier install waits meanwhile (one installs at a time) and runs again whatever happens;
# the setup itself starts the updater when it is done
if [ "$(docker inspect --type container --format '{{.State.Running}}' kokoah-subnet-updater 2>/dev/null)" = true ]; then
    docker stop kokoah-subnet-updater >/dev/null
    trap 'rm -rf "$DOCKER_CONFIG"; docker start kokoah-subnet-updater >/dev/null 2>&1 || true' EXIT
fi
docker pull --quiet "$image@$digest" >/dev/null
docker run --rm --user 0:0 --read-only --tmpfs /tmp -e HOME=/tmp -e DOCKER_CONFIG=/tmp/docker \
    -v /var/run/docker.sock:/var/run/docker.sock -v "$dir:$dir" -v /etc/kokoah-subnet:/etc/kokoah-subnet:ro \
    "$image@$digest" validator update setup --digest "$digest" --project-dir "$dir" "${older[@]}"
echo "installed: sudo docker compose --project-directory $dir ps" >&2
