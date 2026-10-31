#!/usr/bin/env bash
# vault-rag UserPromptSubmit hook for Claude Code and Codex (both send {"session_id","prompt",...} on stdin).
# Injects up to 3 strong, not-yet-seen vault chunks as additionalContext (whole JSON output <= 1,500 chars).
# Hook only (the vault-recall CLI keeps everything): raw layers (history.md, journal.ndjson, sessions/, _archive/) are dropped,
# harness-tagged prompts (<task-notification>, <local-command-stdout>, ...) are skipped, headings lose their <!-- --> keys
# and snippets are cut to 120 chars (measure with eval_hook.py --replay).
# Fail open: any error or no strong hit -> no output, exit 0.
RECALL=${VAULT_RECALL:-$HOME/.local/bin/vault-recall}
STATE=${VAULT_RAG_STATE:-$HOME/.local/state/vault-rag}
TAGGED='^[[:space:]]*<[a-z]'  # harness notices (<task-notification>, <local-command-stdout>), not typed prompts
HEADER="vault-recall (paths under ~/AI_Projects/_system/second-brain/ unless ~/; snippets only, Read before relying):"

main() {
  local input prompt sid res new
  input=$(cat) || return
  prompt=$(jq -r '.prompt // empty' <<<"$input") || return
  sid=$(jq -r '.session_id // "nosession"' <<<"$input" | tr -cd 'A-Za-z0-9_-')
  [ -n "$prompt" ] && [ "${#prompt}" -le 2000 ] || return
  [[ $prompt == /* ]] && return
  [[ $prompt =~ $TAGGED ]] && return
  [ "$(wc -w <<<"$prompt")" -ge 6 ] || return
  res=$(timeout -s KILL 5 "$RECALL" --json -k 3 -- "$prompt" 2>/dev/null) || return  # -- : prompts may start with "-"
  mkdir -p "$STATE" && touch "$STATE/seen-$sid" || return
  new=$(jq -c --rawfile seen "$STATE/seen-$sid" \
    '($seen | split("\n")) as $s | [.hits[] | select(.strong and ((.id | IN($s[])) | not)
      and (.path | test("(^|/)(history\\.md|journal\\.ndjson)$|(^|/)(sessions|_archive)/") | not))]' <<<"$res") || return
  [ "$new" = "[]" ] && return
  jq -r '.[].id' <<<"$new" >>"$STATE/seen-$sid"
  jq -c --arg h "$HEADER" '
    def out($c): {hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $c}};
    def fit($c): if (out($c) | tojson | length) <= 1500 then out($c) else fit($c[0:-40]) end;
    def cut: if length <= 120 then . else .[0:120] | sub(" [^ ]*$"; "") + "…" end;
    fit([$h] + map("- \(.path)#\(.heading | gsub("\\s*<!--.*?-->"; "")) — \(.snippet | cut)") | join("\n"))' <<<"$new"
}

if [ "${1:-}" = --self-test ]; then
  t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
  self=$(readlink -f "$0")
  cat >"$t/recall" <<'EOF'
#!/usr/bin/env bash
[ -e "$(dirname "$0")/fail" ] && exit 1
printf '%s\n' "$@" >"$(dirname "$0")/args"
if [ -e "$(dirname "$0")/long" ]; then
  jq -nc '{mode:"hybrid",hits:[range(3) as $i | {path:("Projects/" + ("p" * 120) + "\($i).md"),heading:("\"h\"" * 60),snippet:("s\n" * 120),strong:true,id:"long\($i)"}]}'; exit 0
fi
echo '{"mode":"hybrid","hits":[{"path":"Projects/p/history.md","heading":"H","snippet":"rawlog","strong":true,"id":"hhh"},{"path":"Projects/p/_archive/old.md","heading":"O","snippet":"archived","strong":true,"id":"ooo"},{"path":"a.md","heading":"A  <!-- key:x -->","snippet":"alpha","strong":true,"id":"aaa"},{"path":"b.md","heading":"B","snippet":"beta '"$(printf 'word%.0s ' $(seq 40))"'","strong":true,"id":"bbb"},{"path":"c.md","heading":"C","snippet":"weak","strong":false,"id":"ccc"}]}'
EOF
  chmod +x "$t/recall"
  run() { VAULT_RECALL="$t/recall" VAULT_RAG_STATE="$t/state" bash "$self" <<<"$1"; }
  ok=1; fail() { echo "FAIL: $1"; ok=0; }
  long=$(printf 'word %.0s' $(seq 1 450))
  [ -z "$(run '{"session_id":"s1","prompt":"too short prompt here"}')" ] || fail "short prompt"
  [ -z "$(run '{"session_id":"s1","prompt":"/clear and then do six more words"}')" ] || fail "slash command"
  [ -z "$(run "{\"session_id\":\"s1\",\"prompt\":\"$long\"}")" ] || fail "long prompt"
  [ -z "$(run 'not json at all')" ] || fail "bad stdin"
  out=$(run '{"session_id":"s1","prompt":"how do I wake thor before the backup runs"}')
  ctx=$(jq -r .hookSpecificOutput.additionalContext <<<"$out" 2>/dev/null)
  [[ $ctx == *"a.md#A — alpha"* && $ctx == *"b.md#B — beta word"*"…" && $ctx != *weak* && ${#ctx} -le 1500 ]] || fail "inject: $out"
  [[ $ctx != *rawlog* && $ctx != *archived* && $ctx != *"<!--"* && $(grep -c '^- b.md' <<<"$ctx") = 1 && $(grep '^- b.md' <<<"$ctx" | wc -m) -lt 150 ]] || fail "raw layers / trim: $ctx"
  [ -z "$(run '{"session_id":"s6","prompt":"<task-notification> agent finished the build and all tests passed"}')" ] || fail "tagged prompt"
  [ -z "$(run '{"session_id":"s1","prompt":"how do I wake thor before the backup runs"}')" ] || fail "dedupe"
  [ -n "$(run '{"session_id":"s2","prompt":"how do I wake thor before the backup runs"}')" ] || fail "new session"
  run '{"session_id":"s4","prompt":"-k is a flag but this prompt is fine"}' >/dev/null
  [ "$(tail -n2 "$t/args" | tr '\n' '|')" = "--|-k is a flag but this prompt is fine|" ] || fail "dash prompt args: $(cat "$t/args")"
  touch "$t/long"; out=$(run '{"session_id":"s5","prompt":"how do I wake thor before the backup runs"}'); rm "$t/long"
  [ -n "$out" ] && [ "${#out}" -le 1500 ] && jq -e .hookSpecificOutput.additionalContext <<<"$out" >/dev/null || fail "1500 cap: ${#out}"
  touch "$t/fail"
  [ -z "$(run '{"session_id":"s3","prompt":"how do I wake thor before the backup runs"}')" ] || fail "recall error"
  [ "$ok" = 1 ] && echo "self-test OK" || exit 1
  exit 0
fi

main 2>/dev/null
exit 0
