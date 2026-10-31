#!/usr/bin/env bash
# install.sh: idempotent. (1) thor venv for the whisper fallback, (2) link the systemd --user units from this dir,
# (3) daemon-reload. It does NOT enable the timer (orchestrator runs the printed enable command after the first run).  install.sh --dry-run prints the plan and changes nothing.
# A unit already there that is not this dir's link is kept as <unit>.bak-<time> first.  install.sh --self-test: link() checks.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"; UNITS="$HOME/.config/systemd/user"; DRY=0
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
PKGS='yt-dlp==2026.8.19 faster-whisper==1.2.1 nvidia-cublas-cu12==12.9.2.10 nvidia-cudnn-cu12==9.27.0.42 av==15.1.0'  # exact pins (= thor's working venv); av 16+ breaks faster-whisper 1.2.1
VENV='~/.local/lane-intel/venv'
if [ $DRY = 1 ]; then
  echo "+ thor-up; ssh thor: [ venv imports faster_whisper ] || python3 -m venv $VENV && pip install $PKGS"
else
  $HOME/.local/bin/thor-up
  ssh -o BatchMode=yes thor "$VENV/bin/python -c 'import faster_whisper' 2>/dev/null && echo 'thor venv ok' ||
    { python3 -m venv $VENV && nice $VENV/bin/pip install -q $PKGS && echo 'thor venv installed'; }"
fi
run mkdir -p "$UNITS"
for u in lane-intel.service lane-intel.timer; do link "$DIR/units/$u" "$UNITS/$u"; done
run systemctl --user daemon-reload
echo "timer NOT enabled. After the first manual run: systemctl --user enable --now lane-intel.timer"
[ $DRY = 1 ] && echo "dry-run: nothing changed" || true
