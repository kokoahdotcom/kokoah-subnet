#!/usr/bin/env bash
# The validator with automatic updates in one command (docs/validators.md section 6), run from the repository:
#   sudo deploy/validator/setup.sh [--wallet NAME] [--hotkey NAME] [--wallet-path DIR] [--interface]
# Defaults: wallet `validator`, hotkey `default`, in ~/.bittensor/wallets of the user who runs sudo. It makes the
# service user, copies the hotkey and the coldkey's public file (never the coldkey) to /var/lib/kokoah-subnet, starts
# the record's PostgreSQL (scripts/db.sh, keeping an earlier one and its passwords), writes the settings in
# /etc/kokoah-subnet and /opt/kokoah-subnet/docker/.env, installs the daily backup (section 9), then runs install.sh.
# What is already there is kept, so it can be run again; --interface also starts the public interface (section 8).
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
for tool in docker pgrep runuser; do
    command -v "$tool" >/dev/null \
        || fail "$tool is needed (Docker Engine 25 or newer; pgrep and runuser come with procps and util-linux)"
done

# the wallet and an earlier record's settings belong to the user who ran sudo, not to root
owner=${SUDO_USER:-root}
home=$(getent passwd "$owner" | cut -d: -f6 || true)
[ -n "$home" ] || fail "no home directory found for $owner"
wallet_path=${wallet_path:-$home/.bittensor/wallets}
key=$wallet_path/$wallet/hotkeys/$hotkey
pub=$wallet_path/$wallet/coldkeypub.txt
dest=/var/lib/kokoah-subnet/wallets/$wallet
envf=/opt/kokoah-subnet/docker/.env

# the validator's settings file: the .env's, else validator-main.env, else the one the systemd setup made
name=validator-main.env
if [ -f "$envf" ]; then
    name=$(sed -n 's/^KOKOAH_VALIDATOR_ENV=//p' "$envf" | tail -1)
    name=${name:-validator-main.env}
elif [ ! -f "/etc/kokoah-subnet/$name" ] && [ -d /etc/kokoah-subnet ]; then
    found=$(find /etc/kokoah-subnet -maxdepth 1 -name 'validator-*.env' -printf '%f\n')
    [ "$(printf '%s' "$found" | grep -c .)" -le 1 ] || fail "several settings files in /etc/kokoah-subnet: keep one"
    name=${found:-$name}
fi
settings=/etc/kokoah-subnet/$name
# kept settings run one wallet and hotkey: another one given here is refused, never half applied
if [ -f "$settings" ]; then
    kept_wallet=$(sed -n 's/.*--wallet[ =]\([^ ]*\).*/\1/p' "$settings" | tail -1)
    kept_hotkey=$(sed -n 's/.*--wallet-hotkey[ =]\([^ ]*\).*/\1/p' "$settings" | tail -1)
    if [ "${kept_wallet:-$wallet}" != "$wallet" ] || [ "${kept_hotkey:-$hotkey}" != "$hotkey" ]; then
        fail "$settings runs --wallet ${kept_wallet:-$wallet} --wallet-hotkey ${kept_hotkey:-$hotkey}: run this with" \
            "--wallet ${kept_wallet:-$wallet} --hotkey ${kept_hotkey:-$hotkey}, or change that file first"
    fi
fi

# a hotkey copied before (the systemd setup's) is used as it is
[ -f "$dest/hotkeys/$hotkey" ] || [ -f "$key" ] \
    || fail "no hotkey at $key (choose it with --wallet, --hotkey and --wallet-path)"
[ -f "$dest/coldkeypub.txt" ] || [ -f "$pub" ] || fail "no coldkeypub.txt at $pub"

# one validator writes the record: any other validator must be stopped first; a validator in a container is the
# Docker setup's own (install.sh checks the systemd services once more)
others=$({ pgrep -f 'kokoah-subnet validator run' 2>/dev/null || true; } | while read -r pid; do
    grep -qE 'docker|containerd|libpod' "/proc/$pid/cgroup" 2>/dev/null || echo "$pid"
done | tr '\n' ' ')
[ -z "${others// }" ] || fail "stop the validator that runs outside Docker first (process $others; a systemd one:" \
    "sudo systemctl disable --now 'kokoah-subnet-validator@*'), then run this again"

# the service user. Its home is its own to write, so everything in it is made as that user, never as root: a link
# put there then leads nowhere that user could not write anyway
id -u kokoah-subnet >/dev/null 2>&1 \
    || useradd --system --home-dir /var/lib/kokoah-subnet --create-home --shell /usr/sbin/nologin kokoah-subnet
