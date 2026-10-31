#!/usr/bin/env bash
# Deploy tg (run on odin). --dry-run changes no installed file: it says what differs and would be backed up.
# --self-test checks the backup-then-install step offline, in a temp dir.
# No units are added or changed.
#   heimdall: /usr/local/bin/tg, heimdall-alert -> 2-line shim, fleet-heat (say() via tg), seed topics.json
#   odin:     ~/.local/bin/tg (thin ssh client)
#   thor:     thor-heat (topic heat) + heimdall-watch (topic fleet) read a copy of topics.json
# audit 2026-10-13 S9/S13: heimdall's files are staged in a private `mktemp -d` dir (fixed /tmp names let another
# account precreate what sudo then installed), and every existing target that differs from the new file, tg included,
# is first kept as <file>.bak-<UTC timestamp> next to it (.1, .2 ... if taken), so no earlier backup is overwritten.
set -euo pipefail
cd "$(dirname "$0")"
d=${1:-}; [[ $d == --dry-run || $d == --self-test || -z $d ]] || { echo "usage: install.sh [--dry-run|--self-test]"; exit 2; }
DRY=${d:+1} TS=$(date -u +%Y%m%dT%H%M%SZ) SUDO=
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10)

# put SRC DST: install SRC (mode 755) as DST; a DST that exists and differs is first copied to DST.bak-$TS.
# Uses $TS, $DRY (non-empty: only say what it would do) and $SUDO. Also runs on heimdall (sent with declare -f).
put() {
  local src=$1 dst=$2 bak n=1
  [ -f "$src" ] && [ ! -L "$src" ] || { echo "not a regular file: $src" >&2; return 1; }
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    if cmp -s -- "$src" "$dst"; then echo "  $dst: unchanged"; return 0; fi
    bak=$dst.bak-$TS
    while [ -e "$bak" ] || [ -L "$bak" ]; do bak=$dst.bak-$TS.$n; n=$((n + 1)); done
    if [ -n "$DRY" ]; then echo "  $dst: differs, would keep it as $bak"; else
      $SUDO cp -pP -- "$dst" "$bak" || return 1  # no backup, no replacement
      echo "  $dst: previous copy kept as $bak"
    fi
  fi
  if [ -n "$DRY" ]; then echo "  $dst: would install"; return 0; fi
  $SUDO install -m 755 -- "$src" "$dst" || return 1
  echo "  $dst: installed"
}

if [[ $d == --self-test ]]; then
  t=$(mktemp -d); trap 'rm -rf -- "$t"' EXIT
  printf new >"$t/src"; printf old >"$t/a"; printf new >"$t/same"; printf target >"$t/tgt"; ln -s tgt "$t/link"
  TS=T0 DRY=1; put "$t/src" "$t/a" >/dev/null  # dry run: nothing changes
  [[ $(cat "$t/a") == old && ! -e $t/a.bak-T0 ]]
  DRY=; put "$t/src" "$t/a" >/dev/null; put "$t/src" "$t/same" >/dev/null; put "$t/src" "$t/link" >/dev/null
  [[ $(cat "$t/a") == new && $(cat "$t/a.bak-T0") == old && ! -e $t/same.bak-T0 ]]  # differing: kept; identical: left
  [[ -L $t/link.bak-T0 && ! -L $t/link && $(cat "$t/link") == new && $(cat "$t/tgt") == target ]]  # symlink kept as is
  printf newer >"$t/src"; put "$t/src" "$t/a" >/dev/null
  [[ $(cat "$t/a.bak-T0") == old && $(cat "$t/a.bak-T0.1") == new && $(cat "$t/a") == newer ]]  # earlier backup kept
  ln -s src "$t/evil"
  if put "$t/evil" "$t/a" 2>/dev/null; then echo "a symlinked source was installed" >&2; exit 1; fi
  [[ $(cat "$t/a") == newer ]]
  echo "install.sh self-test: ok"; exit 0
fi
./tg --self-test
echo "heimdall:"
stage=$("${SSH[@]}" heimdall 'mktemp -d /tmp/tg-install.XXXXXXXXXX')
[[ $stage =~ ^/tmp/tg-install\.[A-Za-z0-9]{10}$ ]] || { echo "unexpected staging dir on heimdall: $stage" >&2; exit 1; }
trap '"${SSH[@]}" heimdall "rm -rf -- $stage"' EXIT
scp -q -o BatchMode=yes tg heimdall-alert ../host-heat/fleet-heat "heimdall:$stage/"
# shellcheck disable=SC2016  # expanded on heimdall
{ declare -f put; echo 'set -eu; stage=$1 TS=$2 DRY=${3:-} SUDO="sudo -n"
for f in tg heimdall-alert fleet-heat; do put "$stage/$f" "/usr/local/bin/$f"; done
[ -n "$DRY" ] || { /usr/local/bin/tg --self-test && /usr/local/bin/tg --show; }'
} | "${SSH[@]}" heimdall bash -s -- "$stage" "$TS" "$DRY"
echo "odin:"
put tg-odin ~/.local/bin/tg
if timeout 10 "${SSH[@]}" thor true 2>/dev/null; then
  if [[ -n $DRY ]]; then
    "${SSH[@]}" thor 'sudo -n python3 - --dry-run' < patch-thor.py
    echo "+ would copy heimdall's topics.json to thor"
  else
    "${SSH[@]}" thor 'sudo -n python3 -' < patch-thor.py
    j=$("${SSH[@]}" heimdall cat .config/telegram/topics.json) && [[ -n $j ]] &&
      "${SSH[@]}" thor 'umask 077; mkdir -p .config/telegram && cat > .config/telegram/topics.tmp && mv .config/telegram/topics.tmp .config/telegram/topics.json' <<<"$j" ||
      { echo "topics.json not copied to thor (empty or unreadable on heimdall)" >&2; exit 1; }
  fi
else
  echo "thor asleep: run ~/.local/bin/thor-up, then install.sh again (the heimdall/odin steps are idempotent)"
fi
