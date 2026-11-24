#!/usr/bin/env bash
# The validator with automatic updates in one command (docs/validators.md section 6), run from the repository:
#   sudo deploy/validator/setup.sh [--wallet NAME] [--hotkey NAME] [--wallet-path DIR] [--interface]
# Defaults: wallet `validator`, hotkey `default`, in ~/.bittensor/wallets of the user who runs sudo. It makes the
# service user, copies the hotkey and the coldkey's public file (never the coldkey) to /var/lib/kokoah-subnet, starts
# the record's PostgreSQL (scripts/db.sh, keeping an earlier one and its passwords), writes the settings in
# /etc/kokoah-subnet and /opt/kokoah-subnet/docker/.env, then runs install.sh. What is already there is kept, so it
# can be run again; --interface also starts the public interface (section 8).
set -euo pipefail
umask 022

repo=$(cd "$(dirname "$0")/../.." && pwd)
wallet=validator
hotkey=default
wallet_path=
interface=0
usage() {
    echo "usage: sudo deploy/validator/setup.sh [--wallet NAME] [--hotkey NAME] [--wallet-path DIR] [--interface]" >&2
    exit 2
}
while [ $# -gt 0 ]; do
    case "$1" in
        --wallet) [ $# -ge 2 ] || usage; wallet=$2; shift 2 ;;
        --hotkey) [ $# -ge 2 ] || usage; hotkey=$2; shift 2 ;;
        --wallet-path) [ $# -ge 2 ] || usage; wallet_path=$2; shift 2 ;;
        --interface) interface=1; shift ;;
        *) usage ;;
    esac
done
fail() { echo "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "run it with sudo: sudo deploy/validator/setup.sh"
for name in "$wallet" "$hotkey"; do
    [[ "$name" =~ ^[A-Za-z0-9._-]+$ && "$name" != . && "$name" != .. ]] || fail "not a wallet or hotkey name: $name"
done
command -v docker >/dev/null || fail "install Docker Engine 25 or newer first"

# the wallet and an earlier record's settings belong to the user who ran sudo, not to root
owner=${SUDO_USER:-root}
home=$(getent passwd "$owner" | cut -d: -f6)
[ -n "$home" ] || fail "no home directory for $owner"
wallet_path=${wallet_path:-$home/.bittensor/wallets}
key=$wallet_path/$wallet/hotkeys/$hotkey
pub=$wallet_path/$wallet/coldkeypub.txt
dest=/var/lib/kokoah-subnet/wallets/$wallet
# a hotkey copied before (the systemd setup's) is used as it is
[ -f "$dest/hotkeys/$hotkey" ] || [ -f "$key" ] \
    || fail "no hotkey at $key (choose it with --wallet, --hotkey and --wallet-path)"
[ -f "$dest/coldkeypub.txt" ] || [ -f "$pub" ] || fail "no coldkeypub.txt at $pub"

# one validator writes the record: a validator started by hand must be stopped first (the systemd services are
# checked by install.sh); a validator in a container is the Docker setup's own
manual=$({ pgrep -f 'kokoah-subnet validator run' 2>/dev/null || true; } | while read -r pid; do
    grep -qE 'docker|containerd|libpod' "/proc/$pid/cgroup" 2>/dev/null || echo "$pid"
done | tr '\n' ' ')
[ -z "${manual// }" ] || fail "stop the validator you started by hand first (process $manual), then run this again"

# the service user, and the hotkey where only the validator reads it
id -u kokoah-subnet >/dev/null 2>&1 \
    || useradd --system --home-dir /var/lib/kokoah-subnet --create-home --shell /usr/sbin/nologin kokoah-subnet
install -d -o kokoah-subnet -g kokoah-subnet -m 700 /var/lib/kokoah-subnet/data "$dest/hotkeys"
copy() {  # the file, unless it is there already; a different one is never replaced
    if [ -e "$2" ]; then
        [ ! -e "$1" ] || cmp -s "$1" "$2" || fail "$2 holds another key: move it away first if you mean to replace it"
    else
        install -o kokoah-subnet -g kokoah-subnet -m 600 "$1" "$2"
    fi
}
copy "$key" "$dest/hotkeys/$hotkey"
copy "$pub" "$dest/coldkeypub.txt"

# the record: an earlier scripts/db.sh's database and passwords when there are some, else a new one
install -d -m 755 /etc/kokoah-subnet
db_env=/etc/kokoah-subnet/db.env
earlier=$home/.local/share/kokoah/subnet-db.env
[ -f "$db_env" ] || [ ! -f "$earlier" ] || db_env=$earlier
KOKOAH_SUBNET_DB_ENV=$db_env "$repo/scripts/db.sh" start
[ "$db_env" = /etc/kokoah-subnet/db.env ] || install -m 640 "$db_env" /etc/kokoah-subnet/db.env
chgrp kokoah-subnet /etc/kokoah-subnet/db.env && chmod 640 /etc/kokoah-subnet/db.env
grep '^KOKOAH_SUBNET_DB_READER_URL=' /etc/kokoah-subnet/db.env \
    | install -g kokoah-subnet -m 640 /dev/stdin /etc/kokoah-subnet/db-reader.env

# the validator's settings, written once (an edited file is kept): validator-main.env, or the one settings file the
# systemd setup made under another name
name=validator-main.env
if [ -f /opt/kokoah-subnet/docker/.env ]; then
    name=$(sed -n 's/^KOKOAH_VALIDATOR_ENV=//p' /opt/kokoah-subnet/docker/.env | tail -1)
    name=${name:-validator-main.env}
elif [ ! -f /etc/kokoah-subnet/$name ]; then
    found=$(find /etc/kokoah-subnet -maxdepth 1 -name 'validator-*.env' -printf '%f\n')
    [ "$(printf '%s' "$found" | grep -c .)" -le 1 ] || fail "several settings files in /etc/kokoah-subnet: keep one"
    name=${found:-$name}
fi
settings=/etc/kokoah-subnet/$name
if [ ! -f "$settings" ]; then
    printf 'KOKOAH_SUBNET_ARGS=%s\n' "--network finney --netuid 39 \
--builder 0x43ccc0618e4358494f87a4b8699230203dc4ab67 --agent-service https://api.kokoah.com/miners \
--data-dir /var/lib/kokoah-subnet/data --wallet $wallet --wallet-hotkey $hotkey \
--wallet-path /var/lib/kokoah-subnet/wallets" \
        | install -g kokoah-subnet -m 640 /dev/stdin "$settings"
fi
install -d -m 755 /opt/kokoah-subnet/docker
if [ ! -f /opt/kokoah-subnet/docker/.env ]; then
    {
        printf 'KOKOAH_UID=%s\nKOKOAH_GID=%s\nKOKOAH_VALIDATOR_ENV=%s\n' \
            "$(id -u kokoah-subnet)" "$(id -g kokoah-subnet)" "$name"
        [ "$interface" = 0 ] || echo 'COMPOSE_PROFILES=interface'
    } | install -m 644 /dev/stdin /opt/kokoah-subnet/docker/.env
fi

exec "$repo/deploy/validator/install.sh"
