#!/usr/bin/env bash
# dispatch.sh <Event> -- one hook process per event instead of one per hook (audit 2026-10-12, item G).
# Runs the hooks settings.json.hooks.original lists for the event, in the same order, and prints one merged result.
#   PreToolUse:                     all five hooks are Python -> run.py runs them in one interpreter.
#   UserPromptSubmit, SessionStart: each bash hook is sourced in a subshell (fork, no new bash) with $0 and its
#                                   args as before; 0/1 outputs pass through verbatim, 2+ go to run.py --merge.
# Plumbing only: no hook is edited. To add/remove a hook, edit the table below (or PRE in run.py) and settings.
# --self-test: syntax + merge checks. Writes nothing itself (the hooks keep their own side effects).
_d_dir=$HOME/.claude/hooks/dispatch
_d_ev="${1:-}"
case "$_d_ev" in
  PreToolUse) exec /usr/bin/python3 -I -S "$_d_dir/run.py" PreToolUse ;;  # pinned: webcred-guard's body needs it
  UserPromptSubmit)
    _d_hooks=("$HOME/.claude/hooks/github-publish-nudge.sh"
              "$HOME/AI_Projects/_tools/vault-rag/hook-user-prompt.sh") ;;
  SessionStart)
    _d_hooks=("$HOME/.claude/hooks/herdr-agent-state.sh session"
              "$HOME/AI_Projects/pipeline/handoff-v2/bin/handoff-start.sh"
              "$HOME/AI_Projects/.claude/hooks/inject-vault-boot.sh") ;;
  --self-test)
    bash -n "$_d_dir/dispatch.sh" && python3 -I -S "$_d_dir/run.py" --self-test && echo "dispatch.sh self-test passed"
    exit ;;
  *) exit 0 ;;
esac
# ponytail: every UserPromptSubmit/SessionStart matcher today is ""/"*", so no matcher check here.
_d_in="$(</dev/stdin)"
set --  # a sourced hook given no args must not see ours
_d_outs=()
_d_block=0
for _d_h in "${_d_hooks[@]}"; do
  _d_sh="${_d_h%% *}"
  _d_args="${_d_h#"$_d_sh"}"
  # shellcheck disable=SC1090,SC2086
  _d_o="$(BASH_ARGV0="$_d_sh"; . "$_d_sh" $_d_args <<<"$_d_in")"
  case $? in
    0) [[ -n "$_d_o" ]] && _d_outs+=("$_d_o") ;;
    2) _d_block=1 ;;  # ponytail: exit 2 blocks the whole event; no hook here uses it today
    *) ;;             # non-blocking error: output dropped, stderr already went through
  esac
done
((_d_block)) && exit 2
case ${#_d_outs[@]} in
  0) ;;
  1) printf '%s\n' "${_d_outs[0]}" ;;
  *) printf '%s\0' "${_d_outs[@]}" | python3 -I -S "$_d_dir/run.py" --merge "$_d_ev" ;;
esac
exit 0
