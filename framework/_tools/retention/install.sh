#!/usr/bin/env bash
# install.sh [--enable|--dry-run|--self-test]: copy the retain units into ~/.config/systemd/user and daemon-reload.
# Does NOT enable the timer unless --enable is passed. Idempotent (install -C rewrites a unit only when it changed);
# a unit that differs from this folder's is first moved aside to <unit>.bak-<timestamp>.
# Undo: systemctl --user disable --now retain.timer
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
U=$HOME/.config/systemd/user
UNITS=(retain.service retain.timer)
case ${1:-} in
  --self-test)
    systemd-analyze --user verify "${UNITS[@]/#/$HERE/units/}"
    python3 "$HERE/retain.py" --self-test >/dev/null
    python3 "$HERE/repatch.py" --self-test
    t=$(mktemp -d); mkdir -p "$t/.config/systemd/user"; echo mine > "$t/.config/systemd/user/${UNITS[1]}"
    p=$(HOME=$t bash "$HERE/install.sh" --dry-run); rm -rf "$t"   # a unit of yours is moved aside first
    grep -q "^+ mv $t/.config/systemd/user/${UNITS[1]} $t/.config/systemd/user/${UNITS[1]}.bak-" <<<"$p" ||
      { echo "FAIL: no backup before replacing: $p"; exit 1; }
    echo "install.sh self-test: ok"; exit 0 ;;
  --dry-run) R() { echo "+ $*"; } ;;
  ""|--enable) R() { echo "+ $*"; "$@"; } ;;
  *) echo "usage: install.sh [--enable|--dry-run|--self-test]"; exit 2 ;;
esac
R mkdir -p "$U"
TS=$(date +%Y%m%d-%H%M%S)
for u in "${UNITS[@]}"; do
  # cksum, not cmp: diffutils is not in a minimal Arch or Debian install
  if { [ -e "$U/$u" ] || [ -L "$U/$u" ]; } && [ "$(cksum <"$HERE/units/$u")" != "$(cksum <"$U/$u" 2>/dev/null)" ]; then
    R mv "$U/$u" "$U/$u.bak-$TS"
  fi
  R install -C -m 0644 "$HERE/units/$u" "$U/$u"
done
R systemctl --user daemon-reload
if [[ ${1:-} == --enable ]]; then R systemctl --user enable --now retain.timer
else echo "timer installed, not enabled: systemctl --user enable --now retain.timer  (or ./install.sh --enable)"; fi
