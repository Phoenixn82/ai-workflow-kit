#!/usr/bin/env bash
# Import the second-brain vault (manifest paths) into ai-memory's own wiki so its
# FTS5 index covers the vault clone on this machine. Idempotent: write-page
# updates in place. Run after `vault-sync pull` or by hand. T2 linux-relaunch 2026-09-04.
set -euo pipefail
REPO="${VAULT_SYNC_REPO:-$HOME/AI_Projects/_system/second-brain}"
AIM="${AI_MEMORY_BIN:-$HOME/.local/bin/ai-memory}"
DATA="${AI_MEMORY_DATA_DIR:-$HOME/.local/share/ai-memory}"
CFG="${AI_MEMORY_CONFIG:-$HOME/.config/ai-memory/config.toml}"
WS="${AI_MEMORY_WORKSPACE:-second-brain}"
PROJECT="${1:-vault}"          # vault | archive
LISTFILE=""; [ "${2:-}" = "--files" ] && LISTFILE="${3:?--files needs a path}"
cd "$REPO"
if [ -n "$LISTFILE" ]; then
  # incremental: only the listed repo-relative paths; vanished files are removed from the index
  n=0; fail=0; del=0
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    case "$rel" in *.md) ;; *) continue ;; esac
    if [ -f "$rel" ]; then
      if "$AIM" --data-dir "$DATA" --config "$CFG" write-page --workspace "$WS" --project "$PROJECT" --path "$rel" --body - --tier semantic --pinned < "$rel" >/dev/null 2>>"$DATA/vault-index.err"; then n=$((n+1)); else fail=$((fail+1)); fi
    else
      "$AIM" --data-dir "$DATA" --config "$CFG" delete-page --workspace "$WS" --project "$PROJECT" --path "$rel" >/dev/null 2>>"$DATA/vault-index.err" && del=$((del+1)) || true
    fi
  done < "$LISTFILE"
  rm -f "$LISTFILE"
  echo "ai-memory incremental: project=$PROJECT updated=$n deleted=$del failed=$fail"
  exit 0
fi
if [ "$PROJECT" = archive ]; then
  ROOT="${VAULT_ARCHIVE:?set VAULT_ARCHIVE to the archived vault directory}"; cd "$ROOT"
  mapfile -d '' FILES < <(find . -name '*.md' -type f -print0)
else
  mapfile -d '' FILES < <(git ls-files -z -- Actions Projects Reference Mechanics People dev-registry harness-memory index.md boot.md session-state.md '_CLAUDE*.md' 'START-HERE*.md' log.md | grep -z '\.md$')
fi
n=0; fail=0
for f in "${FILES[@]}"; do
  rel="${f#./}"
  if "$AIM" --data-dir "$DATA" --config "$CFG" write-page --workspace "$WS" --project "$PROJECT" --path "$rel" --body - --tier semantic --pinned < "$rel" >/dev/null 2>>"$DATA/vault-index.err"; then n=$((n+1)); else fail=$((fail+1)); fi
done
echo "ai-memory import: project=$PROJECT pages=$n failed=$fail (errors in $DATA/vault-index.err)"
