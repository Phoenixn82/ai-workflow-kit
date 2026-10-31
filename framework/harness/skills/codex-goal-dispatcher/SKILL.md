---
name: codex-goal-dispatcher
description: Dispatch long-running autonomous loops to Codex's /goal feature with the right verification gates. Use when a task has clear quantifiable success criteria and would otherwise burn hours of Claude time iterating — comprehensive user testing, test-coverage lift, accessibility audit, batch asset generation, large-volume page fill, bug bash. Auto-fire trigger phrases include "initialize a codex goals loop", "initialize codex goals", "kick off a codex goal", "fire a codex goal", "spin up codex /goal", "dispatch codex goal", "run a codex goal", "comprehensive user testing on X", "lift coverage to 90", "make this pass a11y on every page", "generate all assets for this game", "bug bash this app". When the user says any of these, fire this skill instead of grinding it out in Claude. Auto-fires from project-director Phase 6 (QA) when scope + criteria match a registered template.
---

> **Linux:** `scripts/shared_log.py` and `scripts/relay_changes.py` are cross-platform, so the
> Claude<->Codex bridge in global CLAUDE.md works.
> **NOT PORTED:** `scripts/dispatch.py` is Windows-coupled (finds `codex.exe` under `%LOCALAPPDATA%`,
> writes a CRLF `.bat` launcher, spawns it through `powershell -NoExit`, calls `aos_lock.py` at a Windows
> path). Long unattended `/goal` runs stay unavailable until it is ported to `codex` on PATH + a POSIX
> launcher script + a terminal spawn that works under Hyprland.

# Codex /goal dispatcher

Single purpose: take a fuzzy "do this thing autonomously" request, lock it into a `/goal`-ready prompt with quantifiable stop conditions, and hand off to Codex's `/goal` loop. Claude does NOT execute the loop — Codex does. Claude composes the spec, spawns Codex, watches the status file.

## When to use

Trigger phrases (auto-fire):
- **Direct invocation:** "initialize a codex goals loop", "initialize codex goals", "kick off a codex goal", "fire a codex goal", "spin up codex /goal", "dispatch codex goal", "run a codex goal", "use codex /goal for this", "set this up as a goal", "/goal this"
- "comprehensive user testing", "QA the whole app", "test this end to end"
- "lift coverage to N%", "get tests passing", "test-coverage lift"
- "accessibility audit", "a11y pass", "fix all the a11y issues"
- "generate all assets", "batch image gen", "create all the sprites"
- "fill all N pages", "build out the programmatic pages", "seo page fill"
- "bug bash", "fix every bug", "grind on this until tests pass"

When the user says any direct-invocation phrase, just do it — write the plan, spawn the loop, return the goal_id. Don't ask for confirmation, don't enumerate options. The skill is the answer.

Project-director auto-fire conditions (from Phase 6 — QA):
- Acceptance criteria are objectively machine-verifiable (build passes, tests pass, lighthouse score, page count, file existence)
- Estimated iteration count > 10 turns
- No human-in-the-loop decision points between iterations

## When NOT to use

- Single edit, single function, single bug → just do it in Claude
- Creative judgment required mid-loop (design decisions, copy direction) → use project-director with human gates
- No machine-verifiable end state → use `/loop` skill with manual review instead
- Plan isn't locked yet → run `/plan` or plan-mode first; `/goal` with a fuzzy plan gives mediocre output (Chase AI's own warning)

## How it works

Codex's `/goal` is a built-in graceful RALF loop (Codex 0.128.0+, `[features] goals = true` in `~/.codex/config.toml` — already set on the user's box). Verified 2026-05-11.

Per-turn behavior:
1. Codex runs one turn, reads `continuation.md` + `budget_limit.md` (hidden internal state)
2. At end of turn, four paths:
   - Work left + budget OK → keep going
   - Near token cap → inject `budget_limit.md`, wrap gracefully, emit handoff
   - Complete → `update_goal` tool call, audit deliverables, mark done
   - Pause/edit/crash → recover via state file
3. Each `/goal` run is bound to ONE Codex thread. Parallel goals = parallel threads = new terminals.

## Run flow

