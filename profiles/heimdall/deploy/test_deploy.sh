#!/usr/bin/env bash
# test_deploy.sh: the heimdall deploy kit end to end with a disposable project, in containers. Needs docker (and
# restic, or it runs restic from the restic/restic image). Writes only under a temp folder, removes its
# containers, and never touches ~/apps, systemd or tailscale. About a minute.
#   1. deploy.sh --no-systemd puts a tiny web app (python:3.12-alpine) on 127.0.0.1:<free port>; health passes;
#      a second run keeps .env and compose.yaml; bad names and a new project without --image are refused; hostile
#      --image/--health/--internal-port values (sed's e flag, ; | & \ $() ` quotes, newlines) are refused and run
#      nothing, a health path with ? = & lands in .env verbatim, and an APPS_DIR with ' ; # and spaces runs nothing
#   2. backup.sh snapshots it into a temp restic repository (stopping and restarting the app)
#   3. the project is wiped: containers down, folder gone
#   4. restore.sh brings it back from the snapshot: healthy again, the marker file served, an old folder kept aside
#   5. the Caddyfile validates; app@.service passes systemd-analyze verify (when systemd-analyze exists)
#   6. app@.service's own ExecStart/ExecStop, run as a transient user unit (when a user systemd answers; nothing is
#      installed): it starts the project, a killed app comes back (Restart=on-failure), stopping it removes it
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
command -v docker >/dev/null || { echo "SKIP: no docker"; exit 0; }
tmp=$(mktemp -d "${TMPDIR:-/tmp}/kit-deploy-test.XXXXXX")
name=kittest$$
export APPS_DIR=$tmp/apps
unit=kittest-$name
cleanup() {
  systemctl --user stop "$unit" >/dev/null 2>&1 || true
  [ -f "$APPS_DIR/$name/compose.yaml" ] && (cd "$APPS_DIR/$name" && docker compose down -t 1 >/dev/null 2>&1) || true
  [ -f "${odd:-/nonexistent}/${name}b/compose.yaml" ] && (cd "$odd/${name}b" && docker compose down -t 1 >/dev/null 2>&1) || true
  # the restic container writes as us, the app writes nothing: the temp folder is ours to remove
  rm -rf "$tmp"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')

# restic: the host's, else the official image as our own user, seeing only the temp folder
mkdir -p "$tmp/bin"
if command -v restic >/dev/null; then RESTIC=restic
else
  cat > "$tmp/bin/restic" <<R
#!/bin/sh
exec docker run --rm -u $(id -u):$(id -g) -e RESTIC_REPOSITORY -e RESTIC_PASSWORD_FILE -e RESTIC_CACHE_DIR=$tmp/cache -v "$tmp:$tmp" restic/restic:0.18.1 "\$@"
R
  chmod +x "$tmp/bin/restic"; RESTIC=$tmp/bin/restic
fi
export RESTIC
export RESTIC_ENV=$tmp/restic.env
printf 'export RESTIC_REPOSITORY=%s/repo\nexport RESTIC_PASSWORD_FILE=%s/pass\n' "$tmp" "$tmp" > "$RESTIC_ENV"
echo "test-only-password" > "$tmp/pass"

echo "== 1. deploy"
! "$here/deploy.sh" "Bad Name" --image x --port "$port" --no-systemd >/dev/null 2>&1 || fail "a bad name was accepted"
! "$here/deploy.sh" "$name" --port "$port" --no-systemd >/dev/null 2>&1 || fail "a new project without --image was accepted"
# hostile values: refused before anything is written, and no marker command runs (the old sed program ran this one)
nl=$'\n' mk=$tmp/marker
for v in "x; touch $mk|e #" "x|e touch $mk #" 'a&b' 'a\b' "a${nl}b" "a'b" 'a"b' "a\$(touch $mk)b" "a\`touch $mk\`b" 'a b' 'a#b'; do
  ! "$here/deploy.sh" hostile --image "$v" --port "$port" --no-systemd >/dev/null 2>&1 || fail "--image $(printf %q "$v") was accepted"
done
for v in "/h|e touch $mk #" '/a\b' "/a${nl}b" "/a'b" "/a\$(touch $mk)" '/a b' '/a#b' 'h'; do
  ! "$here/deploy.sh" hostile --image x:1 --port "$port" --health "$v" --no-systemd >/dev/null 2>&1 || fail "--health $(printf %q "$v") was accepted"
done
for v in 0 65536 80x "80${nl}"; do
  ! "$here/deploy.sh" hostile --image x:1 --port "$port" --internal-port "$v" --no-systemd >/dev/null 2>&1 || fail "--internal-port $(printf %q "$v") was accepted"
done
[ ! -e "$mk" ] && [ ! -e "$APPS_DIR/hostile" ] || fail "a hostile value ran a command or wrote the project"
dir=$APPS_DIR/$name
mkdir -p "$dir/data"
# the user's own override (compose loads it next to compose.yaml): this app serves ./data on port 8080
cat > "$dir/compose.override.yaml" <<'O'
services:
  app:
    command: ["python", "-m", "http.server", "8080", "--directory", "/data"]
O
echo ok > "$dir/data/health"
echo "marker-$name" > "$dir/data/index.html"
"$here/deploy.sh" "$name" --image python:3.12-alpine --port "$port" --internal-port 8080 --health '/health?probe=1&x=a:b' --no-systemd
[ "$(stat -c %a "$dir/.env")" = 600 ] || fail ".env is not mode 600"
grep -qx "APP_PORT=$port" "$dir/.env" || fail ".env has no APP_PORT=$port"
grep -qxF 'APP_HEALTH_PATH=/health?probe=1&x=a:b' "$dir/.env" && grep -qxF 'APP_IMAGE=python:3.12-alpine' "$dir/.env" \
  || fail "the values did not land in .env verbatim: $(grep '^APP_' "$dir/.env")"
docker inspect --format '{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}{{.HostIp}}{{end}}{{end}}' "$(cd "$dir" && docker compose ps -q app)" | grep -qx 127.0.0.1 \
  || fail "the app port is not bound to 127.0.0.1 only"
echo "# my edit" >> "$dir/.env"; echo "# my edit" >> "$dir/compose.yaml"
out=$("$here/deploy.sh" "$name" --no-systemd) || fail "second deploy failed: $out"
echo "$out" | grep -q "kept" || fail "second deploy did not report keeping the files"
grep -q "# my edit" "$dir/.env" && grep -q "# my edit" "$dir/compose.yaml" || fail "second deploy overwrote the user's files"
curl -fsS "http://127.0.0.1:$port/" | grep -qx "marker-$name" || fail "the app does not serve the marker"
# a projects folder whose name would break out of the old sh -c "cd '$dir' && ..." string
odd="$tmp/it's; touch $mk #"
port2=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
mkdir -p "$odd/${name}b/data"; cp "$dir/compose.override.yaml" "$odd/${name}b/"; echo ok > "$odd/${name}b/data/health"
APPS_DIR=$odd "$here/deploy.sh" "${name}b" --image python:3.12-alpine --port "$port2" --health /health --no-systemd >/dev/null \
  || fail "deploy into an APPS_DIR with ' ; # failed"
(cd "$odd/${name}b" && docker compose down -t 1 >/dev/null 2>&1)
[ ! -e "$mk" ] || fail "the APPS_DIR name ran a command"
echo "  ok: hostile values refused, nothing ran; values with ? = & stored verbatim; odd APPS_DIR is only a folder"

echo "== 2. backup"
"$here/backup.sh" "$name"
"$here/healthcheck.sh" "http://127.0.0.1:$port/health" 30 1 >/dev/null || fail "the app did not come back after the backup"
(. "$RESTIC_ENV"; "$RESTIC" snapshots --tag "app:$name" --json) | python3 -c 'import json,sys; assert len(json.load(sys.stdin)) == 1' \
  || fail "no snapshot tagged app:$name"

echo "== 3. wipe"
(cd "$dir" && docker compose down -t 1)
rm -rf "$dir"
curl -fsS -m 3 "http://127.0.0.1:$port/" >/dev/null 2>&1 && fail "the app still answers after the wipe"

echo "== 4. restore"
mkdir -p "$dir"; echo stale > "$dir/stale-file"         # a half-rebuilt folder: must be kept aside, not deleted
"$here/restore.sh" "$name" --no-systemd
curl -fsS "http://127.0.0.1:$port/" | grep -qx "marker-$name" || fail "restored app does not serve the marker"
grep -q "# my edit" "$dir/.env" || fail "the restored .env is not the backed-up one"
ls -d "$dir".pre-restore-* >/dev/null 2>&1 && [ -f "$(ls -d "$dir".pre-restore-* | head -n 1)/stale-file" ] \
  || fail "the folder that was there before the restore was not kept aside"
[ -z "$(find "$APPS_DIR" -maxdepth 1 -name ".restore-$name.*")" ] || fail "restore left its temp folder behind"

echo "== 5. templates"
docker run --rm -v "$here/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.10.2 \
  caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile >/dev/null 2>&1 || fail "the Caddyfile does not validate"
if command -v systemd-analyze >/dev/null; then
  mkdir -p "$tmp/units"; cp "$here/app@.service" "$tmp/units/app@demo.service"
  out=$(systemd-analyze --user verify "$tmp/units/app@demo.service" 2>&1 || true)
  ! echo "$out" | grep -v 'WorkingDirectory\|No such file\|Failed to\|is not executable' | grep -qi 'error\|unknown' \
    || fail "app@.service: $out"
fi

echo "== 6. app@.service under systemd"
if systemd-run --user --quiet --wait true 2>/dev/null; then
  grep -qx 'Restart=on-failure' "$here/app@.service" || fail "app@.service lost Restart=on-failure"
  start=$(sed -n 's/^ExecStart=//p' "$here/app@.service") stop=$(sed -n 's/^ExecStop=//p' "$here/app@.service")
  ok_codes=$(sed -n 's/^SuccessExitStatus=//p' "$here/app@.service") force=$(sed -n 's/^RestartForceExitStatus=//p' "$here/app@.service")
  (cd "$dir" && docker compose down -t 1 >/dev/null 2>&1)
  # the unit's own lines; only RestartSec is shortened (15 s in the unit) and PATH passed for a non-standard docker;
  # ExecStopPost records each stop's result (the unit is collected once inactive, so it can't be asked afterwards)
  # shellcheck disable=SC2086
  systemd-run --user --quiet --collect --unit="$unit" -p WorkingDirectory="$dir" -p Restart=on-failure -p RestartSec=2 \
    -p "SuccessExitStatus=$ok_codes" -p "RestartForceExitStatus=$force" \
    -p "ExecStop=$stop" -p "ExecStopPost=/bin/sh -c 'echo \$\$SERVICE_RESULT \$\$EXIT_STATUS >> $tmp/unit-results'" \
    --setenv=PATH="$PATH" $start
  "$here/healthcheck.sh" "http://127.0.0.1:$port/health" 60 1 >/dev/null || fail "the unit did not start the project"
  docker kill "$(cd "$dir" && docker compose ps -q app)" >/dev/null
  sleep 3
  "$here/healthcheck.sh" "http://127.0.0.1:$port/health" 60 1 >/dev/null || fail "a killed app did not come back (Restart=on-failure)"
  [ "$(systemctl --user show -p NRestarts --value "$unit")" -ge 1 ] || fail "the unit did not restart"
  systemctl --user stop "$unit"
  [ -z "$(cd "$dir" && docker compose ps -q)" ] || fail "stopping the unit left containers running"
  last=$(tail -n 1 "$tmp/unit-results" 2>/dev/null || true)
  [ "${last%% *}" = success ] || fail "stopping the unit ended '$last', not success (SuccessExitStatus)"
  echo "  ok: started by the unit, restarted after a kill, removed on stop ($last)"
else
  echo "  skip: no user systemd here"
fi
echo "PASS: deploy, health, keep-on-rerun, backup, wipe, restore, templates and the unit"
