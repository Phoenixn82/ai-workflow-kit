#!/usr/bin/env bash
# Run every exported tool's --self-test from a framework tree with an empty environment.
# usage: [KIT_HOME=<clone>] [WITH_BUS=1] tools/run-selftests.sh [framework dir] [tool...]   (PASS/SKIP/FAIL per tool; exit 1 if any fail)
# KIT_HOME passes the kit clone (route/lanes lookup); WITH_BUS keeps XDG_RUNTIME_DIR (systemd --user tools).
# VAULT_RAG_PY (a Python with sqlite-vec, e.g. vault-rag's .venv/bin/python) runs the vault-rag tests instead of skipping them.
fw=$(cd "${1:-framework}" && pwd); fails=0
tools=(harness/bin/cx harness/bin/route harness/bin/thor-up harness/hooks/dispatch/dispatch.sh harness/hooks/dispatch/run.py
  harness/hooks/hook_event.py harness/hooks/odin-model-guard.py harness/hooks/route-guard.py harness/hooks/secret-firewall/block-secrets.py
  _tools/hermes-kit/install.sh _tools/hermes-kit/agent-hooks/route-guard.py _tools/hermes-loki/loki-llm-gate _tools/helper-wall/helper-wall _tools/thor-wake-proxy/thor-wake-proxy
  _tools/agents-slice/claude-slice _tools/agents-slice/heavy _tools/fleet-probes/disk-probe _tools/fleet-probes/freellmapi-probe
  _tools/fleet-wake/fleet-wake _tools/fleet-wake/thor/ollama-embed-check _tools/fleet-wake/thor/thor-sleep
  _tools/harness-lint/harness_lint.py _tools/host-heat/host-heat _tools/lane-intel/apply_lanes.py _tools/lane-intel/propose.py
  _tools/lane-intel/route.py _tools/lane-intel/run.sh _tools/lane-intel/slopalytics.py _tools/lane-intel/stance.py
  _tools/lane-intel/install.sh _tools/lane-intel/summarize.py _tools/lane-intel/theo_watch.py _tools/lane-intel/whisper_thor.py _tools/lane-intel/x_watch.py
  _tools/retention/install.sh _tools/retention/repatch.py _tools/retention/retain.py _tools/secret-firewall-corpus/run.py
  _tools/telegram/tg _tools/telegram/tg-admin _tools/transcript-prune/prune_codex.py _tools/vault-lint/vault_lint.py
  _tools/vault-rag/hook-user-prompt.sh _tools/vault-rag/install.sh _tools/vault-rag/reindex.sh _tools/vault-rag/vault_rag.py
  _tools/vault-rag/vault-recall _tools/weekly-review/collect.py _tools/weekly-review/install.sh _tools/weekly-review/run.sh
  _tools/x-scroll/install.sh _tools/x-scroll/xscroll.py)
[ $# -gt 1 ] && tools=("${@:2}")
for t in "${tools[@]}"; do
  f=$fw/$t; h=$(mktemp -d)
  # Unit verification needs a runtime directory, but no running user manager.
  mkdir -m 700 "$h/runtime"
  runtime=$h/runtime
  [[ -n ${WITH_BUS:-} ]] && runtime=${XDG_RUNTIME_DIR:-$runtime}
  case $t in
    _tools/agents-slice/claude-slice|_tools/agents-slice/heavy)
      if ! command -v systemd-run >/dev/null || ! XDG_RUNTIME_DIR="$runtime" systemctl --user show-environment >/dev/null 2>&1; then
        echo "SKIP $t: requires systemd-run and a reachable systemd user bus (use WITH_BUS=1 in a user session)"
        rm -rf "${h:?}"; continue
      fi ;;
    _tools/retention/install.sh|_tools/vault-rag/install.sh|_tools/weekly-review/install.sh)
      if ! command -v systemd-analyze >/dev/null; then
        echo "SKIP $t: systemd-analyze is not installed"
        rm -rf "${h:?}"; continue
      fi ;;
  esac
  if [[ $t == _tools/weekly-review/run.sh ]]; then
    mkdir -p "$h/AI_Projects/_system/automations"
    cp "$fw/_system/lanes.json" "$h/AI_Projects/_system/"
    cp "$fw/_system/automations/"{preflight.py,record_spend.py} "$h/AI_Projects/_system/automations/"
  fi
  if [[ $t == _tools/vault-rag/install.sh ]]; then
    mkdir -p "$h/AI_Projects/_tools"
    ln -s "$fw/_tools/vault-rag" "$h/AI_Projects/_tools/vault-rag"
  fi
  case $f in *.py) run=(python3 "$f");; *.sh) run=(bash "$f");; *) run=("$f");; esac
  [[ $f != *.py && $(head -c 40 "$f") == "#!/usr/bin/env python"* ]] && run=(python3 "$f")
  # harness hooks expect their installed home (~/.claude/hooks): give the empty HOME an installed copy
  if [[ $t == harness/hooks/* ]]; then mkdir -p "$h/.claude"; cp -r "$fw/harness/hooks" "$h/.claude/hooks"; f=$h/.claude/${t#harness/}
    case $f in *.py) run=(python3 "$f");; *.sh) run=(bash "$f");; *) run=("$f");; esac; fi
  extra=(WEB_PKG_DIR="$fw/_tools/web" XDG_RUNTIME_DIR="$runtime"); [[ -n ${KIT_HOME:-} ]] && extra+=(KIT_HOME="$KIT_HOME")
  if [[ -n ${VAULT_RAG_PY:-} ]]; then extra+=(VAULT_RAG_PY="$VAULT_RAG_PY")   # env -i would drop it
    [[ $t == _tools/vault-rag/vault_rag.py ]] && run=("$VAULT_RAG_PY" "$f"); fi
  arg=(--self-test); [[ $t == harness/bin/cx ]] && arg+=(offline)   # the live probe needs a logged-in codex
  out=$(cd "$h" && env -i PYTHONDONTWRITEBYTECODE=1 HOME="$h" PATH=/usr/local/bin:/usr/bin:/bin "${extra[@]}" timeout 120 "${run[@]}" "${arg[@]}" 2>&1); rc=$?
  if [ $rc = 77 ]; then echo "SKIP $t: $out"
  elif [ $rc = 0 ]; then
    echo "PASS $t"
    grep '^SKIP ' <<<"$out" | sed 's/^/    /'
  else
    fails=1; tb=$(grep -c 'Traceback' <<<"$out"); [ "$tb" = 0 ] && tb=
    echo "FAIL $t (rc $rc${tb:+, traceback=$tb})"; tail -n 4 <<<"$out" | sed 's/^/    /' | cut -c1-220; fi
  rm -rf "${h:?}"
done
exit $fails
