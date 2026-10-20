#!/usr/bin/env bash
# Burn what the burn hotkey received, every few hours. Usage:
#   KOKOAH_SUBNET_NETUID=<n> [BT_NETWORK=test] [KOKOAH_BURN_WALLET=burn] [KOKOAH_BURN_EVERY_S=21600] scripts/burn_loop.sh
# The coldkey password, if the wallet has one, is read from the file named by KOKOAH_BURN_PASSWORD_FILE (one line).
set -euo pipefail
cd "$(dirname "$0")/.."
NET="${BT_NETWORK:-test}"
NETUID="${KOKOAH_SUBNET_NETUID:?set KOKOAH_SUBNET_NETUID}"
WALLET="${KOKOAH_BURN_WALLET:-burn}"
EVERY="${KOKOAH_BURN_EVERY_S:-21600}"
while true; do
  if [ -n "${KOKOAH_BURN_PASSWORD_FILE:-}" ]; then
    uv run kokoah-subnet owner burn --network "$NET" --netuid "$NETUID" --wallet "$WALLET" --password-stdin < "$KOKOAH_BURN_PASSWORD_FILE" || true
  else
    uv run kokoah-subnet owner burn --network "$NET" --netuid "$NETUID" --wallet "$WALLET" || true
  fi
  sleep "$EVERY"
done
