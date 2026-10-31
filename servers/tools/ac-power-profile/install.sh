#!/usr/bin/env bash
# Install ac-power-profile on a Linux laptop: sudo servers/tools/ac-power-profile/install.sh   (undo: --undo)
# Needs power-profiles-daemon (powerprofilesctl). Safe to re-run.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
[ "$(id -u)" = 0 ] || exec sudo "$0" "$@"
if [ "${1:-}" = --undo ]; then
  systemctl disable --now ac-power-profile.service 2>/dev/null || true
  rm -f /usr/local/bin/ac-power-profile /etc/systemd/system/ac-power-profile.service /etc/udev/rules.d/90-ac-power-profile.rules
  systemctl daemon-reload; udevadm control --reload
  echo "ac-power-profile removed; the profile stays where it was (powerprofilesctl set balanced to reset it)"
  exit 0
fi
command -v powerprofilesctl >/dev/null || { echo "install power-profiles-daemon first (powerprofilesctl is missing)" >&2; exit 1; }
install -m 755 "$here/ac-power-profile" /usr/local/bin/ac-power-profile
install -m 644 "$here/ac-power-profile.service" /etc/systemd/system/ac-power-profile.service
install -m 644 "$here/90-ac-power-profile.rules" /etc/udev/rules.d/90-ac-power-profile.rules
systemctl daemon-reload
udevadm control --reload
systemctl enable ac-power-profile.service
systemctl start ac-power-profile.service
echo "ac-power-profile installed: $(powerprofilesctl get)"
