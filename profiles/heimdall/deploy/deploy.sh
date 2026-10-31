#!/usr/bin/env bash
# deploy.sh NAME --image IMAGE --port PORT [--internal-port N] [--health PATH] [--no-systemd] [--dry-run]
#
# Puts one project on heimdall the same way every time (profiles/heimdall/README.md "Deploy a project"):
#   ~/apps/NAME/compose.yaml   from deploy/project/ (copied once; yours afterwards, never overwritten)
#   ~/apps/NAME/.env           mode 600, filled from the flags (kept as it is when it exists)
#   ~/apps/NAME/data/          the project's data: what backup.sh saves
#   ~/.config/systemd/user/app@.service   one unit for every project (a changed one is backed up first)
# then starts it with systemd (app@NAME.service, restarts on failure, starts at boot) and waits for the health
# path to answer. Safe to re-run. --no-systemd starts it with plain `docker compose up -d` (tests, or a box
# without a user systemd). APPS_DIR overrides ~/apps.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
apps=${APPS_DIR:-$HOME/apps}
usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
name="" image="" port="" iport=8080 health=/ systemd=1 dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --image) image=${2:?}; shift ;;
    --port) port=${2:?}; shift ;;
    --internal-port) iport=${2:?}; shift ;;
    --health) health=${2:?}; shift ;;
    --no-systemd) systemd=0 ;;
    --dry-run|-n) dry=1 ;;
    -h|--help) usage ;;
    -*) echo "deploy.sh: unknown flag $1" >&2; usage ;;
    *) [ -z "$name" ] || usage; name=$1 ;;
  esac
  shift
done
[[ $name =~ ^[a-z0-9][a-z0-9_-]{0,40}$ ]] || { echo "deploy.sh: NAME must be lower-case letters, digits, - or _ (it becomes the folder, compose project and unit name)" >&2; exit 2; }
dir=$apps/$name
say() { printf '  %s\n' "$*"; }
run() { printf '  + %s\n' "$*"; [ "$dry" = 1 ] || "$@"; }

if [ ! -f "$dir/.env" ]; then
  # The values go into .env as plain data (written below, never through sed or a shell), so each must be one line
  # of characters that mean nothing to dotenv, docker compose or a shell: no spaces, quotes, $, `, \, # or ;.
  [ -n "$image" ] || { echo "deploy.sh: a new project needs --image" >&2; exit 2; }
  [[ $image =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$ ]] || { echo "deploy.sh: --image must be an image reference like ghcr.io/you/app:1.2.0 (letters, digits, . _ / : @ -)" >&2; exit 2; }
  [[ $port =~ ^[0-9]+$ ]] && [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || { echo "deploy.sh: --port must be 1024-65535" >&2; exit 2; }
  [[ $iport =~ ^[0-9]+$ ]] && [ "$iport" -ge 1 ] && [ "$iport" -le 65535 ] || { echo "deploy.sh: --internal-port must be 1-65535" >&2; exit 2; }
  [[ $health =~ ^/[A-Za-z0-9._~/%:@?=\&+,!*-]*$ ]] || { echo "deploy.sh: --health is a URL path starting with / (letters, digits, . _ ~ / % : @ ? = & + , ! * -)" >&2; exit 2; }
  if ss -Hltn "sport = :$port" 2>/dev/null | grep -q .; then echo "deploy.sh: port $port is already in use on this box" >&2; exit 1; fi
fi

echo "==> $name in $dir"
run mkdir -p "$dir/data"
if [ -e "$dir/compose.yaml" ]; then say "= $dir/compose.yaml (yours, kept)"
else run cp "$here/project/compose.yaml" "$dir/compose.yaml"; fi
if [ -e "$dir/.env" ]; then
  say "= $dir/.env (kept; edit it to change image or ports)"
else
  printf '  + write %s (mode 600)\n' "$dir/.env"
  if [ "$dry" = 0 ]; then
    # env.example line by line; the five known keys get the validated values above, as data
    (umask 077
     while IFS= read -r line || [ -n "$line" ]; do
       case "$line" in
         APP_NAME=*) printf 'APP_NAME=%s\n' "$name" ;;
         APP_IMAGE=*) printf 'APP_IMAGE=%s\n' "$image" ;;
         APP_PORT=*) printf 'APP_PORT=%s\n' "$port" ;;
         APP_INTERNAL_PORT=*) printf 'APP_INTERNAL_PORT=%s\n' "$iport" ;;
         APP_HEALTH_PATH=*) printf 'APP_HEALTH_PATH=%s\n' "$health" ;;
         *) printf '%s\n' "$line" ;;
       esac
     done < "$here/project/env.example" > "$dir/.env")
  fi
fi

if [ "$systemd" = 1 ]; then
  unit=$HOME/.config/systemd/user/app@.service
  if [ -f "$unit" ] && cmp -s "$here/app@.service" "$unit"; then say "= $unit"
  else
    if [ -f "$unit" ]; then run cp -p "$unit" "$unit.bak-$(date +%Y%m%d-%H%M%S)"; fi
    run install -D -m 644 "$here/app@.service" "$unit"
    run systemctl --user daemon-reload
  fi
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" != yes ]; then
    say "lingering is off: user units would stop when you log out. Turning it on needs sudo once:"
    run sudo loginctl enable-linger "$USER"
  fi
  run systemctl --user enable --now "app@$name.service"
else
  printf '  + (cd %q && docker compose up -d)\n' "$dir"
  [ "$dry" = 1 ] || (cd -- "$dir" && docker compose up -d)
fi

[ "$dry" = 1 ] && { echo "(dry run: nothing was changed)"; exit 0; }
p=$(sed -n 's/^APP_PORT=//p' "$dir/.env" | tail -n 1)
h=$(sed -n 's/^APP_HEALTH_PATH=//p' "$dir/.env" | tail -n 1)
"$here/healthcheck.sh" "http://127.0.0.1:${p}${h:-/}" 60 2 || {
  echo "deploy.sh: $name started but is not healthy. Logs: journalctl --user -u app@$name -n 50  (or: cd $dir && docker compose logs)" >&2
  exit 1
}
cat <<NEXT
Next:
  - reach it on the tailnet:   $here/tailscale-serve.sh $name     (or public: deploy/caddy, profiles/heimdall/README.md)
  - Uptime Kuma: add an HTTP monitor for http://127.0.0.1:${p}${h:-/} with your ntfy/Telegram notification
  - backups: $here/backup.sh $name   (add it to a nightly timer; restore: $here/restore.sh $name)
  - register it: one row in your jobs registry (what, where, how to stop: systemctl --user disable --now app@$name)
NEXT
