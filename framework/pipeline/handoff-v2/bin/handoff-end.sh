#!/usr/bin/env bash
# Batch B0 (audit 2026-10-12): a Claude-spawned child run (cx sets CLAUDE_SPAWN=1) must not pay the
# human-session ritual: no vault sync, no boot block, no handoff seal. The parent reads its output file.
[[ "${CLAUDE_SPAWN:-}" == "1" ]] && exit 0
cd "$(dirname "$(readlink -f "$0")")/.." || exit 0
# SessionEnd: seal + enqueue + detach. Never distills, never touches the network.
# Must return in milliseconds - distillation is ~20s and does not belong in a
# lifecycle hook.
if [[ "${1:-}" == "--codex" ]]; then
  # Codex calls this field rollout_path; preserve its reader and only start
  # the existing detached worker after a newly admitted segment.
  status="$(jq 'if (.transcript_path? // "") == "" and (.rollout_path? // "") != "" then .transcript_path = .rollout_path else . end' 2>/dev/null | \
    python3 -c 'from handoff import cli, seal; p = cli._hook_input(); tp = cli._transcript_for(p); r = seal.seal(tp, p.get("session_id", "unknown"), p.get("cwd"), "codex") if tp else {}; print(r.get("status", ""))' \
    2>/dev/null || true)"
  [[ "$status" == "sealed" ]] || exit 0
  python3 -c 'from handoff.cli import _spawn_worker; _spawn_worker()' >/dev/null 2>&1 || true
  exit 0
fi
exec python3 -m handoff.cli seal session-end
