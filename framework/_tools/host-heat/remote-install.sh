#!/bin/bash
# Runs as root on the target host (staged by install.sh). Args: user, --dry-run or -, HALT file or -, staging dir.
set -e
user=$1
arg=$2
halt=$3
stage=$4
# audit 2026-10-13 S9: install only the expected regular file from install.sh's private staging dir
# (a real directory, mode 700, owned by $user); anything else stops here, before any change (exit 3).
bad() { echo "host-heat install: $*" >&2; exit 3; }
uid=$(id -u -- "$user") || bad "no such user: $user"
[ -n "$stage" ] && [ -d "$stage" ] && [ ! -L "$stage" ] || bad "staging dir missing or a symlink: $stage"
[ "$(stat -c '%u %a' -- "$stage")" = "$uid 700" ] || bad "staging dir must be mode 700 and owned by $user: $stage"
src=$stage/host-heat
[ -f "$src" ] && [ ! -L "$src" ] || bad "not a regular file: $src"
[ "$(stat -c %u -- "$src")" = "$uid" ] || bad "not owned by $user: $src"
[ "$arg" = - ] && arg="" || arg=" $arg"   # "-" = empty (ssh drops empty arguments)
[ "$halt" = - ] && halt=""
home=$(getent passwd "$user" | cut -d: -f6)
R=${HOST_HEAT_TEST_ROOT:-}   # tests only (test_host_heat.py): every destination under this folder instead of /
# place MODE SRC DEST : install SRC as DEST. audit 2026-10-13 S13: a DEST (file or symlink) that differs is first kept
# as DEST.bak-<time>[-n], a name no earlier backup has, never executable; an identical DEST is left as it is, but only
# when root owns it (-O): one another account owns is replaced like a different one, as install always did.
place() {
  local b n=0 t
  if [ -f "$3" ] && [ ! -L "$3" ] && [ -O "$3" ] && cmp -s -- "$2" "$3"; then chmod "$1" -- "$3"; return 0; fi
  if [ -e "$3" ] || [ -L "$3" ]; then
    t=$(date +%Y%m%d-%H%M%S); b=$3.bak-$t
    while [ -e "$b" ] || [ -L "$b" ]; do n=$((n + 1)); b=$3.bak-$t-$n; done
    cp -P -p -- "$3" "$b"
    [ -L "$b" ] || chmod a-x -- "$b"
    echo "host-heat install: kept the old $3 as $b"
  fi
  install -m "$1" "$2" "$3"
}
place 755 "$src" "$R/usr/local/bin/host-heat"
install -d -m 755 "$R/etc/host-heat" "$R/var/lib/host-heat"
runuser -u "$user" -- mkdir -p "$home/.local/state/host-heat"
gen=$(mktemp -d)   # root's own folder for the files written here ($stage belongs to $user)
trap 'rm -rf -- "$gen"' EXIT
{
  echo "HOST_HEAT_DIR=$home/.local/state/host-heat"
  echo "HOST_HEAT_USER=$user"
  [ -n "$halt" ] && echo "HOST_HEAT_HALT=$halt"
  true
} > "$gen/env"
place 644 "$gen/env" "$R/etc/host-heat/env"
# SATA SSD temperature needs the drivetemp hwmon driver (loki); NVMe-only hosts skip this
if lsblk -dno TRAN | grep -qx sata && ! grep -qs drivetemp /sys/class/hwmon/hwmon*/name; then
  modprobe drivetemp && echo drivetemp > "$gen/modules" && place 644 "$gen/modules" "$R/etc/modules-load.d/host-heat.conf"
fi
printf '%s\n' '[Unit]' 'Description=host heat watch (CSV record + cool-down + emergency shutdown)' \
  '[Service]' 'Type=oneshot' 'TimeoutStartSec=50' "ExecStart=/usr/local/bin/host-heat --check$arg" > "$gen/service"
place 644 "$gen/service" "$R/etc/systemd/system/host-heat.service"
printf '%s\n' '[Unit]' 'Description=Run host heat watch every minute' '[Timer]' 'OnBootSec=2min' \
  'OnUnitActiveSec=1min' 'AccuracySec=10s' '[Install]' 'WantedBy=timers.target' > "$gen/timer"
place 644 "$gen/timer" "$R/etc/systemd/system/host-heat.timer"
systemctl daemon-reload
systemctl enable --now host-heat.timer
systemctl restart host-heat.timer
echo "host-heat installed on $(hostname): timer $(systemctl is-active host-heat.timer); $(grep ExecStart= "$R/etc/systemd/system/host-heat.service")"
