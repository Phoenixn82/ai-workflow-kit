#!/usr/bin/env bash
# Offline run of media-backup against a throwaway restic repository (needs restic; no root, no Docker).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd); t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
mkdir -p "$t/media/Photos"; echo pic > "$t/media/Photos/a.jpg"
head -c 24 /dev/urandom | base64 > "$t/pass"
cat > "$t/conf" <<C
MEDIA_DIRS="$t/media"
REPO=$t/repo
PASSWORD_FILE=$t/pass
MUST_HAVE="$t/media/Photos"
IMMICH_DB=
C
run() { MEDIA_BACKUP_CONF=$t/conf MEDIA_BACKUP_STATE=$t/state RESTIC_CACHE_DIR=$t/cache bash "$here/media-backup" "$@"; }
run --check >/dev/null 2>&1 && { echo "FAIL: --check passed without a repository"; exit 1; }
RESTIC_PASSWORD_FILE=$t/pass restic init --repo "$t/repo" -q
touch "$t/marker"; sleep 1
run --check | grep -q "repository $t/repo opens" || { echo "FAIL: --check"; exit 1; }
[ -z "$(find "$t/repo" -newer "$t/marker")" ] || { echo "FAIL: --check wrote to the repository"; exit 1; }
[ ! -e "$t/state" ] && [ ! -e "$t/cache" ] || { echo "FAIL: --check wrote state or cache"; exit 1; }
# a run killed mid-way leaves its lock behind; the next run must clear it and go on
(sleep 8 | RESTIC_PASSWORD_FILE=$t/pass RESTIC_CACHE_DIR=$t/cache restic --repo "$t/repo" -q backup --stdin --stdin-filename x) &
for _ in $(seq 50); do [ -n "$(ls -A "$t/repo/locks" 2>/dev/null)" ] && break; sleep 0.2; done
pkill -9 -f "restic --repo $t/repo" || true; wait 2>/dev/null || true
[ -n "$(ls -A "$t/repo/locks")" ] || { echo "FAIL: no stale lock to test with"; exit 1; }
run >/dev/null 2>&1 && { echo "FAIL: ran without --run"; exit 1; }
run --run >/dev/null 2>&1 || { echo "FAIL: first run"; exit 1; }
[ -s "$t/state/last-ok" ] || { echo "FAIL: no last-ok"; exit 1; }
n=$(RESTIC_PASSWORD_FILE=$t/pass restic --repo "$t/repo" snapshots --json -q | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
[ "$n" -ge 1 ] || { echo "FAIL: no snapshot"; exit 1; }
rm -rf "$t/media/Photos"/*
run --run >/dev/null 2>&1 && { echo "FAIL: ran with an empty MUST_HAVE folder"; exit 1; }
touch "$t/media/Photos/b.jpg"
echo "media-backup test: ok ($n snapshots)"
