#!/usr/bin/env bash
# backup.sh NAME [--no-stop]: a restic snapshot of ~/apps/NAME (compose files, .env, data/), tagged app:NAME.
# The project is stopped for the snapshot (a copy of a running database can be torn) and started again after,
# also when the backup fails. --no-stop skips that, for a project with nothing that writes.
# Settings: RESTIC_ENV (default ~/.config/restic/heimdall.env, template restic.env.example). It creates the
# repository on first use, and deletes nothing (pruning old snapshots is yours: restic forget --help).
# APPS_DIR overrides ~/apps; RESTIC overrides the restic command.
set -euo pipefail
name=${1:?usage: backup.sh NAME [--no-stop]}
stop=1; [ "${2:-}" = --no-stop ] && stop=0
dir=${APPS_DIR:-$HOME/apps}/$name
[ -f "$dir/compose.yaml" ] || { echo "backup.sh: no project at $dir" >&2; exit 1; }
envf=${RESTIC_ENV:-$HOME/.config/restic/heimdall.env}
[ -r "$envf" ] || { echo "backup.sh: missing $envf (template: profiles/heimdall/deploy/restic.env.example)" >&2; exit 1; }
. "$envf"
: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY in $envf}"
read -r -a restic <<< "${RESTIC:-restic}"

if [ -n "${WAKE_MAC:-}" ] && [ -n "${WAKE_HOST:-}" ]; then   # a repository on a box that sleeps (thor)
  up() { timeout 3 bash -c ': </dev/tcp/$1/$2' up "$WAKE_HOST" "${WAKE_PORT:-22}" 2>/dev/null; }
  if ! up; then
    python3 -c 'import socket,sys; m=bytes.fromhex(sys.argv[1].replace(":","")); s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1); s.sendto(b"\xff"*6+m*16,("255.255.255.255",9))' "$WAKE_MAC"
    echo "backup.sh: woke $WAKE_HOST, waiting"
    for _ in $(seq 36); do up && break; sleep 5; done
    up || { echo "backup.sh: $WAKE_HOST did not wake up" >&2; exit 1; }
  fi
fi

how=""
if [ "$stop" = 1 ]; then
  if systemctl --user is-active --quiet "app@$name.service" 2>/dev/null; then
    how=unit; systemctl --user stop "app@$name.service"
  elif [ -n "$(cd "$dir" && docker compose ps -q 2>/dev/null)" ]; then
    how=compose; (cd "$dir" && docker compose stop)
  fi
fi
restart() {
  case "$how" in
    unit) systemctl --user start "app@$name.service" ;;
    compose) (cd "$dir" && docker compose start) ;;
  esac
}
trap restart EXIT

"${restic[@]}" cat config >/dev/null 2>&1 || "${restic[@]}" init
"${restic[@]}" backup --tag "app:$name" "$dir"
echo "backup.sh: $name saved (restic snapshots --tag app:$name)"
