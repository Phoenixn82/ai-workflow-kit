#!/usr/bin/env bash
# vault-rag-reindex timer entry point: HALT + preflight -> dry-run -> thor-up ONLY if chunks need embedding AND the embed host is thor
# (heimdall, always on, in the user's hosts.env) -> incremental index. Embed host: VAULT_RAG_OLLAMA_HOST, else EMBED_URL from the
# environment or ~/.config/ai-workflow-kit/hosts.env (KIT_HOSTS_ENV); it is thor when it names "thor" or THOR_HOST from the same file.
# --dry-run prints the plan and what would change (no thor wake, no writes). --self-test checks HALT, preflight, dry-run, wake/no-wake.
set -uo pipefail
D=$(dirname "$(readlink -f "$0")")
HALT=${VAULT_RAG_HALT:-$HOME/AI_Projects/_system/automations/HALT}
PREFLIGHT=${VAULT_RAG_PREFLIGHT:-$HOME/AI_Projects/_system/automations/preflight.py}
STATE=${VAULT_RAG_STATE:-$HOME/.local/state/vault-rag}
# Python with sqlite-vec, as in vault-recall: $VAULT_RAG_PY, else install.sh's .venv, else uv run
if [ -n "${VAULT_RAG_PY:-}" ]; then PY=("$VAULT_RAG_PY")
elif [ -x "$D/.venv/bin/python" ]; then PY=("$D/.venv/bin/python")
else PY=(uv run --quiet --no-project --with sqlite-vec==0.1.9 python); fi
HOSTS_ENV=${KIT_HOSTS_ENV:-$HOME/.config/ai-workflow-kit/hosts.env}
cfg() { local v=${!1:-}; [ -n "$v" ] || v=$(sed -n "s/^$1=//p" "$HOSTS_ENV" 2>/dev/null | tail -n1); v=${v%[\"\']}; v=${v#[\"\']}; printf %s "$v"; }
EMB=${VAULT_RAG_OLLAMA_HOST:-$(cfg EMBED_URL)}; THOR=$(cfg THOR_HOST)
# only thor sleeps; heimdall is always on
WAKE=
case "$EMB" in *thor*) WAKE=thor-up ;; esac
if [ -n "$THOR" ]; then case "$EMB" in *"$THOR"*) WAKE=thor-up ;; esac; fi

if [ "${1:-}" = --self-test ]; then
  t=$(mktemp -d); trap 'rm -rf "$t"' EXIT; touch "$t/HALT"
  out=$(VAULT_RAG_HALT="$t/HALT" bash "$D/reindex.sh"); rc=$?
  [ "$out" = "HALT present" ] && [ "$rc" = 0 ] || { echo "FAIL: HALT path rc=$rc out=$out"; exit 1; }
  # fakes: python (dry-run reports FAKE_N chunks to embed), preflight, thor-up (leaves a marker), logger (silent)
  mkdir "$t/bin"; printf '#!/bin/sh\ntouch "%s/woke"\n' "$t" >"$t/bin/thor-up"; printf '#!/bin/sh\n' >"$t/bin/logger"
  printf '#!/bin/sh\ncase "$*" in *--dry-run*) echo "dry-run: 9 chunks; would embed ${FAKE_N}, remove 0";; *) echo "indexed [OK]";; esac\n' >"$t/py"
  echo 'raise SystemExit(0)' >"$t/pf.py"; chmod +x "$t/bin/"* "$t/py"
  VAULT_RAG_HALT="$t/none" VAULT_RAG_PY="$t/py" FAKE_N=0 bash "$D/reindex.sh" --dry-run | grep -q '^dry-run:' || { echo "FAIL: dry-run"; exit 1; }
  # hosts.env: thor 192.0.2.7, embeds on heimdall 192.0.2.8 (TEST-NET addresses)
  printf 'THOR_HOST=192.0.2.7\nEMBED_URL=http://192.0.2.8:11434\n' >"$t/hosts.env"; printf 'EMBED_URL=http://192.0.2.7:11434\nTHOR_HOST=\047192.0.2.7\047\n' >"$t/thor.env"
  f() { env -u EMBED_URL -u THOR_HOST VAULT_RAG_HALT="$t/none" VAULT_RAG_PREFLIGHT="$t/pf.py" VAULT_RAG_STATE="$t/st" VAULT_RAG_PY="$t/py" PATH="$t/bin:$PATH" FAKE_N="$1" KIT_HOSTS_ENV="${K-$t/hosts.env}" VAULT_RAG_OLLAMA_HOST="${H-http://192.0.2.7:11434}" bash "$D/reindex.sh"; }
  f 0 >/dev/null && [ ! -e "$t/woke" ] || { echo "FAIL: woke thor with nothing to embed"; exit 1; }
  H=http://192.0.2.8:11434 f 5 >/dev/null && [ ! -e "$t/woke" ] || { echo "FAIL: woke thor for a heimdall embed"; exit 1; }
  H= f 5 >/dev/null && [ ! -e "$t/woke" ] || { echo "FAIL: woke thor with VAULT_RAG_OLLAMA_HOST unset (hosts.env EMBED_URL is heimdall)"; exit 1; }
  H= K="$t/thor.env" f 5 >/dev/null && [ -e "$t/woke" ] || { echo "FAIL: did not wake thor for hosts.env EMBED_URL = THOR_HOST"; exit 1; }
  rm -f "$t/woke"; H=http://thor:11434 K="$t/none" f 5 >/dev/null && [ -e "$t/woke" ] || { echo "FAIL: did not wake thor for a host named thor"; exit 1; }
  rm -f "$t/woke"; f 5 >/dev/null && [ -e "$t/woke" ] || { echo "FAIL: did not wake thor with 5 to embed"; exit 1; }
  echo 'raise SystemExit(2)' >"$t/pf.py"; rm "$t/woke"
  [ "$(f 5)" = "preflight blocked" ] && [ ! -e "$t/woke" ] || { echo "FAIL: preflight kill-switch"; exit 1; }
  echo "self-test OK"; exit 0
fi
[ -e "$HALT" ] && { echo "HALT present"; exit 0; }
if [ "${1:-}" = --dry-run ]; then
  host="embed host $EMB is always on"; [ -n "$EMB" ] || host="no embed host: set EMBED_URL in $HOSTS_ENV"
  echo "plan: preflight; prune seen-* older than 7 days; ${WAKE:-no wake ($host)}${WAKE:+ only if chunks need embedding}; vault_rag.py index (incremental)"
  exec "${PY[@]}" "$D/vault_rag.py" index --dry-run
fi
python3 "$PREFLIGHT" >/dev/null || { echo "preflight blocked"; exit 0; }  # budget.json ceiling 0 = kill-switch
find "$STATE" -name 'seen-*' -mtime +7 -delete 2>/dev/null
plan=$(timeout -s KILL 300 "${PY[@]}" "$D/vault_rag.py" index --dry-run 2>&1) || { echo "$plan"; logger -t vault-rag "reindex dry-run failed"; exit 1; }
if grep -q 'would embed 0,' <<<"$plan"; then
  echo "nothing to embed"
elif [ -n "$WAKE" ] && ! "$WAKE"; then
  echo "thor did not wake; index unchanged"; logger -t vault-rag "reindex skipped: thor did not wake"; exit 1
fi
# 7200 s: on a small CPU-only embed host a whole-vault delta can take a couple of hours. The index commit is all-or-nothing, but every embedded batch is cached
# (embed-cache.jsonl), so a run killed at the cap resumes the next day instead of starting over.
out=$(timeout -s KILL 7200 "${PY[@]}" "$D/vault_rag.py" index 2>&1); rc=$?
echo "$out"
logger -t vault-rag "reindex rc=$rc: $(tail -n1 <<<"$out")"
exit "$rc"
