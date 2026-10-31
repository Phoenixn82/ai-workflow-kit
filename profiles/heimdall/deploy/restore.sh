#!/usr/bin/env bash
# restore.sh NAME [SNAPSHOT]: put ~/apps/NAME back from its restic snapshot (default: the latest tagged app:NAME),
# start it, and wait for its health path. The current folder is moved aside to NAME.pre-restore-<time>, never
# deleted, so a wrong restore is undone with two mv commands. Works on a fresh heimdall too: the snapshot holds
# compose files, .env and data; deploy.sh NAME afterwards only (re)installs the unit.
# Settings as backup.sh (RESTIC_ENV, APPS_DIR, RESTIC). --no-systemd: start with docker compose, not the unit.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
systemd=1 args=()
for a in "$@"; do [ "$a" = --no-systemd ] && systemd=0 || args+=("$a"); done
name=${args[0]:?usage: restore.sh NAME [SNAPSHOT] [--no-systemd]}
snap=${args[1]:-latest}
apps=${APPS_DIR:-$HOME/apps}
dir=$apps/$name
envf=${RESTIC_ENV:-$HOME/.config/restic/heimdall.env}
[ -r "$envf" ] || { echo "restore.sh: missing $envf" >&2; exit 1; }
. "$envf"
read -r -a restic <<< "${RESTIC:-restic}"

if systemctl --user is-active --quiet "app@$name.service" 2>/dev/null; then systemctl --user stop "app@$name.service"
elif [ -f "$dir/compose.yaml" ]; then (cd "$dir" && docker compose down) || true; fi

mkdir -p "$apps"
tmp=$(mktemp -d "$apps/.restore-$name.XXXXXX")     # same filesystem as the project: the final mv is instant
filter=(); [ "$snap" != latest ] || filter=(--tag "app:$name")   # latest = the newest snapshot of this project
"${restic[@]}" restore "$snap" "${filter[@]}" --target "$tmp"
# the snapshot holds the folder under its old absolute path; find it whatever that path was
src=$(find "$tmp" -type f -name compose.yaml -path "*/$name/compose.yaml" -printf '%h\n' | head -n 1)
[ -n "$src" ] || { echo "restore.sh: no $name/compose.yaml in snapshot $snap (left in $tmp)" >&2; exit 1; }
if [ -e "$dir" ]; then
  aside=$dir.pre-restore-$(date +%Y%m%d-%H%M%S)
  mv -- "$dir" "$aside"
  echo "restore.sh: the folder as it was is kept at $aside"
fi
mv -- "$src" "$dir"
find "$tmp" -depth -type d -empty -delete 2>/dev/null || true   # only the now-empty parent folders

if [ "$systemd" = 1 ] && [ -f "$HOME/.config/systemd/user/app@.service" ]; then systemctl --user start "app@$name.service"
else (cd "$dir" && docker compose up -d); fi
p=$(sed -n 's/^APP_PORT=//p' "$dir/.env" | tail -n 1)
h=$(sed -n 's/^APP_HEALTH_PATH=//p' "$dir/.env" | tail -n 1)
if [ -n "$p" ]; then "$here/healthcheck.sh" "http://127.0.0.1:${p}${h:-/}" 60 2; fi
echo "restore.sh: $name restored from $snap"
