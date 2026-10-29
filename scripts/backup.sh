#!/usr/bin/env bash
# Daily backup (kokoah-subnet-backup.timer): the validator's record and, where the agent service runs, its database
# and stored agent code. 14 of each are kept.
set -euo pipefail
shopt -s nullglob
dir="$HOME/.local/share/kokoah/backups"
day="$(date -u +%Y%m%d)"
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$dir"
chmod 700 "$dir"
umask 077
"$here/db.sh" backup "$dir/kokoah_subnet-$day.dump"
if [ -n "${KOKOAH_SUBNET_AGENT_DATA:-}" ] && [ -d "$KOKOAH_SUBNET_AGENT_DATA" ]; then
  "$here/db.sh" backup "$dir/kokoah_subnet_agents-$day.dump" kokoah_subnet_agents
  tar czf "$dir/kokoah_subnet_agents_bundles-$day.tgz.tmp" -C "$KOKOAH_SUBNET_AGENT_DATA" bundles
  mv -f "$dir/kokoah_subnet_agents_bundles-$day.tgz.tmp" "$dir/kokoah_subnet_agents_bundles-$day.tgz"
fi
# keep the newest 14 of each kind; the date stamp in the name sorts in time order (a kind may have no files)
for kind in kokoah_subnet- kokoah_subnet_agents- kokoah_subnet_agents_bundles-; do
  files=("$dir/$kind"[0-9]*)
  if [ "${#files[@]}" -gt 14 ]; then
    printf '%s\n' "${files[@]}" | sort -r | tail -n +15 | xargs -r -d '\n' rm -f --
  fi
done
