#!/usr/bin/env bash
# run.sh: weekly-review. HALT -> collect.py -> review (claude -p, route headless-summary) -> verify (claude -p, route
# verify-judge) -> commit the report (vault) and REGISTRY.md (_system) -> one silent Telegram line.
#   run.sh             real run (the timer)
#   run.sh --dry-run   print every command; runs nothing, writes nothing
#   run.sh --smoke     stage 2 only, 10 turns, report into ~/.local/state/weekly-review/smoke/, applies nothing,
#                      no verify / commit / Telegram
#   run.sh --self-test offline checks (no claude, no network)
#   run.sh --verify-only <date>  rerun stage 3 + commit + Telegram on an existing review (needs <date>/pre and review.json)
# Costs per stage: ~/.local/state/weekly-review/costs.jsonl. Exits non-zero on failure or a fence breach.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
AI=$HOME/AI_Projects
VAULT=$AI/_system/second-brain
SYS=$AI/_system
HALT=${WR_HALT:-$SYS/automations/HALT}
STATE=$HOME/.local/state/weekly-review
CLAUDE=$HOME/.local/bin/claude
MODE=${1:-run}
DATE=$(date +%F)
[[ $MODE == --verify-only ]] && DATE=${2:-}
RUN=$STATE/$DATE
WEEKLY=$VAULT/Projects/workflow/weekly
INBOX="$HOME/Review Queue Inbox"
export CLAUDE_SPAWN=1 ENABLE_CLAUDEAI_MCP_SERVERS=false

log() { echo "weekly-review: $*"; logger -t weekly-review -- "$*" 2>/dev/null || true; }
AUTO=${WR_AUTOMATIONS:-$SYS/automations}  # automation contract (REGISTRY.md:5): preflight.py before work, record_spend.py after
halt() {
  if [[ -e $HALT ]]; then echo "HALT present"; exit 0; fi
  local why; why=$(python3 "$AUTO/preflight.py" "$AUTO" 2>&1) || { echo "preflight blocked: $why"; exit 0; }
}
tg() { [[ $MODE == run || $MODE == --verify-only ]] || return 0; NOTIFIED=1; timeout -s KILL 60 tg weekly-review "$1" || true; }  # the tg CLI (odin: ssh to heimdall)
fail() { log "FAILED: $*"; tg "weekly-review $DATE FAILED: $*"; exit 1; }
trap 'rc=$?; [[ $rc == 0 || -n ${NOTIFIED:-} ]] || tg "weekly-review $DATE FAILED: exit $rc (unexpected; journalctl --user -u weekly-review)"' EXIT

render() {  # render <template> KEY=value...  (every {{KEY}} must be filled)
  python3 -c '
import sys
t = open(sys.argv[1]).read()
for kv in sys.argv[2:]:
    k, v = kv.split("=", 1)
    t = t.replace("{{%s}}" % k, v)
assert "{{" not in t, "unfilled placeholder in " + sys.argv[1]
sys.stdout.write(t)' "$@"
}

routed() {  # routed <task class> -> "model effort" from the lanes.json routes table; max becomes xhigh (never max)
  python3 -c 'import json, sys; t = json.load(open(sys.argv[2])); l = t["lanes"][t["routes"][sys.argv[1]]["lane"]]; print(l["model"], "xhigh" if l["effort"] == "max" else l["effort"])' "$1" "$SYS/lanes.json"
}
[[ -f $SYS/lanes.json ]] || { echo "weekly-review: no lanes.json at $SYS/lanes.json (install the framework into ~/AI_Projects first)" >&2; exit 2; }
read -r MODEL EFFORT < <(routed headless-summary)  # stage 2 review
read -r VMODEL VEFFORT < <(routed verify-judge)    # stage 3 verify
S_REVIEW='{"type":"object","properties":{"health":{"type":"string","enum":["green","yellow","red"]},"summary":{"type":"string"},"report_path":{"type":"string"},"html_path":{"type":"string"},"applied":{"type":"array","items":{"type":"string"}},"proposals":{"type":"integer"}},"required":["health","summary","report_path","html_path","applied","proposals"]}'
S_VERIFY='{"type":"object","properties":{"verdict":{"type":"string","enum":["pass","reverted","fail"]},"reverted":{"type":"array","items":{"type":"string"}},"summary":{"type":"string"}},"required":["verdict","reverted","summary"]}'
# Settings allow these globally (installers, cd-chains); a headless reviewer gets none of them. Hard excludes too.
DENY=("Bash(pacman *)" "Bash(yay *)" "Bash(paru *)" "Bash(omarchy *)" "Bash(npm install -g *)" "Bash(pnpm add -g *)"
      "Bash(pip install *)" "Bash(pipx install *)" "Bash(uv tool install *)" "Bash(brew install *)" "Bash(cargo install *)"
      "Bash(go install *)" "Bash(cd * && grep *)" "Bash(cd * && cat *)" "Bash(cd * && ls *)" "Bash(cd * && rg *)"
      "Edit(/$HOME/.claude/**)" "Edit(/$HOME/.codex/**)" "Edit(/$HOME/.config/**)" "Edit(/$SYS/lanes.json)"
      "Edit(/$SYS/skills/guard/**)" "Edit(/$AI/**/CLAUDE.md)" "Edit(/$AI/AGENTS.md)" "Edit(/$AI/.claude/**)"
      "Read(/$HOME/.claude/projects/**)" "WebFetch(domain:artificialanalysis.ai)" "WebFetch(domain:www.artificialanalysis.ai)"
      "Bash(git *--output*)")

