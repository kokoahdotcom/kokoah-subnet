#!/usr/bin/env bash
# First start of the validator's Docker setup with automatic updates (docs/validators.md section 6). Checks that the
# published image was signed by the release workflow on main (cosign, from its own pinned image), then hands over to
# that image, which installs itself (`kokoah-subnet validator update setup`, which checks the signature again) and
# starts the validator, the health check, the interface when COMPOSE_PROFILES asks for it, and the updater.
#   sudo deploy/validator/install.sh
set -euo pipefail
umask 022

image=docker.io/kokoahdotcom/kokoah-subnet
identity=https://github.com/kokoahdotcom/kokoah-subnet/.github/workflows/release.yml@refs/heads/main
issuer=https://token.actions.githubusercontent.com
cosign=ghcr.io/sigstore/cosign/cosign:v2.6.5@sha256:ad281047f85c5e1fc6ffbc30c2b55be3b07b4032bef715a12122ce5829619aca
dir=/opt/kokoah-subnet/docker

[ -f "$dir/.env" ] || { echo "write $dir/.env first (docs/validators.md section 6)" >&2; exit 1; }
active=$(systemctl list-units --state=active --no-legend --plain 'kokoah-subnet-validator@*' \
    kokoah-subnet-api.service kokoah-subnet-check.timer 2>/dev/null | awk '{print $1}' || true)
if [ -n "$active" ]; then
    echo "stop and disable the systemd services first, so that one validator writes the record:" >&2
    echo "  sudo systemctl disable --now $(echo "$active" | tr '\n' ' ')" >&2
    exit 1
fi

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

docker pull --quiet "$image@$digest" >/dev/null
docker run --rm --user 0:0 --read-only --tmpfs /tmp -e HOME=/tmp -e DOCKER_CONFIG=/tmp/docker \
    -v /var/run/docker.sock:/var/run/docker.sock -v "$dir:$dir" -v /etc/kokoah-subnet:/etc/kokoah-subnet:ro \
    "$image@$digest" validator update setup --digest "$digest" --project-dir "$dir"
echo "installed: docker compose --project-directory $dir ps" >&2
