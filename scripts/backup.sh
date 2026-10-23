#!/usr/bin/env bash
# Daily backup (kokoah-subnet-backup.timer): the validator's record and, where the agent service runs, its database
# and stored agent code. 14 of each are kept.
set -euo pipefail
dir="$HOME/.local/share/kokoah/backups"
day="$(date -u +%Y%m%d)"
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$dir"
chmod 700 "$dir"
umask 077
"$here/db.sh" backup "$dir/kokoah_subnet-$day.dump"
if [ -n "${KOKOAH_SUBNET_AGENT_DATA:-}" ]; then
  "$here/db.sh" backup "$dir/kokoah_subnet_agents-$day.dump" kokoah_subnet_agents
  tar czf "$dir/kokoah_subnet_agents_bundles-$day.tgz" -C "$KOKOAH_SUBNET_AGENT_DATA" bundles
fi
for kind in kokoah_subnet- kokoah_subnet_agents- kokoah_subnet_agents_bundles-; do
  ls -1t "$dir/$kind"[0-9]* 2>/dev/null | tail -n +15 | xargs -r rm -f
done