if [[ $MODE == --smoke ]]; then
  OUT=$STATE/smoke REPORT=$STATE/smoke/$DATE.md HTML=$STATE/smoke/weekly-review-$DATE.html TURNS=10 BUDGET=4
  EDITS=("Edit(/$OUT/**)")
  MODE_NOTE=$'\nSMOKE TEST: you have 10 turns in total. Read only collected.md and lanes.json, fetch the Claude Code CHANGELOG at most once, skip the Agent tool, and write the report and the HTML copy by turn 7.\n'
  APPLY_NOTE="SMOKE MODE: apply nothing. Under Applied, list what you would have applied."
else
  OUT=$WEEKLY REPORT=$WEEKLY/$DATE.md HTML="$INBOX/weekly-review-$DATE.html" TURNS=80 BUDGET=25
  EDITS=("Edit(/$WEEKLY/**)" "Edit(/$INBOX/weekly-review-*.html)" "Edit(/$AI/SKILLS_INDEX.md)" "Edit(/$AI/DECISION_MAP.md)"
         "Edit(/$SYS/automations/REGISTRY.md)" "Edit(/$AI/**/.codex-session-changes/*.md)")
  MODE_NOTE=""
  APPLY_NOTE="Apply these only where collected.md shows the need; otherwise leave the files alone."
fi

cmd() {  # cmd <turns> <model> <effort> <budget> <schema> <add-dir>... -- <allow rule>...   -> CMD array
  local turns=$1 model=$2 effort=$3 budget=$4 schema=$5 dirs=(); shift 5
  while [[ $1 != -- ]]; do dirs+=("$1"); shift; done; shift
  CMD_MODEL="$model $effort"
  CMD=(timeout -k 30 "$((turns * 90))" "$CLAUDE" -p --model "$model" --effort "$effort" --permission-mode dontAsk
       --permission-prompts none --max-turns "$turns" --max-budget-usd "$budget" --output-format json --strict-mcp-config
       --json-schema "$schema" --add-dir "${dirs[@]}" --allowedTools "$@" --disallowedTools "${DENY[@]}")
}

stage() {  # stage <name> <prompt file>: run CMD with the prompt on stdin (variadic flags would swallow a trailing prompt)
  log "stage $1 start (${CMD_MODEL:-?})"
  (cd "$STATE" && "${CMD[@]}" < "$2" > "$RUN/$1.json") || log "stage $1: claude exited $?"
  python3 - "$1" "$RUN/$1.json" "$STATE/costs.jsonl" "$DATE" "$AUTO" <<'PY'
import datetime, json, subprocess, sys
stage, f, out, date, auto = sys.argv[1:]
try:
    d = json.load(open(f))
except (OSError, ValueError):
    d = {"is_error": True, "subtype": "no-json-output"}
row = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "date": date, "stage": stage,
       "cost_usd": d.get("total_cost_usd"), "turns": d.get("num_turns"), "subtype": d.get("subtype"), "is_error": d.get("is_error"),
       "duration_s": round((d.get("duration_ms") or 0) / 1000), "modelUsage": d.get("modelUsage"),
       "denials": [{"tool": x.get("tool_name"), "input": json.dumps(x.get("tool_input"))[:160]} for x in d.get("permission_denials") or []]}
open(out, "a").write(json.dumps(row) + "\n")
# real token count: every model in modelUsage (review subagents run on other models); top-level usage as the fallback
mu = d.get("modelUsage") or {}
if mu:
    tokens = sum(int(m.get(k) or 0) for m in mu.values() for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
else:
    u = d.get("usage") or {}
    tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
status = f"{stage}:{'error' if row['is_error'] else 'ok'}"
r = subprocess.run([sys.executable, auto + "/record_spend.py", auto, "weekly-review", str(tokens), status], capture_output=True, text=True)
if r.returncode:
    print(f"weekly-review: record_spend failed: {r.stderr.strip()}", file=sys.stderr)
print(f"weekly-review: stage {stage}: cost ${row['cost_usd']} turns {row['turns']} {row['subtype']} {row['duration_s']} s denials {len(row['denials'])} tokens {tokens}")
sys.exit(1 if row["is_error"] or not d.get("structured_output") else 0)
PY
}

snapshot() {  # pre-run content snapshot: `git stash create` captures dirty tracked files without touching the tree
  mkdir -p "$RUN/pre"
  for r in "$VAULT" "$SYS"; do b=$(git -C "$r" stash create 2>/dev/null || true); echo "${b:-$(git -C "$r" rev-parse HEAD)}"; done > "$RUN/pre/bases"
  git -C "$VAULT" ls-files --others --exclude-standard > "$RUN/pre/vault-untracked"
  git -C "$SYS" ls-files --others --exclude-standard > "$RUN/pre/sys-untracked"
  cp "$AI/SKILLS_INDEX.md" "$AI/DECISION_MAP.md" "$RUN/pre/"
  touch "$RUN/pre/marker"
}

