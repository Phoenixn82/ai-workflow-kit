---
name: ui_audit
triggers: ["ui audit", "ui sizing audit", "playwright ui check", "audit the ui", "ui pass with playwright", "sizing-first audit", "visual quality audit"]
budget_default_minutes: 180
required_context_files: ["CLAUDE.md"]
verification_kind: machine
---

# Goal: UI / Visual-Quality Audit — {{project_name}}

Playwright-driven UI audit across the full viewport matrix. Catches overflow, clipping, hero clipping, undersized trust strips, flattened heading hierarchies, CTA prominence regressions, and per-screenshot "this looks wrong" issues.

**App context:**
- Project root: `{{project_dir}}`
- Stack: {{stack}}
- Routes detected: {{routes}}
- Branch: `{{branch}}`

## NON-NEGOTIABLE: Headed Chromium for ALL Playwright runs

The user watches the runs live. Every Playwright invocation in this goal MUST run **headed** (`headless: false`) with **slow motion** so the Chromium window is observable. Workers must be `1` so only one Chromium window pops at a time — parallel headed runs overwhelm the screen.

**Required `playwright.config.ts` at project root** (create if missing — do not overwrite if the project already has one; merge instead):

```ts
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    headless: false,
    slowMo: 200,
    screenshot: "on",
    video: "retain-on-failure",
    trace: "retain-on-failure",
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
  },
});
```

**Required CLI invocation** when running the spec from within the goal:
```
npx playwright test tests/ui-audit/<spec>.spec.ts --headed --workers=1
```

If you bypass this (e.g. running `chromium.launch({ headless: true })` directly in the spec), you violate the contract. No exceptions for "faster CI mode" — this goal is for the user to watch.

## Operating constraints (NON-NEGOTIABLE)

1. **DO NOT touch the global font scale.** `html { font-size: 130% }` (if present in `src/app/globals.css`) is intentional. Do not shrink it.
2. **DO NOT change image file extensions.** `.webp` stays `.webp`. `.png` stays `.png`.
3. **DO NOT replace brand colors with neutral grays.** Brand accent colors are intentional.
4. **DO NOT remove sections.** If a section is sized wrong, FIX the sizing; never delete the section.
5. **DO NOT touch `src/app/dashboard/**` or `src/app/api/**`.** Out of scope.
6. **Minimum-diff edits.** Karpathy guidelines: touch only what is broken. Every changed line must trace to a sizing/visual bug you can name with a screenshot.
7. **If a fix requires deleting brand identity or content, the fix is wrong.** Find a different fix.

## Viewport matrix (6 sizes × every route)

| Name | Width | Height | Profile |
|------|-------|--------|---------|
| mobile-sm | 375 | 812 | iPhone SE-class |
| mobile-lg | 430 | 932 | iPhone 15 Pro Max |
| tablet | 768 | 1024 | iPad portrait |
| desktop | 1280 | 800 | Laptop |
| desktop-lg | 1440 | 900 | Common laptop |
| desktop-xl | 1920 | 1080 | Large monitor |

Routes: auto-discover from `src/app/**/page.tsx` (skip `dashboard/`, `api/`).

## Verification matrix — V1–V14

V1–V8 (mechanical):
- V1 No horizontal scroll at any viewport
- V2 No clipped text (`scrollWidth > clientWidth` AND `overflow: hidden|clip`)
- V3 No element extends beyond viewport bounds
- V4 No unintentional overlap of visible text blocks
- V5 Font-size sanity: no computed font-size < 11px on visible text
- V6 Section containment: section children's bboxes contained within section
- V7 Console clean (tolerate transient Next dev 500s with retry)
- V8 Screenshot captured per (route, viewport) pair

V9–V13 (sizing-quality):
- **V9 — Hero above-the-fold (CRITICAL).** Entire hero (h1 + subhead + primary CTA button) fully visible inside `[0, viewport.height]` without scrolling, at every viewport.
- **V10 — Trust/press strip readability.** "As featured in / Trusted by" logos rendered ≥ **40px** tall on desktop (≥1280) and ≥ **28px** tall on mobile (<768).
- **V11 — Heading hierarchy.** Computed font-size of any h1 ≥ 1.4× largest h2 on same page; h2 ≥ 1.2× largest h3.
- **V12 — Minimum heading sizes at viewport.**
  - Hero h1 (first h1 in first section): ≥ 40px mobile, ≥ 56px tablet, ≥ 64px desktop, ≥ 72px desktop-xl.
  - Other h1: ≥ 32px mobile, ≥ 44px tablet, ≥ 52px desktop.
  - h2: ≥ 24px mobile, ≥ 32px tablet, ≥ 36px desktop.
  - Section eyebrow labels: ≥ 11px and font-weight ≥ 600.
