---
name: bug_bash
triggers: ["bug bash", "fix every bug", "grind on bugs", "zero failures"]
budget_default_minutes: 180
required_context_files: ["CLAUDE.md"]
verification_kind: machine
---

# Goal: zero failing tests — {{project_name}}

## Plan
Make the test suite pass. Then keep it passing. Root-cause every failure — never delete a test to make it pass.

**App context:**
- Project root: `{{project_dir}}`
- Test command: `{{test_command}}`
- Current failure count: {{current_failure_count}} (run before dispatch)

**What to do per iteration:**
1. Run `{{test_command}}`
2. Pick the first failing test
3. Read the test, understand intent
4. Read the source it's testing
5. Form a hypothesis for why it fails
6. Apply the fix (in source, not in test)
7. Re-run that one test
8. If green, re-run full suite
9. Repeat until 0 failures

## Verification
- [ ] `{{test_command}}` exits 0
- [ ] No `.skip`, `.only`, `.todo`, or commented-out tests
- [ ] Test count is at least equal to count before the bash started (no tests deleted)
- [ ] `git diff --stat` shows source changes, not just test changes
- [ ] No `expect.assertions(0)` or empty assertion blocks

## Budget
- `max_minutes`: {{budget_minutes|180}}
- `respawn_count_cap`: 2

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

## Important — Iron Rule
- **Do not delete or skip a failing test to make the suite green.** Either fix the source, or mark the test with a clear `// TODO: <reason it's broken>` comment AND write up in `goal-runs/{{ts}}/known-broken.md` why you couldn't fix it.
- If a test is testing a deprecated feature, propose deletion in `known-broken.md` with reasoning — don't auto-delete.
- Follow `superpowers:systematic-debugging` discipline: investigate → analyze → hypothesize → implement. No fix-by-vibes.
