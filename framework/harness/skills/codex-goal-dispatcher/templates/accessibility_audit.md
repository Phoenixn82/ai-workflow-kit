---
name: accessibility_audit
triggers: ["accessibility audit", "a11y pass", "fix all the a11y", "pa11y clean"]
budget_default_minutes: 120
required_context_files: ["CLAUDE.md"]
verification_kind: machine
---

# Goal: full accessibility pass — {{project_name}}

## Plan
Run pa11y + axe across every route, fix every violation, prove clean.

**App context:**
- Project root: `{{project_dir}}`
- Routes: {{routes}}
- Standard: WCAG 2.1 AA

**What to do per iteration:**
1. Run `pa11y --standard WCAG2AA <url>` against each route
2. Run `npx @axe-core/cli <url>` against each route
3. Group violations by component (not by route — same component breaks every route)
4. Fix the component
5. Re-run on all affected routes
6. Repeat until 0 violations everywhere

## Verification
- [ ] `pa11y --standard WCAG2AA` returns 0 errors on every route in `{{routes}}`
- [ ] `@axe-core/cli` returns 0 violations (serious + critical) on every route
- [ ] Every interactive element has a discernible name (button text, aria-label, or aria-labelledby)
- [ ] Every form input has an associated `<label>`
- [ ] Every image has `alt` (decorative → `alt=""`)
- [ ] Color contrast ≥ 4.5:1 for body text, ≥ 3:1 for large text
- [ ] Heading hierarchy is sequential (no h1 → h3 skip)
- [ ] Focus visible on every interactive element (no `outline: none` without replacement)
- [ ] Keyboard-only navigation works on every route (Playwright test proves it)

## Budget
- `max_minutes`: {{budget_minutes|120}}
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

## Important
- Don't add `aria-hidden` to fix contrast — fix the contrast
- Don't suppress axe rules to pass — actually fix them
- If a violation is in third-party code (e.g., a UI library button), open an issue in `pipeline/goal-runs/{{ts}}/third-party-blockers.md` and wrap the component locally