- **V13 — CTA button prominence.** Primary CTAs (bg-brand-orange / bg-brand-green / cta-pulse / text matching /quote|call|text|book|get started|schedule/i):
  - Computed font-size ≥ 14px mobile, ≥ 16px desktop.
  - Computed height ≥ 44px (Apple HIG tap target).
  - `getBoundingClientRect().right ≤ window.innerWidth`.
  - In hero sections, contrast ≥ 4.5:1 (WCAG AA).

V14 (eyeball pass):
- **V14 — Per-screenshot eyeball pass.** For every captured screenshot, examine the image and answer four questions in `eyeball-notes.md`:
  1. Is anything compositionally **tiny** that should be prominent? (logos, headings, CTAs)
  2. Is anything **clipped, cut off, or cropped** awkwardly?
  3. Are proportions **awkward or imbalanced**?
  4. Any other "this just looks wrong" observation a designer would call out?
  Severity: P1 (must-fix) / P2 (should-fix) / P3 (polish).

## Phases

**A. Bootstrap.** Auto-discover routes. Verify `bun run dev` (or `npm run dev`) is up. Create/merge `playwright.config.ts` with headed + slowMo settings. Create `tests/ui-audit/audit.spec.ts` with V1-V14 checks.

**B. Baseline sweep.** Run V1-V14 across all (route × viewport) pairs via `npx playwright test --headed --workers=1`. Write `baseline-report.md` + `eyeball-notes.md`.

**C. Fix loop.** Address every P1 + P2 finding in priority order:
  1. V9 hero above-fold
  2. V10 trust strip readability
  3. V12 minimum heading sizes
  4. V13 CTA prominence
  5. V11 hierarchy
  6. V14 P1 items
  7. V14 P2 items
  8. V1-V8 regressions

After each fix, re-run only the affected (route × viewport × check) cells. Keep `attempts.log`.

**D. Stress sweep.** Tailwind breakpoint edges (639/640/767/768/1023/1024/1279/1280), long content, form abuse (500-char paste), language toggle EN↔ES if i18n exists.

**E. Build gate.** `bun run build` (or `npm run build`) exit 0.

**F. Commit + final report.** Commit on the audit branch with message `ui-audit: <summary>`. Do NOT push.

## Status file emission (continuous)

Update `pipeline/goal-runs/<ts>/status.json` at every checkpoint with phase, status, current, per-check counts (V1-V13 pass/fail, V14 P1/P2/P3 open/fixed), all_passed, build_exit_code, attempts, last_file_modified, last_update_iso. `all_passed: true` requires V1-V13 zero-fail, V14 P1+P2 fully fixed, build_exit_code 0.

## Deliverables

1. `baseline-report.md`
2. `eyeball-notes.md`
3. `attempts.log`
4. `final-report.md`
5. `results.json`
6. `screenshots/<route>__<viewport>.png` (baseline + final)
7. `playwright-report/` (HTML, generated by reporter)
8. One commit on the audit branch.
9. `status.json`: status=complete, all_passed=true, build_exit_code=0.

## Coordination with Claude (REQUIRED every turn)

A shared activity log lives at `{{project_dir}}/.claude-codex-log.md`. Claude appends to it automatically after every Edit/Write/Bash. You must append at end-of-turn so Claude sees what you did.

**At the START of every turn**, tail recent Claude activity:
```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py" tail --project "{{project_dir}}" --lines 15
```

**At the END of every turn**, emit one summary line (<180 chars):
```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py" append --project "{{project_dir}}" --actor codex --action turn --summary "<verb> <object> — <outcome>"
```

## Tone / philosophy

This is a **visual-quality** pass, not a metric-chasing pass. V14 eyeball findings are equally important as V9-V13 thresholds. When in doubt between two valid fixes, pick the one that makes the screenshot look **more designed** — bigger heading, more breathing room, clearer hierarchy — over the one that makes a number turn green. The user is a designer-in-chief reviewing screenshots. Be ruthless. Find embarrassing tiny strips, cropped heroes, flattened hierarchies. Fix them with surgical minimum-diff edits.

The user is watching the Chromium window. Make the moves visible.
