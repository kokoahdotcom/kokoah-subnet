#!/usr/bin/env bash
# Daily backup (kokoah-subnet-backup.timer): the validator's record and, where the agent service runs, its database
# and stored agent code. 14 of each are kept, in $KOKOAH_SUBNET_BACKUP_DIR (default ~/.local/share/kokoah/backups).
set -euo pipefail
shopt -s nullglob
dir="${KOKOAH_SUBNET_BACKUP_DIR:-$HOME/.local/share/kokoah/backups}"
day="$(date -u +%Y%m%d)"
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$dir"
chmod 700 "$dir"
umask 077
# a file being written is named *.tmp*: removed if this run fails, never counted as a backup, and swept once a day old
# (left by a run that was killed)
find "$dir" -maxdepth 1 -type f -name '*.tmp*' -mmin +1440 -delete
"$here/db.sh" backup "$dir/kokoah_subnet-$day.dump"
if [ -n "${KOKOAH_SUBNET_AGENT_DATA:-}" ] && [ -d "$KOKOAH_SUBNET_AGENT_DATA" ]; then
  "$here/db.sh" backup "$dir/kokoah_subnet_agents-$day.dump" kokoah_subnet_agents
  bundles="$dir/kokoah_subnet_agents_bundles-$day.tgz"
  trap 'rm -f "$bundles.tmp"' EXIT
  tar czf "$bundles.tmp" -C "$KOKOAH_SUBNET_AGENT_DATA" bundles
  mv -f "$bundles.tmp" "$bundles"
  trap - EXIT
fi
# keep the newest 14 of each kind; the date stamp in the name sorts in time order (a kind may have no files)
for kind in kokoah_subnet- kokoah_subnet_agents- kokoah_subnet_agents_bundles-; do
  files=()
  for f in "$dir/$kind"[0-9]*; do
    case "$f" in *.tmp | *.tmp.*) ;; *) files+=("$f") ;; esac
  done
  if [ "${#files[@]}" -gt 14 ]; then
    printf '%s\n' "${files[@]}" | sort -r | tail -n +15 | xargs -r -d '\n' rm -f --
  fi
done
