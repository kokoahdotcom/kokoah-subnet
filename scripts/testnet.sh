#!/usr/bin/env bash
# Bittensor testnet steps for this subnet, in order. Test TAO has no value, but nothing here is run for you:
# each step is a command to run when the one before it is done. Wallets are created without a password; keep the
# printed mnemonics somewhere safe anyway, the same wallets may serve on mainnet later.
#
#   scripts/testnet.sh wallets      create owner, validator and miner wallets (prints the owner address to fund)
#   scripts/testnet.sh register     register the subnet (τ1 on testnet), start it, register validator and miner
#   scripts/testnet.sh run          start the validator loop against the testnet
#
# Test TAO: the faucet is off; ask in the Bittensor Discord with the owner coldkey address.
set -euo pipefail
cd "$(dirname "$0")/.."
NET="${BT_NETWORK:-test}"
NETUID="${KOKOAH_SUBNET_NETUID:-}"
BUILDER="${KOKOAH_BUILDER_ADDRESS:-}"
B="uv run btcli"

case "${1:-}" in
  wallets)
    for w in owner validator miner; do
      if [ ! -d "$HOME/.bittensor/wallets/$w" ]; then
        $B wallet create -w "$w" -H default --no-password
      fi
    done
    echo "fund this owner coldkey with test TAO (Bittensor Discord), then run: scripts/testnet.sh register"
    $B wallet list 2>/dev/null | grep -A2 -i owner || true
    ;;
  register)
    $B tx register-subnet -w owner --network "$NET"
    echo "note the netuid printed above, then: KOKOAH_SUBNET_NETUID=<n> scripts/testnet.sh start"
    ;;
  start)
    [ -n "$NETUID" ] || { echo "set KOKOAH_SUBNET_NETUID" >&2; exit 2; }
    $B tx start-call --netuid "$NETUID" -w owner --network "$NET"
    $B tx burned-register --netuid "$NETUID" -w validator --network "$NET"
    $B tx burned-register --netuid "$NETUID" -w miner --network "$NET"
    $B tx add-stake --netuid "$NETUID" --amount-tao 1 --no-slippage-protection -w validator -H default --network "$NET"
    $B tx set-hyperparameter --netuid "$NETUID" --name owner_cut_auto_lock_enabled --value true -w owner --network "$NET" || true
    $B query metagraph --netuid "$NETUID" --network "$NET"
    ;;
  run)
    [ -n "$NETUID" ] && [ -n "$BUILDER" ] || { echo "set KOKOAH_SUBNET_NETUID and KOKOAH_BUILDER_ADDRESS" >&2; exit 2; }
    exec uv run kokoah-subnet validator run --network "$NET" --netuid "$NETUID" --builder "$BUILDER" \
      --data-dir "$HOME/kokoah-subnet-data" --wallet validator
    ;;
  *)
    sed -n 2,11p "$0"
    ;;
esac