```
The user says "comprehensive user testing on this app"
  → dispatcher matches trigger
  → loads templates/comprehensive_user_testing.md
  → fills with project context (routes, components, forms)
  → writes goal_prompt.md to <project>/pipeline/goal-runs/<timestamp>/
  → spawns Codex in new Windows Terminal tab in project dir
  → seeds with: codex --enable goals "/goal use goal: $(cat goal_prompt.md)"
  → writes status file <project>/pipeline/goal-runs/<timestamp>/status.json
  → returns thread_id + status file path to Claude
  → Claude continues other work; dashboard polls status.json for progress
```

## Steps

### Step 1 — Match a template

Use `templates/index.md` to map trigger phrase → template file. If no match, fall back to `templates/generic.md` which forces Claude to interview for verification criteria before writing the prompt.

### Step 2 — Lock the plan

If the user hasn't already produced a tight plan, REFUSE to dispatch. Tell them: *"`/goal` with a fuzzy plan wastes Codex hours. Run plan mode first, or invoke `/autoplan`. Then I'll dispatch."*

If plan exists (in `<project>/plan.md`, work order, or PRD), proceed.

### Step 3 — Fill the template

Read the project's CLAUDE.md, plan.md, and scan the repo for:
- Routes (Next.js: `app/**/page.tsx`, Vite: `src/routes/**`)
- Forms (any `<form>` or react-hook-form usage)
- Test file patterns (`*.test.ts`, `*.spec.ts`)
- Lighthouse / pa11y / axe configs

Substitute into template. Output: `<project>/pipeline/goal-runs/<ts>/goal_prompt.md`.

### Step 4 — Write the status file

```json
{
  "goal_id": "<ts>-<slug>",
  "template": "comprehensive_user_testing",
  "spawned_at": "<iso>",
  "project_dir": "<abs>",
  "prompt_file": "<abs>",
  "status": "spawning",
  "budget_minutes": 180,
  "last_check": null,
  "verification_results": null,
  "thread_id": null
}
```

### Step 5 — Spawn Codex

Use the dispatch script. The default operational path is hands-free: it runs
`cx long-unattended-build workspace-write` (model and effort from that route in
`_system/lanes.json`; `route long-unattended-build` shows them) to completion,
captures output, and drives `status.json` without a TUI keystroke:

```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/dispatch.py" \
  --project-dir "<project>" \
  --prompt-file "<project>/pipeline/goal-runs/<ts>/goal_prompt.md" \
  --status-file "<project>/pipeline/goal-runs/<ts>/status.json" \
  --headless
```

With `--headless`, the script inlines the plan into a non-interactive Codex
prompt, blocks until exit or budget timeout, captures `codex-exec.log` and
`last-message.txt`, and writes the terminal state. Omit `--headless` only when
The user explicitly wants a visible Windows Terminal session; that legacy path
seeds the TUI and still requires `/goal use goal` interactively.

### Step 6 — Arm the babysit loop (REQUIRED — non-skippable)

After every successful **visible TUI** dispatch, Claude **MUST** schedule a
recurring babysit check via CronCreate before reporting completion. The user's
standing instruction (2026-05-11): *"make sure its doing a good job, you are
its baby sitter."* The blocking `--headless` path owns process supervision and
terminal-state updates itself; do not create a duplicate watcher for it.

> **Carve-out vs AGENTS hard rule #1 / "no background spend":** the goal-run babysit cron is a user-sanctioned exception to the no-autonomous-timers rule. It is armed ONLY as a downstream effect of a user-triggered dispatch (never standalone) and is stood down at goal end (see CHANGES-RELAY step: stand down on completed/failed/aborted). The bridge map doc records the matching note on its side.

Default cadence: **every 20 minutes**. Cron: `7,27,47 * * * *` (off-:00 to avoid fleet collision, ~20-min spacing). Override only when the user explicitly asks for a different cadence.

