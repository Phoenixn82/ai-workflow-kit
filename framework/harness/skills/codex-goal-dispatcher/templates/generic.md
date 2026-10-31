---
name: generic
triggers: ["use codex goal", "set this up as a goal", "/goal this", "codex goal dispatch"]
budget_default_minutes: 120
required_context_files: []
verification_kind: tbd
---

# Goal: {{goal_title}} — {{project_name}}

> **Generic template — Claude must fill the Verification block before dispatch.**
>
> If you got here, the dispatcher didn't match a specific template. Before spawning Codex,
> Claude MUST interview the user (or extract from plan.md) to fill in objective verification
> criteria. Without these, `/goal` cannot terminate — it just burns budget.

## Plan
{{plan_body}}

## Verification (stop conditions — Claude fills before dispatch)

> **Required:** at least 3 machine-verifiable checks. If you can't list 3, this work is NOT
> a good fit for `/goal` — recommend doing it in Claude with the user in the loop instead.

- [ ] {{check_1}}
- [ ] {{check_2}}
- [ ] {{check_3}}
- [ ] {{check_n}}

## Budget
- `max_minutes`: {{budget_minutes|120}}
- `respawn_count_cap`: 2
- `on_budget_hit`: write `handoff.md` listing what was done + what's left

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

## Session-end manifest (REQUIRED)

Before you finish — whether the goal completed, hit budget, or was aborted — write `{{project_dir}}/pipeline/goal-runs/{{goal_id}}/changes.md` using the exact schema in `~/.codex/AGENTS.md` § "Session-end changes manifest". This is the hand-back-to-Claude artifact; the babysitter reads it and relays the summary to the user. Half a manifest at `outcome: partial` is far better than none.

## Important
- This is a generic dispatch. The plan and verification block came from manual entry, not a curated template.
- Quality of the loop output is bounded by quality of the verification block. Be specific.
