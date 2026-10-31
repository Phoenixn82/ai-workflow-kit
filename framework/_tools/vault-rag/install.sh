#!/usr/bin/env bash
# Idempotent install: make .venv beside vault_rag.py if missing (uv venv + sqlite-vec), link vault-recall into ~/.local/bin,
# link the reindex units, daemon-reload. Embed host: EMBED_URL in ~/.config/ai-workflow-kit/hosts.env (README).
# The daily timer is enabled ONLY with --enable-timer. Never registers hooks (see README). Anything else standing where a
# link goes (your own file, a link elsewhere) is first moved aside to <path>.bak-<timestamp>.
# --dry-run prints the plan (combine with --enable-timer); --self-test verifies files + units.
set -euo pipefail
D=$(dirname "$(readlink -f "$0")")
U="$HOME/.config/systemd/user"
DRY="" TIMER=""
TS=$(date +%Y%m%d-%H%M%S)
run() { if [ -n "$DRY" ]; then echo "+ $*"; else "$@"; fi; }
link() {  # link SRC DEST: back up a different file or link at DEST first
  if { [ -e "$2" ] || [ -L "$2" ]; } && [ "$(readlink -f "$2")" != "$(readlink -f "$1")" ]; then run mv "$2" "$2.bak-$TS"; fi
  run ln -sfn "$1" "$2"
}
for a in "$@"; do case "$a" in
  --dry-run) DRY=1 ;;
  --enable-timer) TIMER=1 ;;
  --self-test)
    for f in vault_rag.py vault-recall hook-user-prompt.sh reindex.sh systemd/vault-rag-reindex.service systemd/vault-rag-reindex.timer; do
      [ -e "$D/$f" ] || { echo "FAIL: missing $f"; exit 1; }
    done
    systemd-analyze --user verify "$D/systemd/vault-rag-reindex.service" "$D/systemd/vault-rag-reindex.timer" || { echo "FAIL: unit verify"; exit 1; }
    p=$(bash "$D/install.sh" --dry-run)
    want=5; [ -x "$D/.venv/bin/python" ] || want=7  # + uv venv, uv pip install
    [ "$(grep '^+ ' <<<"$p" | grep -vc '^+ mv ')" = $want ] && ! grep -q enable <<<"$p" || { echo "FAIL: default plan must not enable the timer: $p"; exit 1; }
    t=$(mktemp -d); mkdir -p "$t/.local/bin"; echo mine > "$t/.local/bin/vault-recall"   # a file of yours is moved aside first
    p=$(HOME=$t bash "$D/install.sh" --dry-run); rm -rf "$t"
    grep -q "^+ mv $t/.local/bin/vault-recall $t/.local/bin/vault-recall.bak-" <<<"$p" || { echo "FAIL: no backup before replacing: $p"; exit 1; }
    [ "$(bash "$D/install.sh" --dry-run --enable-timer | grep -c '^+ systemctl --user enable --now')" = 1 ] || { echo "FAIL: --enable-timer plan"; exit 1; }
    echo "self-test OK"; exit 0 ;;
  *) echo "usage: install.sh [--dry-run] [--enable-timer] | --self-test" >&2; exit 2 ;;
esac; done
if [ ! -x "$D/.venv/bin/python" ]; then
  [ -n "$DRY" ] || command -v uv >/dev/null || { echo "install.sh: needs uv (https://docs.astral.sh/uv/) to make $D/.venv" >&2; exit 1; }
  run uv venv --quiet "$D/.venv"
  run uv pip install --quiet --python "$D/.venv/bin/python" sqlite-vec==0.1.9
fi
run mkdir -p "$HOME/.local/bin" "$U" "$HOME/.local/state/vault-rag"
link "$D/vault-recall" "$HOME/.local/bin/vault-recall"
link "$D/systemd/vault-rag-reindex.service" "$U/vault-rag-reindex.service"
link "$D/systemd/vault-rag-reindex.timer" "$U/vault-rag-reindex.timer"
run systemctl --user daemon-reload
if [ -n "$TIMER" ]; then run systemctl --user enable --now vault-rag-reindex.timer; fi