classify() {  # stdin paths -> "in   <p>" (allowed) or "OUT  <p>"
  while IFS= read -r p; do
    case $p in
      "$WEEKLY"/*|"$SYS/automations/REGISTRY.md"|"$AI/SKILLS_INDEX.md"|"$AI/DECISION_MAP.md"|"$INBOX"/weekly-review-*.html|*/.codex-session-changes/*.md) echo "in   $p" ;;
      *) echo "OUT  $p" ;;
    esac
  done
}

changes() {  # every watched path that changed since the snapshot, classified
  local vb sb m=$RUN/pre/marker
  { read -r vb; read -r sb; } < "$RUN/pre/bases"
  {
    git -C "$VAULT" diff --name-only "$vb" | sed "s|^|$VAULT/|" || true
    git -C "$VAULT" ls-files --others --exclude-standard | grep -vxF -f "$RUN/pre/vault-untracked" | sed "s|^|$VAULT/|" || true
    git -C "$SYS" diff --name-only "$sb" | sed "s|^|$SYS/|" || true
    git -C "$SYS" ls-files --others --exclude-standard | grep -vxF -f "$RUN/pre/sys-untracked" | sed "s|^|$SYS/|" || true
    find "$AI" -maxdepth 1 -type f -newer "$m" 2>/dev/null || true
    find "$HOME/.claude/settings.json" "$HOME/.claude/CLAUDE.md" "$HOME/.claude/hooks" "$HOME/.config/systemd/user" "$HOME/.codex/config.toml" \
      -maxdepth 1 -type f -newer "$m" 2>/dev/null || true
    find "$INBOX" -maxdepth 1 -type f -newer "$m" -name 'weekly-review-*' 2>/dev/null || true
    find "$AI" -maxdepth 6 \( -name node_modules -o -name .git -o -name _archive -o -name .venv \) -prune -o \
      -path '*/.codex-session-changes/*.md' -newer "$m" -print 2>/dev/null || true
  } | sort -u | classify
}

concurrent() {  # another Claude or Codex session wrote a transcript during the window (ours live under *weekly-review*)
  find "$HOME/.claude/projects" "$HOME/.codex/sessions" -name '*.jsonl' -newer "$RUN/pre/marker" -not -path '*weekly-review*' -print -quit 2>/dev/null || true  # no `| head -1`: SIGPIPE + pipefail = exit 141
}

unattributed() {  # stdin: OUT paths -> those no concurrent session is shown to have WRITTEN (astra2_9). A path is excused
  # only when a concurrent transcript (newer than the marker, outside *weekly-review*) records a write of exactly that path
  # timestamped after the marker -- Claude: Edit/Write/MultiEdit/NotebookEdit tool_use whose tool_result is not an error;
  # Codex: a FileChange item_completed event with status "completed" that lists that absolute path (or moves a file to it);
  # Codex logs one per applied patch and none for a failed one, so a failed, swallowed (catch, allSettled), still-running
  # or never-called patch excuses nothing, whatever the exec script's own status says (astra3_R1-9) -- AND no weekly-review session
  # (review, verifier, their subagents) ran a write tool on it. A mention, a Read, a denied edit, an edit from before the
  # marker or a longer path that merely contains it excuses nothing; Bash-made changes are unattributable and stay held.
  python3 -c '
import datetime, json, os, sys
since, roots, want = os.path.getmtime(sys.argv[1]), sys.argv[2:], [l.rstrip("\n") for l in sys.stdin if l.strip()]
WRITE = ("Edit", "Write", "MultiEdit", "NotebookEdit")
def after(ts):
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() >= since
    except (AttributeError, TypeError, ValueError):
        return False
ours, theirs = set(), set()
for root in roots:
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            try:
                if not f.endswith(".jsonl") or os.path.getmtime(p) <= since:
                    continue
                lines = open(p, errors="ignore").read().splitlines()
            except OSError:
                continue
            mine, pending, ok = "weekly-review" in p, {}, set()
            for line in lines:
                if "tool_" not in line and "FileChange" not in line:
                    continue
                try:
                    row = json.loads(line)
                    content, pay = (row.get("message") or {}).get("content"), row.get("payload")
                except (ValueError, AttributeError):
                    continue
                for c in content if isinstance(content, list) else []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use" and c.get("name") in WRITE:
                        i = c.get("input") if isinstance(c.get("input"), dict) else {}
                        path = i.get("file_path") or i.get("notebook_path")
                        if mine:
                            ours.add(path)
                        elif c.get("id") and after(row.get("timestamp")):
                            pending[c["id"]] = path
                    elif c.get("type") == "tool_result" and c.get("tool_use_id") and c.get("is_error") is not True:
                        ok.add(c["tool_use_id"])
                it = pay.get("item") if isinstance(pay, dict) and pay.get("type") == "item_completed" else None
                if not mine and isinstance(it, dict) and it.get("type") == "FileChange" and it.get("status") == "completed" and after(row.get("timestamp")):
                    ch = it.get("changes") if isinstance(it.get("changes"), dict) else {}
                    theirs.update(x for k, v in ch.items() for x in (k, v.get("move_path") if isinstance(v, dict) else None) if isinstance(x, str))
            theirs.update(v for k, v in pending.items() if k in ok)
for w in want:
    if w in ours or w not in theirs:
        print(w)' "$RUN/pre/marker" "$HOME/.claude/projects" "$HOME/.codex/sessions"
}

VB=VAULT_BASE SB=SYS_BASE  # placeholders for --dry-run; the real values come from $RUN/pre/bases
verify_args() {  # read-only git via -C only (no cd); checkout pinned to BASE and to the safe-class paths
  VERIFY_ARGS=(40 "$VMODEL" "$VEFFORT" 12 "$S_VERIFY" "$HOME/.claude/skills" "$HOME/.claude/plugins/cache" "$AI" "$STATE" "$INBOX" -- "${EDITS[@]}"
    "Bash(git -C $VAULT checkout $VB -- Projects/workflow/weekly/*)" "Bash(git -C $SYS checkout $SB -- automations/REGISTRY.md)"
    "Bash(cp $RUN/pre/SKILLS_INDEX.md $AI/SKILLS_INDEX.md)" "Bash(cp $RUN/pre/DECISION_MAP.md $AI/DECISION_MAP.md)" "Bash(diff -u *)" "Bash(ls *)" "Bash(stat *)" "Bash(grep *)")
  local r s; for r in "$VAULT" "$SYS"; do for s in status diff log show; do VERIFY_ARGS+=("Bash(git -C $r $s *)"); done; done
}

verify_prompt() {  # VERIFY.md + the report + diff --stat + the full diff of OUT paths only, cut at 30 KB
  local ch out
  ch=$(changes); out=$(grep '^OUT' <<< "$ch" | cut -c6- || true)
  {
    render "$HERE/VERIFY.md" DATE="$DATE" RUN_DIR="$RUN" REPORT_MD="$REPORT" REPORT_HTML="$HTML" VAULT_BASE="$VB" SYS_BASE="$SB" \
      CONCURRENT="$1" CHANGES="$(grep '^in' <<< "$ch" || true)
OUT  $(grep -c . <<< "$out" || true) path(s): see the stat and the OUT diff below"
    printf '\n## The report as written (%s)\n\n' "$REPORT"; cat "$REPORT"
    printf '\n## diff --stat since BASE (first 25 files per repo)\n\n'
    git -C "$VAULT" diff --stat=120 --stat-count=25 "$VB" || true
    git -C "$SYS" diff --stat=120 --stat-count=25 "$SB" || true
    for f in SKILLS_INDEX.md DECISION_MAP.md; do git diff --no-index --stat=120 "$RUN/pre/$f" "$AI/$f" || true; done
    printf '\n## Full diff of the OUT paths (tracked ones; cut at the 30 KB cap)\n\n'
    grep "^$VAULT/" <<< "$out" | sed "s|^$VAULT/||" | xargs -r -d '\n' git -C "$VAULT" diff "$VB" -- || true
    grep "^$SYS/" <<< "$out" | grep -v "^$VAULT/" | sed "s|^$SYS/||" | xargs -r -d '\n' git -C "$SYS" diff "$SB" -- || true
    grep '^/' <<< "$out" | grep -v "^$SYS/" | sed 's/^/not in git (list only): /' || true
  } | python3 -c '
import sys
d, n = sys.stdin.buffer.read(), 30000
sys.stdout.write(d[:n].decode("utf-8", "ignore"))
if len(d) > n:
    print(f"\n[cut at {n} bytes; get the rest with small `git -C <repo> diff <BASE> -- <path>` calls]")' > "$RUN/verify-prompt.md"
}

finish() {  # after stage 3 and BEFORE any commit: review failure, verdict ("pass" only), deterministic fence; then commit + one line
  local verdict outside conc unexpl cost msg
  verdict=$(jq -r '.structured_output.verdict // "missing"' "$RUN/verify.json" 2>/dev/null) || verdict=unreadable
  [[ -z $RFAIL ]] || fail "$RFAIL (verifier: $verdict; nothing committed)"
  [[ $verdict == pass ]] || fail "verifier verdict $verdict ($(jq -r '.structured_output.summary // ""' "$RUN/verify.json" 2>/dev/null | cut -c1-200)); nothing committed, see $RUN"
  changes > "$RUN/changes.txt"
  outside=$(grep -c '^OUT' "$RUN/changes.txt" || true)
  conc=$(concurrent)
  # anything still changed outside the allowed set is a breach. A concurrent session excuses only the paths it can be
  # shown to have touched (unattributed); the rest blocks the commit for the user (astra2_9)
  if (( outside > 0 )); then
    [[ -n $conc ]] || fail "FENCE: $outside path(s) changed outside the allowed set; nothing committed, see $RUN/changes.txt"
    unexpl=$(grep '^OUT' "$RUN/changes.txt" | cut -c6- | unattributed) || fail "FENCE: attribution check failed; nothing committed, see $RUN/changes.txt"
    [[ -z $unexpl ]] || fail "FENCE: $(grep -c . <<< "$unexpl") of $outside path(s) outside the allowed set not attributable to the concurrent session (e.g. $(head -1 <<< "$unexpl")); nothing committed, held for the user, see $RUN/changes.txt"
  fi
  # nothing staged after add = nothing to commit; any other add/commit failure (hook, lock, disk) is a FAILED run (astra2_10)
  git -C "$VAULT" add -- "$REPORT" || fail "vault: git add of $REPORT failed; nothing committed"
  if git -C "$VAULT" diff --cached --quiet -- "$REPORT"; then log "vault: nothing to commit"
  else git -C "$VAULT" commit -q -m "weekly-review: $DATE report" -- "$REPORT" || fail "vault: commit of $REPORT failed (exit $?); report left uncommitted"
  fi
  git -C "$SYS" diff --quiet HEAD -- automations/REGISTRY.md || git -C "$SYS" commit -q -m "weekly-review: $DATE registry rows" -- automations/REGISTRY.md ||
    fail "_system: commit of automations/REGISTRY.md failed (exit $?); vault report already committed"
  cost=$(jq -s 'map(.total_cost_usd // 0) | add | . * 100 | round / 100' "$RUN/review.json" "$RUN/verify.json")
  msg="weekly-review $DATE: $(jq -r '.structured_output.health' "$RUN/review.json"). $(jq -r '.structured_output.summary' "$RUN/review.json") Verifier: $verdict. \$$cost. Projects/workflow/weekly/$DATE.md"
  [[ $outside -eq 0 ]] || msg+=" ($outside path(s) outside the allowed set, all written by a concurrent session: $RUN/changes.txt)"
  log "$msg"
  tg "$msg"
}

self_test() {
  local t; t=$(mktemp -d)
  bash -n "$0"
  touch "$t/HALT"
  [[ $(WR_HALT=$t/HALT bash "$0" --dry-run) == "HALT present" ]] || { echo "HALT check failed"; exit 1; }
  # preflight: a budget.json ceiling of 0 blocks the run; stage() books real modelUsage tokens through record_spend.py
  mkdir -p "$t/auto"; cp "$SYS/automations/preflight.py" "$SYS/automations/record_spend.py" "$t/auto/"; echo '{"daily_token_ceiling": 0}' > "$t/auto/budget.json"
  [[ $(WR_HALT=$t/none WR_AUTOMATIONS=$t/auto bash "$0" --dry-run) == "preflight blocked: daily_token_ceiling <= 0"* ]] || { echo "preflight check failed"; exit 1; }
  echo '{"daily_token_ceiling": 1000000000}' > "$t/auto/budget.json"
  ( AUTO=$t/auto RUN=$t STATE=$t CMD=(echo '{"structured_output": {"ok": 1}, "is_error": false, "modelUsage": {"a": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3, "cacheCreationInputTokens": 4}, "b": {"inputTokens": 10}}}')
    log() { :; }; stage spendtest /dev/null ) > /dev/null || { echo "stage spendtest failed"; exit 1; }
  grep -q '"automation": "weekly-review", "tokens": 20, "status": "spendtest:ok"' "$t"/auto/spend-ledger-*.jsonl || { echo "record_spend row missing"; exit 1; }
  [[ $MODEL == claude-* && -n $EFFORT && $EFFORT != max && $VMODEL == claude-* && -n $VEFFORT && $VEFFORT != max ]] || { echo "lanes.json routes headless-summary / verify-judge unreadable or max"; exit 1; }
  local tl; tl=$(python3 -c 'import json, sys; t = json.load(open(sys.argv[1])); f = lambda c: t["lanes"][t["routes"][c]["lane"]]; print(*(x for c in ("headless-summary", "verify-judge") for x in (f(c)["model"], f(c)["effort"])))' "$SYS/lanes.json")
  [[ $tl == "$MODEL $EFFORT $VMODEL $VEFFORT" || $tl == *max* ]] || { echo "routed() disagrees with lanes.json routes: $tl vs $MODEL $EFFORT $VMODEL $VEFFORT"; exit 1; }
  local keep=$RUN c; RUN=$t; mkdir -p "$t/pre"; touch -d '-2 days' "$t/pre/marker"; c=$(concurrent); RUN=$keep  # many matches: must not die of SIGPIPE
  render "$HERE/PROMPT.md" DATE=d RUN_DIR=r LAST_REPORT=l CLAUDE_VERSION=v REPORT_MD=m REPORT_HTML=h MODE_NOTE= APPLY_NOTE=a > /dev/null
  render "$HERE/VERIFY.md" DATE=d RUN_DIR=r REPORT_MD=m REPORT_HTML=h VAULT_BASE=b SYS_BASE=s CONCURRENT=no CHANGES=c > /dev/null
  python3 -c 'import json, sys; [json.loads(a) for a in sys.argv[1:]]' "$S_REVIEW" "$S_VERIFY"
  [[ $(printf '%s\n' "$WEEKLY/x.md" "$AI/AGENTS.md" "$INBOX/weekly-review-x.html" | classify | cut -c1-3 | tr -d ' \n') == inOUTin ]] || { echo "classify failed"; exit 1; }
  [[ $(bash "$0" --verify-only 1999-01-01) == usage:* ]] || { echo "--verify-only guard failed"; exit 1; }
  verify_args; [[ ${VERIFY_ARGS[0]} == 40 && " ${VERIFY_ARGS[*]} " == *" Bash(git -C $VAULT status *) "* && ${VERIFY_ARGS[*]} != *"checkout * --"* ]] || { echo "verify_args failed"; exit 1; }
  cmd 3 m1 high 1 "$S_REVIEW" /a "/b c" -- "Edit(//x/**)" WebSearch
  [[ ${CMD[*]} == *"-p --model m1 --effort high "*"--add-dir /a /b c --allowedTools Edit(//x/**) WebSearch --disallowedTools"* ]] || { echo "cmd build failed"; exit 1; }
  cmd "${VERIFY_ARGS[@]}"; [[ ${CMD[*]} == *"--model $VMODEL --effort $VEFFORT "* ]] || { echo "verify stage not on route verify-judge"; exit 1; }
  # after stage 3 (Astra round 1, #9): a verdict other than "pass", or a fence breach, commits nothing and sends FAILED.
  # astra2_9: a concurrent session excuses an OUT path only when its transcript records a successful write of exactly that
  # path after the marker and no weekly-review session (review, verifier, their subagents) wrote it. CONC = a Claude Edit
  # of it, CONC CODEX = a Codex apply_patch of it (both commit); held: CONCX = another path, CONCM = only mentioned/read
  # (prompt text, Read, Codex cat), CONCD = the edit was denied, CONCB = edited before the marker, CONCP = a longer path
  # (AGENTS.md.bak), OWN = a weekly-review subagent wrote it too.
  # astra3_R1-9: a Codex patch counts only through the FileChange item_completed event (status "completed") Codex logs
  # when the patch is applied. Held: CODEXF = "Script failed" with apply_patch verification failed, CODEXU = no output row
  # (unknown), CODEXR = still running (async cell), CODEXK = completed but the script caught the failed patch. Review
  # (astra3r_R1-9) found the "Script completed" proxy fooled: CODEXS = Promise.allSettled swallowed the failure (no
  # "catch" in the script), CODEXH = the script only built the patch text and never called apply_patch (shape of a real
  # 2026-09-30 transcript); also held: CODEXX = FileChange status failed, CODEXO = FileChange of AGENTS.md.bak, CODEXB =
  # FileChange before the marker. Commit: CODEXC = a successful patch inside try/catch, CODEXM = a Move to AGENTS.md.
  # astra2_10: a real vault add or commit failure (GADD, GCOMMIT) is a FAILED run; nothing staged (GCLEAN) is a normal run.
  mkdir -p "$t/run/pre" "$t/home/.claude/projects/-other" "$t/home/.claude/projects/-wr-weekly-review/s1/subagents" "$t/home/.codex/sessions"
  touch -d '-1 hour' "$t/run/pre/marker"
  local ts old; ts=$(date -u +%FT%T.000Z); old=$(date -u -d '-2 hours' +%FT%T.000Z)
  _edit() {  # _edit <timestamp> <path> <is_error>: a concurrent Claude Edit tool_use and its tool_result
    printf '{"timestamp":"%s","message":{"content":[{"type":"tool_use","id":"u1","name":"Edit","input":{"file_path":"%s"}}]}}\n' "$1" "$2"
    printf '{"timestamp":"%s","message":{"content":[{"type":"tool_result","tool_use_id":"u1","is_error":%s,"content":"x"}]}}\n' "$1" "$3"
  }
  _codex() {  # _codex <exec script> <FileChange: -|completed|failed|old|bak|move> [<output text>...]: a concurrent Codex
    # exec call, the FileChange event Codex logs for an applied patch (- = none), its custom_tool_call_output row only when given
    python3 -c 'import json, sys
ts, old, p, js, fc, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6:]
row = lambda typ, pay, t=ts: print(json.dumps({"timestamp": t, "type": typ, "payload": pay}))
row("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": js})
fc == "-" or row("event_msg", {"type": "item_completed", "item": {"type": "FileChange", "id": "exec-1", "status": "failed" if fc == "failed" else "completed",
    "changes": {p + ".bak" if fc in ("bak", "move") else p: {"type": "update", "unified_diff": "", "move_path": p if fc == "move" else None}}}}, old if fc == "old" else ts)
out and row("response_item", {"type": "custom_tool_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": x} for x in out]})' \
      "$ts" "$old" "$AI/AGENTS.md" "$@" > "$t/home/.codex/sessions/r.jsonl"
  }
  local ap='tools.apply_patch("*** Begin Patch\n*** Update File: '"$AI"'/AGENTS.md\n@@\n-a\n+b\n*** End Patch")' okout=$'Script completed\nWall time 0.0 seconds\nOutput:\n'
  local patch="text(await $ap)"
  echo '{"structured_output": {"health": "green", "summary": "ok."}, "total_cost_usd": 1}' > "$t/run/review.json"
  local v r
  for v in '"fail"' '"reverted"' 'null' '"pass" OUT' '"pass" OUT CONC' '"pass" OUT CONC CODEX' '"pass" OUT CONCX' \
           '"pass" OUT CONCM' '"pass" OUT CONCD' '"pass" OUT CONCB' '"pass" OUT CONCP' '"pass" OUT CONC OWN' '"pass"' \
           '"pass" GCOMMIT' '"pass" GCLEAN' '"pass" GADD' '"pass" OUT CONC CODEXF' '"pass" OUT CONC CODEXU' \
           '"pass" OUT CONC CODEXR' '"pass" OUT CONC CODEXK' '"pass" OUT CONC CODEXS' '"pass" OUT CONC CODEXH' \
           '"pass" OUT CONC CODEXX' '"pass" OUT CONC CODEXO' '"pass" OUT CONC CODEXB' '"pass" OUT CONC CODEXC' \
           '"pass" OUT CONC CODEXM'; do
    printf '{"structured_output": {"verdict": %s, "reverted": [], "summary": "canned"}, "total_cost_usd": 1}' "${v%% *}" > "$t/run/verify.json"
    rm -f "$t"/home/.claude/projects/*/*.jsonl "$t"/home/.claude/projects/*/*/*/*.jsonl "$t"/home/.codex/sessions/*.jsonl
    case $v in
      *CONCX*) _edit "$ts" "$AI/other.md" false ;;
      *CONCM*) printf '{"timestamp":"%s","attachment":{"type":"prompt_snapshot","systemPrompt":["Contents of %s"]}}\n' "$ts" "$AI/AGENTS.md"
               printf '{"timestamp":"%s","message":{"content":[{"type":"tool_use","id":"u1","name":"Read","input":{"file_path":"%s"}}]}}\n' "$ts" "$AI/AGENTS.md"
               printf '{"timestamp":"%s","message":{"content":[{"type":"tool_result","tool_use_id":"u1","content":"x"}]}}\n' "$ts"
               printf '%s\n' '{"timestamp":"'"$ts"'","type":"response_item","payload":{"type":"custom_tool_call","call_id":"c1","name":"exec","input":"text(await tools.exec_command({cmd:\"cat '"$AI"'/AGENTS.md\"}))"}}' \
                 > "$t/home/.codex/sessions/r.jsonl" ;;
      *CONCD*) _edit "$ts" "$AI/AGENTS.md" true ;;
      *CONCB*) _edit "$old" "$AI/AGENTS.md" false; printf '{"timestamp":"%s","message":{"content":"still running"}}\n' "$ts" ;;
      *CONCP*) _edit "$ts" "$AI/AGENTS.md.bak" false ;;
      *CODEXF*) _codex "$patch" - $'Script failed\nWall time 0.0 seconds\nOutput:\n' $'Script error:\napply_patch verification failed: Failed to find expected lines' ;;
      *CODEXU*) _codex "$patch" - ;;
      *CODEXR*) _codex "$patch" - 'Script running with cell ID 7' ;;
      *CODEXK*) _codex "try { $patch } catch (e) { text(String(e)) }" - "$okout" ;;
      *CODEXS*) _codex "const r = await Promise.allSettled([$ap]); text(\"done\")" - "$okout" done ;;
      *CODEXH*) _codex 'const patch = "*** Begin Patch\n*** Update File: '"$AI"'/AGENTS.md\n@@\n-a\n+b\n*** End Patch";' - "$okout" ;;
      *CODEXX*) _codex "$patch" failed "$okout" ;;
      *CODEXO*) _codex "$patch" bak "$okout" '{}' ;;
      *CODEXB*) _codex "$patch" old "$okout" '{}' ;;
      *CODEXC*) _codex "try { $patch } catch (e) { text(String(e)) }" completed "$okout" '{}' ;;
      *CODEXM*) _codex "$patch" move "$okout" '{}' ;;
      *CODEX*) _codex "$patch" completed "$okout" '{}' ;;
      *CONC*) _edit "$ts" "$AI/AGENTS.md" false ;;
    esac > "$t/home/.claude/projects/-other/a.jsonl"
    echo '{"message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"'"$AI"'/AGENTS.md"}}]}}' > "$t/home/.claude/projects/-wr-weekly-review/s1.jsonl"
    [[ $v != *OWN* ]] || echo '{"message":{"content":[{"type":"tool_use","name":"Write","input":{"file_path":"'"$AI"'/AGENTS.md"}}]}}' \
      > "$t/home/.claude/projects/-wr-weekly-review/s1/subagents/agent-1.jsonl"
    : > "$t/calls"; r=0
    ( RUN=$t/run RFAIL="" DATE=d REPORT=$t/r.md HOME=$t/home; V=$v
      git() { echo "git $*" >> "$t/calls"; case "$*" in
                *"diff --cached --quiet"*) [[ $V == *GCLEAN* ]] ;;  # 0 = nothing staged
                *" commit "*) [[ $V != *GCOMMIT* ]] ;;
                *" add "*) [[ $V != *GADD* ]] ;;
              esac; }
      tg() { echo "tg $1" >> "$t/calls"; }; log() { :; }
      changes() { echo "in   $WEEKLY/d.md"; [[ $V != *OUT* ]] || echo "OUT  $AI/AGENTS.md"; }
      concurrent() { [[ $V != *CONC* ]] || echo "$HOME/.claude/projects/-other/a.jsonl"; }
      finish ) > /dev/null 2>&1 || r=$?
    case $v in
      '"pass"'|'"pass" OUT CONC'|'"pass" OUT CONC CODEX'|'"pass" OUT CONC CODEXC'|'"pass" OUT CONC CODEXM') [[ $r == 0 ]] && grep -q '^git .* commit ' "$t/calls" && grep -q '^tg weekly-review d: green. ok. Verifier: pass' "$t/calls" ;;
      '"pass" GCLEAN') [[ $r == 0 ]] && ! grep -q '^git .* commit ' "$t/calls" && grep -q '^tg weekly-review d: green. ok. Verifier: pass' "$t/calls" ;;
      '"pass" GCOMMIT') [[ $r == 1 ]] && grep -q '^git .* commit ' "$t/calls" && grep -q '^tg weekly-review d FAILED: ' "$t/calls" && ! grep -q 'Verifier: pass' "$t/calls" ;;
      *) [[ $r == 1 ]] && ! grep -q '^git .* commit ' "$t/calls" && grep -q '^tg weekly-review d FAILED: ' "$t/calls" && ! grep -q 'Verifier: pass' "$t/calls" ;;
    esac || { echo "verdict/fence/commit gate failed for case $v (rc $r):"; cat "$t/calls"; exit 1; }
  done
  # astra2_10 against real git: a rejecting pre-commit hook must fail the run (not log "nothing to commit" and report
  # success); first in the vault (nothing committed), then in _system with REGISTRY.md changed (report committed, run FAILED)
  local h
  for h in gv gs; do
  rm -rf "$t/gv" "$t/gs"; mkdir -p "$t/gv/.git" "$t/gs/automations"
  ( export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
    command git init -q "$t/gv" && command git init -q "$t/gs" && echo row > "$t/gs/automations/REGISTRY.md" &&
      command git -C "$t/gs" add -A && command git -C "$t/gs" commit -q -m init && echo row2 >> "$t/gs/automations/REGISTRY.md"
    printf '#!/bin/sh\nexit 1\n' > "$t/$h/.git/hooks/pre-commit"; chmod +x "$t/$h/.git/hooks/pre-commit"; echo report > "$t/gv/r.md"
    echo '{"structured_output": {"verdict": "pass", "reverted": [], "summary": "canned"}, "total_cost_usd": 1}' > "$t/run/verify.json"
    : > "$t/calls"; r=0
    ( RUN=$t/run RFAIL="" DATE=d VAULT=$t/gv SYS=$t/gs REPORT=$t/gv/r.md
      tg() { echo "tg $1" >> "$t/calls"; }; log() { :; }; changes() { echo "in   $WEEKLY/d.md"; }; concurrent() { :; }
      finish ) > /dev/null 2>&1 || r=$?
    [[ $r == 1 ]] && grep -q '^tg weekly-review d FAILED: ' "$t/calls" && ! grep -q 'Verifier: pass' "$t/calls" &&
      [[ $(command git -C "$t/gv" log --oneline 2>/dev/null | grep -c .) == "$([[ $h == gv ]] && echo 0 || echo 1)" ]] &&
      [[ $(command git -C "$t/gs" log --oneline | grep -c .) == 1 ]] ) || { echo "astra2_10 real-git commit failure ($h hook) not reported:"; cat "$t/calls"; exit 1; }
  done
  python3 "$HERE/collect.py" --self-test
  rm -rf "$t"
  echo "run.sh self-test: ok"
}

case $MODE in
  --self-test) self_test; exit 0 ;;
  run|--dry-run|--smoke) ;;
  --verify-only) [[ $DATE =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ && -s $RUN/pre/bases && -s $RUN/review.json && -s $REPORT ]] ||
                   { echo "usage: run.sh --verify-only YYYY-MM-DD (needs <date>/pre/bases, review.json and the report)"; NOTIFIED=1; exit 2; } ;;
  *) echo "usage: run.sh [--dry-run|--smoke|--self-test|--verify-only YYYY-MM-DD]"; exit 2 ;;
esac
halt

LAST=$(ls "$WEEKLY"/20*.md 2>/dev/null | grep -v "/$DATE.md$" | sort | tail -1 || true)
VER=$("$CLAUDE" --version 2>/dev/null | awk '{print $1}')
REVIEW_ARGS=("$TURNS" "$MODEL" "$EFFORT" "$BUDGET" "$S_REVIEW" "$HOME/.claude/skills" "$HOME/.claude/plugins/cache" "$AI" "$HOME/.config/systemd/user" "$STATE" "$INBOX" -- "${EDITS[@]}" WebSearch WebFetch Agent)
if [[ $MODE == --dry-run ]]; then
  echo "+ nice -n 10 timeout -k 10 900 python3 $HERE/collect.py   # -> $RUN/collected.{md,json}; quiet week => stop here"
  echo "+ snapshot: git stash create in vault and _system -> $RUN/pre/bases; copy SKILLS_INDEX.md, DECISION_MAP.md -> $RUN/pre/"
  cmd "${REVIEW_ARGS[@]}"
  echo "+ (cd $STATE && $(printf '%q ' "${CMD[@]}")< $RUN/review-prompt.md > $RUN/review.json)"
  verify_args; cmd "${VERIFY_ARGS[@]}"
  echo "+ (cd $STATE && $(printf '%q ' "${CMD[@]}")< $RUN/verify-prompt.md > $RUN/verify.json)"
  echo "+ git -C $VAULT commit -m 'weekly-review: $DATE report' -- $REPORT"
  echo "+ git -C $SYS commit -m 'weekly-review: $DATE registry rows' -- automations/REGISTRY.md   # only if changed"
  echo "+ tg weekly-review 'weekly-review $DATE: <health>. <summary> Verifier: <verdict>. \$<cost>. Projects/workflow/weekly/$DATE.md'"
  echo "route headless-summary = $MODEL / $EFFORT, route verify-judge = $VMODEL / $VEFFORT; last report: ${LAST:-none}; claude $VER"
  exit 0
fi

mkdir -p "$RUN" "$OUT"
RFAIL=""
if [[ $MODE == --verify-only ]]; then
  log "verify-only $DATE: stage 3 + commit + Telegram on the existing review"
  jq -e '.structured_output' "$RUN/review.json" > /dev/null || RFAIL="review stage: $(jq -r '.subtype // "no output"' "$RUN/review.json")"
else
  if [[ $MODE == run || ! -s $RUN/collected.json ]]; then
    log "collect"
    nice -n 10 timeout -k 10 900 python3 "$HERE/collect.py" || fail "collect.py exited $?"
  fi
  if [[ $MODE == run ]] && jq -e '.quiet_week' "$RUN/collected.json" > /dev/null; then
    log "quiet week (no Claude or Codex sessions, no failures, all evidence read): LLM stages skipped"; tg "weekly-review $DATE: quiet week, nothing to review"; exit 0
  fi

  render "$HERE/PROMPT.md" DATE="$DATE" RUN_DIR="$RUN" LAST_REPORT="${LAST:-none (first run)}" CLAUDE_VERSION="$VER" \
    REPORT_MD="$REPORT" REPORT_HTML="$HTML" MODE_NOTE="$MODE_NOTE" APPLY_NOTE="$APPLY_NOTE" > "$RUN/review-prompt.md"
  [[ $MODE == run ]] && snapshot
  cmd "${REVIEW_ARGS[@]}"
  halt
  stage review "$RUN/review-prompt.md" || RFAIL="review stage: $(jq -r '.subtype // "no output"' "$RUN/review.json" 2>/dev/null)"
  [[ -s $REPORT ]] || RFAIL="${RFAIL:-review stage:} no report at $REPORT"
  if [[ $MODE == --smoke ]]; then
    [[ -s $REPORT ]] || fail "$RFAIL"
    log "smoke done: $REPORT ${RFAIL:+($RFAIL)}"; echo "--- first 30 lines of $REPORT"; head -30 "$REPORT"; exit 0
  fi
fi
# A failed review may still have edited files: the verifier runs either way, then the run stops before the commit.

{ read -r VB; read -r SB; } < "$RUN/pre/bases"
CONC=$(concurrent)
verify_prompt "$([[ -n $CONC ]] && echo "yes (e.g. $CONC)" || echo no)"
verify_args
cmd "${VERIFY_ARGS[@]}"
halt
stage verify "$RUN/verify-prompt.md" || fail "${RFAIL:+$RFAIL; }verify stage: $(jq -r '.subtype // "no output"' "$RUN/verify.json" 2>/dev/null)"
finish
