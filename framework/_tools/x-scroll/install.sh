#!/usr/bin/env bash
# install.sh: idempotent. Links ~/.local/bin/x-scroll and the systemd --user units, makes the 0700 state dir, seeds
# ~/.config/x-scroll/watch.json if missing, daemon-reload. It does NOT enable the timer (do that after a supervised
# logged-in run passes).  install.sh --dry-run prints the plan and changes nothing.
# A command or unit already there that is not this dir's link is kept as <name>.bak-<time> first.  --self-test: link() checks.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"; UNITS="$HOME/.config/systemd/user"; CFG="$HOME/.config/x-scroll/watch.json"; DRY=0
[ "${1:-}" = --dry-run ] && DRY=1
run() { echo "+ $*"; [ $DRY = 1 ] || "$@"; }
STAMP=$(date +%Y%m%d-%H%M%S)
# link SRC DEST: DEST becomes a symlink to SRC. Anything else at DEST (a file, another or a dangling link, a dir) is moved
# to DEST.bak-<time>[.N] first, never over an earlier backup (kit S13).
link() {
  if [ "$(readlink -f -- "$2" 2>/dev/null)" = "$(readlink -f -- "$1")" ]; then echo "= $2 already linked"; return 0; fi
  if [ -e "$2" ] || [ -L "$2" ]; then
    local b="$2.bak-$STAMP" n=0
    while [ -e "$b" ] || [ -L "$b" ]; do n=$((n + 1)); b="$2.bak-$STAMP.$n"; done
    run mv -nT -- "$2" "$b"
  fi
  run ln -sT -- "$1" "$2"
}
if [ "${1:-}" = --self-test ]; then  # link() against seeded files, links and an earlier backup, run twice
  t=$(mktemp -d); trap 'rm -rf "$t"' EXIT; t=$(readlink -f "$t")
  fail() { echo "install.sh self-test: FAIL $*"; exit 1; }
  printf new >"$t/src"; printf custom >"$t/file"; ln -s "$t/gone" "$t/dangling"; printf other >"$t/other"
  ln -s "$t/other" "$t/otherlink"; ln -s "$t/src" "$t/ours"; mkdir "$t/dir"; printf old >"$t/coll"; printf earlier >"$t/coll.bak-$STAMP"
  for pass in 1 2; do for d in file dangling otherlink ours dir coll; do link "$t/src" "$t/$d" >/dev/null; done; done
  for d in file dangling otherlink ours dir coll; do [ "$(readlink -f "$t/$d")" = "$t/src" ] || fail "$d not linked"; done
  [ "$(cat "$t/file.bak-$STAMP")" = custom ] && [ "$(readlink "$t/dangling.bak-$STAMP")" = "$t/gone" ] &&
    [ "$(readlink "$t/otherlink.bak-$STAMP")" = "$t/other" ] && [ -d "$t/dir.bak-$STAMP" ] || fail "a backup lost the original"
  [ "$(cat "$t/coll.bak-$STAMP")" = earlier ] && [ "$(cat "$t/coll.bak-$STAMP.1")" = old ] || fail "an earlier backup was overwritten"
  [ "$(find "$t" -maxdepth 1 -name '*.bak-*' | wc -l)" = 6 ] || fail "backups: $(ls "$t")"
  echo "install.sh self-test: ok"; exit 0
fi
[ "$(uname -n | tr A-Z a-z)" = odin ] || { echo "x-scroll runs on odin only (web act and the identity browser live there)" >&2; exit 2; }
run mkdir -p "$HOME/.local/bin" "$UNITS"
run install -d -m 700 "$HOME/.local/state/x-scroll"
link "$DIR/xscroll.py" "$HOME/.local/bin/x-scroll"
for u in x-scroll.service x-scroll.timer; do link "$DIR/units/$u" "$UNITS/$u"; done
if [ -e "$CFG" ]; then echo "= $CFG exists"; else
  run mkdir -p "$(dirname "$CFG")"
  echo "+ write $CFG"; [ $DRY = 1 ] || printf '{"handles": [], "searches": [], "minutes": 15}\n' > "$CFG"
fi
run systemctl --user daemon-reload
echo "timer NOT enabled. After a supervised logged-in run passes: systemctl --user enable --now x-scroll.timer"
[ $DRY = 1 ] && echo "dry-run: nothing changed" || true