The babysit prompt template:
```
Babysit check on Codex goal <goal_id>. Read <status.json> and <attempts.log>.
Confirm the Codex process is alive — capture PID, CPU seconds, uptime via:
  Get-CimInstance Win32_Process -Filter "Name='codex.exe'" |
    Where-Object { $_.CommandLine -match 'goals' }
  (Modern Codex is a native codex.exe, NOT node.exe + codex.js. status.json's node_pid/codex_pid
   field already holds the resolved codex.exe PID from dispatch.py; prefer that when present.)
List files modified in the last 25 min: in the goal-run dir AND in the project's
working dirs (lib/, app/, tests/, src/ — depending on stack).
Compare the verification matrix to the prior sweep: which B<n> moved
pending→passed, which are in_progress, which look stuck?
Surface (1) one-line status — "B1-B5 passed, B6 in progress, on track" or
"stalled at B3, 25 min no progress"; (2) any anomaly (CPU flat at 0 for 20+
min AND no file mods AND status.json untouched = stalled — but treat low CPU
alone as fine, codex is API-bound and idle between turns).

IRON RULE: NEVER kill or restart the Codex process. If you believe it is
stalled, surface to the user and ASK first. A working goal once got killed mid-
run because low CPU was misread as "stuck" — that mistake must not repeat.

CHANGES-RELAY STEP (always run after the status/process check):
  python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/relay_changes.py" --check <run_dir>
  → exit 0 means an unrelayed changes.md is sitting in the run dir; Codex
    just finished (or partial-finished) and wrote its hand-back manifest.
  → exit 2 means a changes.md exists but was already relayed; skip.
  → exit 1 means no changes.md yet; Codex is still working, just keep babysitting.
  If exit 0:
    python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/relay_changes.py" --relay <run_dir>
  The script now prints a SINGLE LINE: "[OK] <one-sentence outcome>".
  The user's standing instruction (2026-05-12): do NOT enumerate files,
  steps, or verification details to them after babysitting. Tell them exactly
  the single line the script printed and nothing else. If they want the full
  changes manifest, they will ask — the file is at <run_dir>/changes.md.
  After relaying, stand down the babysit cron if outcome ∈
  (completed, failed, aborted) and the user hasn't asked you to keep watching.
```

The IRON RULE is non-negotiable. Codex is frequently network-bound (zero CPU for minutes between API calls). Low CPU alone is **not** evidence of stall. Real stall signals: status.json untouched AND attempts.log frozen AND no file mods in the project — ALL three for 30+ min — and even then, ASK before acting.

### Step 7 — Hand off

Claude returns control to the user with:
```
Goal dispatched: <goal_id>
Template: <template_name>
Status file: <abs_path>
Codex window: new wt tab in <project_dir>
Babysit cron armed: <cron_job_id> firing every 20 min
```

Then Claude moves on. The cron handles polling. The agentic_os dashboard reads `pipeline/goal-runs/**/status.json` for goal tiles.

## Headed Playwright for UI checks — DEFAULT (the user instruction, 2026-05-12)

**Whenever a dispatched Codex goal runs Playwright against UI surfaces** (template `ui_audit`, `comprehensive_user_testing`, or any hand-authored prompt that drives Chromium against the app), the launch MUST be **headed** with **slowMo** so the user can watch the run live. Workers must be `1` — parallel headed runs overwhelm the screen.

This applies to:
- Dedicated UI audits (`ui_audit.md`)
- Comprehensive user testing when it includes visual checks (`comprehensive_user_testing.md`)
- Accessibility audits when they screenshot pages (`accessibility_audit.md`)
- Any hand-authored prompt where Codex spawns `chrome-headless-shell.exe` or `@playwright/test`

Headed-mode contract (must appear in the goal_prompt.md):
- `playwright.config.ts` at project root with `use: { headless: false, slowMo: 200, screenshot: "on", video: "retain-on-failure", trace: "retain-on-failure" }` and top-level `workers: 1`, `fullyParallel: false`
- Spec invocation: `npx playwright test <spec> --headed --workers=1`
- Spec authors must NOT bypass with `chromium.launch({ headless: true })` — the config-file contract is the canonical setting

Pure backend / non-UI goals (test coverage lift, asset generation, bug bash on non-UI test suites) are unaffected — they don't run Chromium.

When hand-writing a goal_prompt.md without a template (the rare case), copy the "NON-NEGOTIABLE: Headed Chromium" block from `templates/ui_audit.md` verbatim.

## Templates

