---
name: test_coverage_lift
triggers: ["lift coverage", "test coverage to", "get tests passing", "coverage to N%"]
budget_default_minutes: 120
required_context_files: ["CLAUDE.md"]
verification_kind: machine
---

# Goal: lift test coverage to {{target_pct|80}}% — {{project_name}}

## Plan
Write tests until coverage hits the threshold. Tests must be real (assert behavior, not "expect(true).toBe(true)").

**App context:**
- Project root: `{{project_dir}}`
- Test framework: {{test_framework}} (pytest / vitest / jest)
- Coverage tool: {{coverage_tool}}
- Current coverage: {{current_coverage_pct}}% (run before dispatch)
- Target: {{target_pct|80}}%

**What to do per iteration:**
1. Run coverage report
2. Pick the lowest-coverage file under {{src_dir}}
3. Read the file, identify untested branches
4. Write a test that exercises an untested branch
5. Run the test — must pass first try OR fix until it passes
6. Re-run coverage
7. Repeat until threshold reached

## Verification
- [ ] `{{coverage_command}}` reports overall coverage ≥ {{target_pct|80}}%
- [ ] No test file contains `expect(true).toBe(true)` or equivalent placeholder
- [ ] Every new test has at least one meaningful `assert` / `expect` per case
- [ ] All tests pass — no skipped, no `.only`, no `.todo`
- [ ] `npm run lint` (or pylint/ruff) clean on new test files

## Budget
- `max_minutes`: {{budget_minutes|120}}
- `respawn_count_cap`: 2
- `on_budget_hit`: write handoff.md with: files still under 50% coverage, what tests were attempted, what blocked

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
- Test the public API, not implementation details
- If a function is genuinely untestable (e.g., wraps a third-party SDK), mock the SDK and assert wiring
- Skip files in `__generated__`, `dist/`, `build/`, `node_modules/` — they don't count
- Do not lower the threshold to make it pass. If you can't reach it, exit budget gracefully.