[ -d /var/lib/kokoah-subnet ] || install -d -o kokoah-subnet -g kokoah-subnet -m 750 /var/lib/kokoah-subnet
as_service() { runuser -u kokoah-subnet -- "$@"; }
as_service sh -c 'umask 077 && mkdir -p "$1" "$2"' sh /var/lib/kokoah-subnet/data "$dest/hotkeys"
copy() {  # $1 read as root, $2 written as the service user; a file already there is kept, a different one refused
    if [ -L "$2" ] || { [ -e "$2" ] && [ ! -f "$2" ]; }; then
        fail "$2 is not a plain file: move it away first"
    elif [ -e "$2" ]; then
        [ ! -e "$1" ] || cmp -s "$1" "$2" || fail "$2 holds another key: move it away first if you mean to replace it"
    else
        as_service sh -c 'umask 077 && cat > "$1"' sh "$2" < "$1"
    fi
}
copy "$key" "$dest/hotkeys/$hotkey"
copy "$pub" "$dest/coldkeypub.txt"

# the record: an earlier scripts/db.sh's database and passwords when there are some (made as the user or with sudo),
# else a new one; db.sh refuses to make new passwords for a database that exists
install -d -m 755 /etc/kokoah-subnet
db_env=/etc/kokoah-subnet/db.env
if [ ! -f "$db_env" ]; then
    for earlier in "$home/.local/share/kokoah/subnet-db.env" /root/.local/share/kokoah/subnet-db.env; do
        if [ -f "$earlier" ]; then db_env=$earlier; break; fi
    done
fi
KOKOAH_SUBNET_DB_ENV=$db_env "$repo/scripts/db.sh" start
[ "$db_env" = /etc/kokoah-subnet/db.env ] || install -m 640 "$db_env" /etc/kokoah-subnet/db.env
chgrp kokoah-subnet /etc/kokoah-subnet/db.env && chmod 640 /etc/kokoah-subnet/db.env
grep '^KOKOAH_SUBNET_DB_READER_URL=' /etc/kokoah-subnet/db.env \
    | install -g kokoah-subnet -m 640 /dev/stdin /etc/kokoah-subnet/db-reader.env
# both logins must work before anything uses them (over TCP: the container's own socket needs no password)
url_password() { sed -n "s#^$1=[^:]*://[^:]*:\([^@]*\)@.*#\1#p" /etc/kokoah-subnet/db.env | tail -1; }
logs_in() {
    PGPASSWORD=$2 docker exec -e PGPASSWORD kokoah-subnet-postgres \
        psql -h 127.0.0.1 -U "$1" -d kokoah_subnet -tAc 'SELECT 1' 2>/dev/null | grep -qx 1
}
for login in KOKOAH_SUBNET_DB_URL:kokoah KOKOAH_SUBNET_DB_READER_URL:kokoah_reader; do
    logs_in "${login#*:}" "$(url_password "${login%%:*}")" \
        || fail "${login#*:}'s password in /etc/kokoah-subnet/db.env does not log in to the record: put the file" \
            "that holds the database's real passwords there, then run this again"
done

# the validator's settings, written once (an edited file is kept)
if [ ! -f "$settings" ]; then
    printf 'KOKOAH_SUBNET_ARGS=%s\n' "--network finney --netuid 39 \
--builder 0x43ccc0618e4358494f87a4b8699230203dc4ab67 --agent-service https://api.kokoah.com/miners \
--data-dir /var/lib/kokoah-subnet/data --wallet $wallet --wallet-hotkey $hotkey \
--wallet-path /var/lib/kokoah-subnet/wallets" \
        | install -g kokoah-subnet -m 640 /dev/stdin "$settings"
fi
install -d -m 755 /opt/kokoah-subnet/docker
if [ ! -f "$envf" ]; then
    printf 'KOKOAH_UID=%s\nKOKOAH_GID=%s\nKOKOAH_VALIDATOR_ENV=%s\n' \
        "$(id -u kokoah-subnet)" "$(id -g kokoah-subnet)" "$name" | install -m 644 /dev/stdin "$envf"
fi
if [ "$interface" = 1 ] && ! grep -q '^COMPOSE_PROFILES=.*interface' "$envf"; then
    ! grep -q '^COMPOSE_PROFILES=' "$envf" || fail "add interface to the COMPOSE_PROFILES line of $envf"
    echo 'COMPOSE_PROFILES=interface' >> "$envf"
fi

# the daily backup of the record (section 9)
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
    install -d -m 755 /opt/kokoah-subnet/scripts
    install -m 755 "$repo/scripts/db.sh" "$repo/scripts/backup.sh" /opt/kokoah-subnet/scripts/
    install -m 644 "$repo/deploy/systemd/kokoah-subnet-backup.service" \
        "$repo/deploy/systemd/kokoah-subnet-backup.timer" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now kokoah-subnet-backup.timer
else
    echo "no systemd here: set up the daily backup of section 9 yourself" >&2
fi

exec "$repo/deploy/validator/install.sh"
