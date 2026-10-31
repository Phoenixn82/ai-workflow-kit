#!/bin/bash
# install.sh HOST [--dry]   deploy host-heat to HOST as a root systemd timer (every minute).
#   HOST = odin runs locally; others over ssh (needs sudo -n). --dry: the timer runs `--check --dry-run`
#   (pilot: records the CSV, takes no action). Re-run without --dry to go live.
# Undo: sudo systemctl disable --now host-heat.timer; sudo rm -r /usr/local/bin/host-heat /etc/systemd/system/host-heat.service
#       /etc/systemd/system/host-heat.timer /etc/host-heat/env /var/lib/host-heat   (the CSV history stays in
#       ~/.local/state/host-heat). A file an install replaced is kept beside it as <file>.bak-<time>: move it back or delete it.
set -euo pipefail
host=${1:?host}
arg="-"
[ "${2:-}" = "--dry" ] && arg="--dry-run"
here=$(cd "$(dirname "$0")" && pwd)
halt="-"
[ "$host" = odin ] && halt=$HOME/AI_Projects/_system/automations/HALT
if [ "$host" = odin ]; then
  R() { bash -c "$*"; }
  S() { sudo -n bash "$@"; }
else
  R() { ssh -o BatchMode=yes "$host" "$@"; }
  S() { ssh -o BatchMode=yes "$host" sudo -n bash "$@"; }
fi
user=$(R id -un)
# Private staging dir on the target (mktemp -d: mode 700, new name). audit 2026-10-13 S9: fixed /tmp names let
# another account precreate the file sudo then ran or installed. remote-install.sh takes only this dir's host-heat.
stage=$(R mktemp -d /tmp/host-heat-install.XXXXXXXXXX)
[[ $stage =~ ^/tmp/host-heat-install\.[A-Za-z0-9]{10}$ ]] || { echo "unexpected staging dir: $stage" >&2; exit 1; }
trap 'R rm -rf -- "$stage"' EXIT
R "cat > $stage/host-heat" < "$here/host-heat"
R "cat > $stage/remote-install.sh" < "$here/remote-install.sh"
S "$stage/remote-install.sh" "$user" "$arg" "$halt" "$stage"
