#!/usr/bin/env bash
# Start, stop or inspect the local Bittensor chain used for development (Docker, fast 0.25 s blocks by default).
#
#   scripts/localnet.sh start [--slow] [--keep]   start the chain (--slow: 12 s blocks; --keep: keep chain state)
#   scripts/localnet.sh stop                      stop and remove the container
#   scripts/localnet.sh status                    container state and a chain probe
#   scripts/localnet.sh logs                      follow the node logs
#
# The chain listens on ws://127.0.0.1:9944 (`--network local` in btcli and the SDK). Alice is pre-funded with
# 1,000,000 test TAO; subnets 0 and 1 exist at genesis. Everything on it is throwaway.
set -euo pipefail

NAME="${KOKOAH_SUBNET_LOCALNET_NAME:-kokoah-subnet-localnet}"
IMAGE="${KOKOAH_SUBNET_LOCALNET_IMAGE:-ghcr.io/raofoundation/subtensor-localnet:devnet}"
VOLUME="${KOKOAH_SUBNET_LOCALNET_VOLUME:-kokoah-subnet-localnet-data}"

cmd="${1:-status}"
shift || true

case "$cmd" in
  start)
    fast=True
    keep=""
    for arg in "$@"; do
      case "$arg" in
        --slow) fast=False ;;
        --keep) keep="--no-purge" ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
      esac
    done
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      echo "$NAME is already running" >&2
      exit 0
    fi
    docker volume create "$VOLUME" >/dev/null
    # shellcheck disable=SC2086
    docker run -d --rm --name "$NAME" \
      -p 127.0.0.1:9944:9944 -p 127.0.0.1:9945:9945 \
      -v "$VOLUME:/tmp" \
      "$IMAGE" "$fast" $keep >/dev/null
    echo "started $NAME ($IMAGE, fast_blocks=$fast${keep:+, state kept}); waiting for the RPC" >&2
    for _ in $(seq 1 60); do
      if curl -s -o /dev/null -H 'Content-Type: application/json' \
        --data '{"jsonrpc":"2.0","id":1,"method":"system_health","params":[]}' http://127.0.0.1:9944; then
        echo "ready on ws://127.0.0.1:9944" >&2
        exit 0
      fi
      sleep 1
    done
    echo "the chain did not answer within 60 s; see: scripts/localnet.sh logs" >&2
    exit 1
    ;;
  stop)
    docker rm -f "$NAME" >/dev/null 2>&1 && echo "stopped $NAME" >&2 || echo "$NAME was not running" >&2
    ;;
  status)
    if docker ps --format '{{.Names}}\t{{.Status}}' | grep "^$NAME"; then
      curl -s -H 'Content-Type: application/json' \
        --data '{"jsonrpc":"2.0","id":1,"method":"system_health","params":[]}' http://127.0.0.1:9944; echo
    else
      echo "$NAME is not running"
    fi
    ;;
  logs)
    docker logs -f "$NAME"
    ;;
  *)
    echo "usage: $0 start [--slow] [--keep] | stop | status | logs" >&2
    exit 2
    ;;
esac
