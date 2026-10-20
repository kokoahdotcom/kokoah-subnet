#!/usr/bin/env bash
# Bittensor testnet steps for this subnet, in order. Test TAO has no value, but nothing here is run for you:
# each step is a command to run when the one before it is done. Wallets are created without a password; keep the
# printed mnemonics somewhere safe anyway, the same wallets may serve on mainnet later.
#
#   scripts/testnet.sh wallets      create owner, validator and miner wallets (prints the owner address to fund)
#   scripts/testnet.sh register     register the subnet (τ1 on testnet), start it, register validator and miner
#   scripts/testnet.sh run          start the validator loop against the testnet
#
# Test TAO: the faucet is off; ask in the Bittensor Discord with the coldkey address. Environment overrides:
# KOKOAH_WALLET, KOKOAH_OWNER_HOTKEY, KOKOAH_VALIDATOR_HOTKEY, KOKOAH_MINER_HOTKEY, KOKOAH_SUBNET_DATA_DIR.
set -euo pipefail
cd "$(dirname "$0")/.."
NET="${BT_NETWORK:-test}"
NETUID="${KOKOAH_SUBNET_NETUID:-}"
BUILDER="${KOKOAH_BUILDER_ADDRESS:-}"
# wallet layout: one coldkey wallet with three hotkeys by default (owner/validator/miner roles)
WALLET="${KOKOAH_WALLET:-owner}"
OWNER_HK="${KOKOAH_OWNER_HOTKEY:-default}"
VALIDATOR_HK="${KOKOAH_VALIDATOR_HOTKEY:-validator}"
MINER_HK="${KOKOAH_MINER_HOTKEY:-miner}"
DATA_DIR="${KOKOAH_SUBNET_DATA_DIR:-$HOME/kokoah-subnet-data}"
B="uv run btcli"

case "${1:-}" in
  wallets)
    if [ ! -d "$HOME/.bittensor/wallets/$WALLET" ]; then
      $B wallet create -w "$WALLET" -H "$OWNER_HK" --no-password
    fi
    for h in "$VALIDATOR_HK" "$MINER_HK"; do
      [ -f "$HOME/.bittensor/wallets/$WALLET/hotkeys/$h" ] || $B wallet new-hotkey -w "$WALLET" -H "$h" --no-password
    done
    echo "fund this coldkey with test TAO (Bittensor Discord), then run: scripts/testnet.sh register"
    $B wallet list 2>/dev/null | grep -A1 "$WALLET" || true
    ;;
  register)
    $B tx register-subnet -w "$WALLET" -H "$OWNER_HK" --network "$NET"
    echo "note the netuid printed above, then: KOKOAH_SUBNET_NETUID=<n> scripts/testnet.sh start"
    ;;
  start)
    [ -n "$NETUID" ] || { echo "set KOKOAH_SUBNET_NETUID" >&2; exit 2; }
    $B tx start-call --netuid "$NETUID" -w "$WALLET" --network "$NET"
    $B tx burned-register --netuid "$NETUID" -w "$WALLET" -H "$VALIDATOR_HK" --network "$NET"
    $B tx burned-register --netuid "$NETUID" -w "$WALLET" -H "$MINER_HK" --network "$NET"
    $B tx add-stake --netuid "$NETUID" --amount-tao 1 --no-slippage-protection -w "$WALLET" -H "$VALIDATOR_HK" --network "$NET"
    $B tx set-hyperparameter --netuid "$NETUID" --name owner_cut_auto_lock_enabled --value true -w "$WALLET" --network "$NET" || true
    $B query metagraph --netuid "$NETUID" --network "$NET"
    ;;
  run)
    [ -n "$NETUID" ] && [ -n "$BUILDER" ] || { echo "set KOKOAH_SUBNET_NETUID and KOKOAH_BUILDER_ADDRESS" >&2; exit 2; }
    exec uv run kokoah-subnet validator run --network "$NET" --netuid "$NETUID" --builder "$BUILDER" \
      --data-dir "$DATA_DIR" --wallet "$WALLET" --wallet-hotkey "$VALIDATOR_HK"
    ;;
  *)
    sed -n 2,11p "$0"
    ;;
esac
