---
name: always_on_watcher
triggers: ["always-on watcher", "continuous frontend testing", "watch the frontend", "keep testing this"]
budget_default_minutes: 30
required_context_files: ["CLAUDE.md"]
verification_kind: continuous
mode: watch
cadence: "30m"
---

# Goal: always-on frontend watcher — {{project_name}}

> **Watch mode.** This is NOT a finite goal. It runs on a cadence (every {{cadence}}) to sweep the
> frontend, append findings to `feedback.md`, and read pending edits from `goal_edits.md`. Each fire
> is one short Codex run — accumulates state across runs via the goal-run dir.

## Plan
Run the frontend checks below in desktop + mobile viewports. Report deltas vs the last run.
Do NOT fix problems automatically — this is a verification loop, not a fix loop. Findings only.

**App context:**
- Project root: `{{project_dir}}`
- Stack: {{stack}}
- Dev server: `{{dev_command|npm run dev}}` on port `{{dev_port|3000}}`
- Routes: {{routes}}

**Per-fire procedure:**
1. Read `goal_edits.md` if it exists — these are user-injected adjustments. Apply them to your checklist.
2. Start the dev server if not already running. Wait for ready.
3. Launch Playwright with two browser contexts in parallel:
   - **Desktop:** 1440×900, no device emulation
   - **Mobile A:** `devices['iPhone 14 Pro']` (390×844, touch)
   - **Mobile B:** `devices['Pixel 7']` (412×915, touch)
4. Run the checklist below against each route, in each context.
5. Diff findings against the last `feedback.md` entry — only report NEW findings + regressions.
6. Append a timestamped block to `feedback.md` (see format below).
7. Update `status.json`: `last_check = <iso>`, `findings_count = <total since last green sweep>`.
8. Exit (do not loop inside this fire — the scheduler fires the next sweep).

## Checklist (per route, per context)

### Page health
- [ ] Page reaches DOMContentLoaded < 3s, fully loaded < 5s
- [ ] No console errors (warnings OK, log them)
- [ ] No 4xx/5xx network requests except known acceptable ones (list in goal_edits.md to suppress)
- [ ] No layout shift > 0.1 CLS

### Above-the-fold
- [ ] Primary CTA visible in initial viewport (no scrolling required)
- [ ] H1 (or equivalent main heading) visible in initial viewport
- [ ] No content cut off by browser chrome

### Interactive surfaces
- [ ] Every visible button has a discernible name
- [ ] Click primary CTA → navigates OR state-changes within 2s
- [ ] All visible links have hrefs (no `href="#"` without click handler)

### Mobile-specific (only in mobile contexts)
- [ ] No horizontal scroll at 390px / 412px width
- [ ] Hit-areas ≥ 44×44 on every interactive element
- [ ] Nav usable (hamburger reachable + opens, OR persistent nav fits)
- [ ] Forms: keyboard appears on input focus, doesn't cover the input

### Forms (if present on route)
- [ ] Required fields show validation on empty submit
- [ ] Invalid input shows field-level error
- [ ] Valid submit reaches success state within 3s

### Accessibility (axe-core run inline)
- [ ] No serious or critical violations from `@axe-core/playwright`
- [ ] Focus order is sensible (tab through → reaches every interactive in visual order)

### Visual regression (only if baseline exists in `<run>/baseline-screenshots/`)
- [ ] No more than 0.1% pixel diff vs baseline per route/context

## Feedback format

Append to `<run>/feedback.md` every fire:

```markdown
## {{iso_timestamp}} — sweep #{{sweep_number}}

**Routes checked:** {{N}}/{{total}}
**Contexts:** desktop, iPhone 14 Pro, Pixel 7
**Status:** {{green | regressions | new-failures}}

### New findings ({{count}})
- [{{severity}}] `/route` ({{context}}) — {{one-line description}}
  - Repro: {{playwright snippet or steps}}
  - Suggested fix: {{one-liner if obvious; otherwise "needs investigation"}}

### Regressions vs last sweep ({{count}})
- [{{severity}}] `/route` ({{context}}) — was passing in sweep #{{prev}}, now: {{description}}

### Resolved since last sweep ({{count}})
- ✓ `/route` ({{context}}) — {{old finding}} no longer reproduces

### Still failing (carried from sweep #{{prev}})
- [{{severity}}] `/route` ({{context}}) — open for {{N}} sweeps now
```

## Goal-edits format

`<run>/goal_edits.md` is YOUR input channel from the user. Read every fire. Format:

```markdown
## Edit at {{iso}}
{{instruction}}

## Edit at {{iso}}
{{instruction}}
```

Examples the user might write:
- "Suppress the mobile keyboard-nav check on /admin — it's intentional"
- "Add a check that the cart total updates within 500ms of qty change"
- "Skip the Pixel 7 context until we ship phase 5"
- "Promote the LCP check to severity=critical"

Apply each edit and persist your interpretation back to `<run>/goal_edits_applied.md` so the user can verify you read it correctly.

## Budget
- `max_minutes_per_fire`: {{budget_minutes|30}}
- `cadence`: {{cadence|30m}} (recurring via `/loop`)
- `on_budget_hit`: graceful — write what's done to feedback.md, exit. Next fire picks up.

## Status states
- `watching` — normal steady-state, runs scheduled
- `paused` — the user stopped the cadence
- `regression` — sweep found a new failure vs last green
- `green` — last sweep clean

## Coordination with Claude (REQUIRED every fire)

A shared activity log lives at `{{project_dir}}/.claude-codex-log.md`. Claude appends to it automatically after every Edit/Write/Bash via a PostToolUse hook. You must append at end-of-fire so Claude sees what you did. Both sides stay aware via this single file — no extra polling needed.

**At the START of every fire**, tail recent Claude activity:
```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py" tail --project "{{project_dir}}" --lines 15
```

**At the END of every fire**, emit one summary line (<180 chars):
```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py" append --project "{{project_dir}}" --actor codex --action sweep --summary "<sweep #N — N findings, N regressions, N resolved>"
```

## Important — read before each fire
- This is YOU running on a schedule, not a long-running process. Each fire is independent. State lives in files.
- Findings accumulate. Don't re-report items already in `feedback.md` unless they regressed.
- If Playwright isn't installed or dev server won't start, write that to feedback.md and exit cleanly — don't burn the budget retrying.
- Do NOT fix problems. The fix loop is a separate goal (`comprehensive_user_testing` template). This template only watches.
- Native mobile (iOS/Android apps): out of scope here. This template covers web responsive only. Native apps need Appium — flag in feedback.md if the project has a native target.