See `templates/`:
- `ui_audit.md` — Sizing-first Playwright + visual-eyeball audit, **headed Chromium by default** (V1-V14 verification matrix)
- `comprehensive_user_testing.md` — Playwright + axe + Lighthouse, all pages, all interactive surfaces
- `test_coverage_lift.md` — pytest/vitest coverage to threshold
- `accessibility_audit.md` — pa11y/axe clean across all routes
- `asset_generation_batch.md` — image gen pipeline, N unique assets
- `seo_page_fill.md` — programmatic page generation with sitemap regen
- `bug_bash.md` — iterate test suite until 0 failures
- `generic.md` — fallback that forces verification-criteria interview

Adding a template: create `templates/<name>.md` with frontmatter:
```yaml
---
name: <slug>
triggers: ["phrase 1", "phrase 2"]
budget_default_minutes: 180
required_context_files: ["plan.md", "CLAUDE.md"]
verification_kind: machine  # or "human"
---
```

Then add an entry to `templates/index.md`.

## No-self-rescue counter

Same convention as `three-brain-router`. If a goal run hits budget AND the verification matrix still has failing checks, do NOT auto-respawn with the same prompt — write `awaiting-human.md` in the goal-run dir, list the failing checks + handoff report, surface to the user.

Counter persists in status.json `respawn_count`. Hard cap: 2. After 2, escalate.

## Integration: project-director Phase 6 (QA)

`project-director` SKILL.md references this skill from the QA phase. When the QA phase's acceptance criteria match `comprehensive_user_testing` template, project-director's QA gate-keeper invokes:

```
Skill(codex-goal-dispatcher, args="comprehensive user testing on <project>")
```

The dispatcher returns the goal_id; project-director writes it to `progress.md` and advances to the next non-blocked phase (parallel tracks). Phase 6 itself doesn't close until status.json shows `verification_results.all_passed == true`.

## Integration: dashboard

`agentic_os` dashboard reads `~/Projects/*/pipeline/goal-runs/**/status.json` for the "Autonomous Loops" tile row. Tile fields: goal_id, template, elapsed, last_check, status, "open Codex" link (opens the source terminal session).

## Triggers vs `/loop`

- Use `/loop` when you want a prompt fired repeatedly on a clock (every 5 min, check the deploy)
- Use THIS skill when you want one autonomous Codex run that grinds until done, with budget + verification baked in
- They compose: `/loop 30m /codex-goal-dispatcher status check <goal_id>` to babysit a running goal from Claude side

## Shared activity log (Claude ⇄ Codex)

Every project gets a `<project_root>/.claude-codex-log.md` file that both sides append to. It is the single source of truth for "what did the other agent just do." Designed to be cheap: one line per action, ~80–200 tokens to tail.

**Claude side** — automatic. A PostToolUse hook (`~/.claude/hooks/codex-claude-shared-log.sh`, registered globally in `~/.claude/settings.json`) appends one line after every Edit / Write / NotebookEdit / Bash / PowerShell call:
```
[2026-05-12T01:48Z] claude edit — edited src/components/Header.tsx
[2026-05-12T01:51Z] claude run — ran: npm run build
```

**Codex side** — every active template (`comprehensive_user_testing`, `bug_bash`, `accessibility_audit`, etc.) instructs Codex to:
1. **Start-of-turn:** `python shared_log.py tail --project <dir> --lines 15` and reconcile if Claude touched the same surface.
2. **End-of-turn:** `python shared_log.py append --project <dir> --actor codex --action turn --summary "<verb> <object> — <outcome>"`.

The `always_on_watcher` template uses `--action sweep` instead of `--action turn`.

**Read it from Claude side** whenever you're entering a project a Codex goal is running in — `python ~/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py tail --project <dir> --lines 20` gives you the joint timeline in <600 tokens. Do this at session start in any project, before any non-trivial work that touches files Codex might be in.

**Rotation:** file is capped at 100 lines (head-truncated). Format and tooling lives in `scripts/shared_log.py`.

## Verification this skill itself works

Smoke test (one-shot):
```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/dispatch.py" --smoke-test
```
Writes a fake goal-run dir, opens Codex with a trivial prompt ("/goal print hello world to a file then mark complete"), verifies status.json gets written. Cleans up after.

## Source

Built 2026-05-11 in response to Chase AI's "Codex Just Became THE BEST Long Running Agentic Harness" video. Codex `/goal` is the underlying feature; this skill is the Claude-side wrapper that decides WHEN to use it and writes the right prompts to make it terminate.
