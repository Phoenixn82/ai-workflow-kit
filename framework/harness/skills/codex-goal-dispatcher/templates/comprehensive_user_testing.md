---
name: comprehensive_user_testing
triggers: ["comprehensive user testing", "qa the whole app", "test this end to end", "full qa pass"]
budget_default_minutes: 180
required_context_files: ["CLAUDE.md", "plan.md"]
verification_kind: machine
---

# Goal: comprehensive user testing — {{project_name}}

## Plan
Test every user-facing surface in this app and fix everything broken. Do NOT stop until every check below passes.

**App context:**
- Project root: `{{project_dir}}`
- Stack: {{stack}}
- Routes detected: {{routes}}
- Forms detected: {{forms}}
- Auth gated routes: {{auth_routes}}
- Public routes: {{public_routes}}

**What to do per iteration:**
1. Pick the lowest-numbered failing check below
2. Reproduce the failure with Playwright (write the test if missing)
3. Fix the root cause in source (not the test)
4. Re-run the check
5. If passing, mark and move to next; if failing, log attempt to `pipeline/goal-runs/{{ts}}/attempts.log` and retry

## Verification (stop conditions — ALL must pass; goal complete only when verifier returns green)

### Build + run
- [ ] `npm run build` exits 0, no warnings on TypeScript or ESLint
- [ ] `npm run dev` reaches "ready" within 30s
- [ ] Server responds 200 on `/` within 5s of "ready"

### Playwright suite (write to `tests/e2e/`)
For each route in `{{routes}}`:
- [ ] Page loads under 3s on cold cache
- [ ] No console errors (warnings allowed)
- [ ] No 4xx/5xx network requests except expected ones (e.g., /api/me on logged-out)
- [ ] Primary CTA is visible above the fold at 1440x900
- [ ] Primary CTA is visible above the fold at 375x667 (mobile)
- [ ] Click primary CTA → expected navigation OR expected state change

For each form in `{{forms}}`:
- [ ] Submit empty → all required fields show validation
- [ ] Submit invalid (per field type) → field-level error
- [ ] Submit valid → success state visible within 3s
- [ ] Keyboard-only completion possible (tab through, enter to submit)

For each auth-gated route in `{{auth_routes}}`:
- [ ] Unauthenticated GET → redirect to `/login` (or 401 if API)
- [ ] Authenticated GET → 200, expected content

### Accessibility
- [ ] `pa11y` clean (level=WCAG2AA) on every route in `{{routes}}` — no errors
- [ ] `@axe-core/playwright` integrated into e2e suite — zero violations

### Performance
- [ ] `lighthouse` on each route → performance ≥ 80, accessibility ≥ 95, best-practices ≥ 90 (mobile profile)
- [ ] No image larger than 200KB unless `loading="lazy"` and below fold
- [ ] LCP < 2.5s on every route

### Mobile / responsive
- [ ] At 375px width: no horizontal scroll on any route
- [ ] At 375px width: nav usable (hamburger or persistent)
- [ ] At 375px width: all CTAs tappable (min 44x44 hit area)

### Regression catch
- [ ] Existing unit test suite still passes: `npm test` exits 0
- [ ] No new TypeScript errors anywhere: `tsc --noEmit` clean

## Budget
- `max_minutes`: {{budget_minutes|180}}
- `respawn_count_cap`: 2
- `on_budget_hit`:
  1. Write `pipeline/goal-runs/{{ts}}/handoff.md` listing every check, passing or failing
  2. For each failing check, list: file involved, last attempted fix, why it failed
  3. Update `status.json` with `status: "paused-budget"`
  4. Exit gracefully

## On completion
Update `pipeline/goal-runs/{{ts}}/status.json`:
```json
{
  "status": "complete",
  "verification_results": {
    "all_passed": true,
    "checks": [
      {"name": "<check-name>", "passed": true, "evidence": "<file or screenshot path>"},
      ...
    ]
  },
  "completed_at": "<iso>",
  "elapsed_minutes": <int>
}
```

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

## Important — read before starting
- This is a `/goal` run. You are Codex. You have the goals feature enabled.
- Do NOT ask the user for clarification mid-loop. Make defensible choices and document them in `attempts.log`.
- If a check is ambiguous, pick the stricter interpretation and proceed.
- If a check requires data the app doesn't have (e.g., "test logged-in flow" but no auth implemented), mark it `skipped-no-feature` and move on — don't block.
- Keep changes surgical. Don't refactor unrelated code. Don't reformat. Karpathy rules apply (see `CLAUDE.md`).
