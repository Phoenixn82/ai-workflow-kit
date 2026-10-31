---
name: phase_gate
triggers: ["phase gate", "lock in phase", "ratchet phase criteria"]
budget_default_minutes: 60
required_context_files: ["CLAUDE.md", "verification_matrix.json"]
verification_kind: machine
mode: gate
---

# Goal: phase gate — {{phase_name}} ({{project_name}})

> **Gate mode.** One-shot goal that runs after a project-director phase closes. Reads the
> accumulated `verification_matrix.json`, runs every check, fixes failures, exits when
> everything in the matrix passes. The matrix grows phase by phase — each gate inherits
> the locks of every prior phase.

## Plan
Read the verification matrix. Run every check. Fix every failure at the root. Iterate until
the whole matrix is green. Do NOT skip prior phase locks just because they're not the current
focus — they're regression bars.

**Phase context:**
- Phase: `{{phase_name}}` (e.g., `3-design`, `4-build`, `4.5-polish`, `6-qa`)
- Phase deliverables: see `~/Projects/{{project_slug}}/pipeline/{{phase_name}}/` for what just landed
- Verification matrix: `~/Projects/{{project_slug}}/pipeline/verification_matrix.json`
- New criteria added this phase: see `current_phase_criteria` block in the matrix

**Per iteration:**
1. Read `verification_matrix.json` — it's an array of checks, each with `id`, `name`, `phase_added`, `command`, `expect`, `severity`.
2. Run every check in order. Capture pass/fail + evidence.
3. For each failure: investigate, fix at the root (not the check), re-run.
4. If a fix breaks a check from an earlier phase → that's a regression. Fix forward, never weaken the matrix.
5. Once all checks pass → write `gate_result.json` with full evidence + mark goal complete.

## Verification (stop conditions)

The matrix IS the verification. Goal closes when:

```json
{
  "all_passed": true,
  "checks_run": <N>,
  "checks_passed": <N>,
  "checks_failed": 0,
  "regressions_introduced": 0,
  "phase": "{{phase_name}}",
  "evidence_dir": "<path>"
}
```

## verification_matrix.json schema

```json
{
  "project_slug": "{{project_slug}}",
  "matrix_version": 1,
  "checks": [
    {
      "id": "build-passes",
      "name": "npm run build exits 0",
      "phase_added": "4-build",
      "command": "npm run build",
      "expect": {"exit_code": 0},
      "severity": "critical"
    },
    {
      "id": "ts-clean",
      "name": "tsc --noEmit clean",
      "phase_added": "4-build",
      "command": "npx tsc --noEmit",
      "expect": {"exit_code": 0, "stdout_empty": true},
      "severity": "critical"
    },
    {
      "id": "lighthouse-perf",
      "name": "Lighthouse performance ≥ 90 on /",
      "phase_added": "4.5-polish",
      "command": "npx lighthouse http://localhost:3000 --only-categories=performance --output=json --quiet",
      "expect": {"jsonpath": "$.categories.performance.score", "min": 0.9},
      "severity": "high"
    }
  ],
  "current_phase_criteria": ["check-id-a", "check-id-b"]
}
```

## Budget
- `max_minutes`: {{budget_minutes|60}}
- `respawn_count_cap`: 2
- `on_budget_hit`: write partial evidence + matrix state to `gate_result.json`, status: `paused-budget`.

## Coordination with Claude (REQUIRED every turn)

A shared activity log lives at `{{project_dir}}/.claude-codex-log.md`. Claude appends to it automatically after every Edit/Write/Bash via a PostToolUse hook. You must append at end-of-turn so Claude sees what you did. Both sides stay aware via this single file — no extra polling needed.

**At the START of every turn**, tail recent Claude activity:
```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py" tail --project "{{project_dir}}" --lines 15
```
If Claude touched a file you were about to change, reconcile before continuing.

**At the END of every turn**, emit one summary line (<180 chars):
```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py" append --project "{{project_dir}}" --actor codex --action turn --summary "<verb> <object> — <outcome>"
```
Example: `fixed a11y label in QuickCapture — axe clean`.

## Important
- **Never weaken the matrix to make it pass.** If a check is genuinely wrong, write to `goal_edits.md` requesting the human review — don't silently delete or relax it.
- Fixes go in source files, not in checks
- Each fix that touches an earlier phase's surface must re-run THAT phase's full check subset before declaring done — phase isolation is illusory
- Write progress to `feedback.md` like the watcher does — the user's dashboard reads both
