---
name: codex-goal
description: Wrench inside the `router` mechanic. Dispatches long-running Codex loops with template-driven prompts, quantifiable stop conditions, and a hands-free headless path. Use when a task has machine-verifiable success criteria and would otherwise burn hours iterating. Auto-fires on "initialize codex goals", "kick off a codex goal", "comprehensive user testing on X", "lift coverage to N", "make this pass a11y on every page", "bug bash this app".
---

# codex-goal — autonomous loop dispatcher

Take a fuzzy "do this thing autonomously" request, lock it into a `/goal`-ready prompt with quantifiable stop conditions, hand off to Codex's `/goal` loop. **Claude does NOT execute the loop — Codex does.** Claude composes the spec, spawns Codex, watches the status file.

Codex `/goal` is a built-in graceful RALF loop (Codex 0.128.0+, `[features] goals = true` in `~/.codex/config.toml` — already set on the user's box). Per-turn behaviour: Codex runs one turn, reads its hidden `continuation.md` + `budget_limit.md` state, and either keeps going, wraps gracefully near token cap, or marks complete via `update_goal` tool call.

---

## When to use

**Trigger phrases (auto-fire on the no-questions-asked path):**
- "initialize a codex goals loop" / "initialize codex goals" / "kick off a codex goal" / "fire a codex goal" / "spin up codex /goal" / "dispatch codex goal" / "run a codex goal" / "use codex /goal for this" / "set this up as a goal" / "/goal this"
- "comprehensive user testing" / "QA the whole app" / "test this end to end"
- "lift coverage to N%" / "get tests passing" / "test-coverage lift"
- "accessibility audit" / "a11y pass" / "fix all the a11y issues"
- "generate all assets" / "batch image gen" / "create all the sprites"
- "fill all N pages" / "build out the programmatic pages" / "seo page fill"
- "bug bash" / "fix every bug" / "grind on this until tests pass"

When the user says any direct-invocation phrase, **just do it.** Write the plan, spawn the loop, return the goal_id. Don't ask for confirmation, don't enumerate options.

**Auto-fire from project-orchestrator Phase 6 (QA)** when:
- Acceptance criteria are objectively machine-verifiable (build passes, tests pass, lighthouse score threshold, page count, file existence)
- Estimated iteration count > 10 turns
- No human-in-the-loop decision points between iterations

---

## When NOT to use

- Single edit, single function, single bug → just do it in Claude
- Creative judgment required mid-loop (design decisions, copy direction) → use project-orchestrator with human gates
- No machine-verifiable end state → use the `loop` skill with manual review instead
- Plan isn't locked yet → run plan-room first. `/goal` with a fuzzy plan gives mediocre output

---

## Run flow

```
The user says "comprehensive user testing on this app"
  → match the trigger to a template (templates/comprehensive_user_testing.md)
  → fill template with project context (routes, components, forms, test patterns)
  → write goal_prompt.md to <project>/pipeline/goal-runs/<timestamp>/
  → spawn Codex in new Windows Terminal tab in project dir
  → seed Codex with the prompt: codex --enable goals "$(cat goal_prompt.md)"
     (then the interactive /goal activation step — see step 5 note: /goal can't fire from codex exec)
  → write status file <project>/pipeline/goal-runs/<timestamp>/status.json
  → arm 20-min babysit cron (REQUIRED)
  → return thread_id + status file path
  → Claude continues other work
```

---

## Step-by-step

### 1. Match a template

Trigger phrase → template file. Templates lock in the verification criteria so `/goal` knows when to stop.

| Trigger | Template |
|---|---|
| "comprehensive user testing" / "QA end to end" | `comprehensive_user_testing.md` (Playwright + axe + Lighthouse, all pages, all interactive surfaces) |
| "lift coverage to N%" / "test-coverage" | `test_coverage_lift.md` (pytest/vitest coverage to threshold) |
| "accessibility audit" / "a11y" | `accessibility_audit.md` (pa11y/axe clean across all routes) |
| "generate all assets" / "batch image gen" | `asset_generation_batch.md` (image gen pipeline, N unique assets) |
| "fill all N pages" / "programmatic SEO" | `seo_page_fill.md` (programmatic page generation with sitemap regen) |
| "bug bash" / "grind on tests" | `bug_bash.md` (iterate test suite until 0 failures) |
| UI-specific audit | `ui_audit.md` (sizing-first Playwright + visual-eyeball, V1-V14 verification matrix, **headed Chromium**) |
| no match | `generic.md` (forces Claude to interview the user for verification criteria) |

### 2. Lock the plan first

If the user hasn't produced a tight plan, **refuse to dispatch.** Tell them: *"`/goal` with a fuzzy plan wastes Codex hours. Run plan-room first, or invoke `autoplan`. Then I'll dispatch."*

A "tight plan" means: machine-verifiable acceptance criteria, listed routes/components/files in scope, and an explicit stop condition.

### 3. Fill the template

Read CLAUDE.md, plan.md, and scan the repo for:
- Routes (Next.js: `app/**/page.tsx`; Vite: `src/routes/**`; SvelteKit: `src/routes/**/+page.svelte`)
- Forms (any `<form>` or react-hook-form usage)
- Test file patterns (`*.test.ts`, `*.spec.ts`, `tests/**`)
- Lighthouse / pa11y / axe configs
- Build commands, test commands

Substitute into template → `<project>/pipeline/goal-runs/<ts>/goal_prompt.md`.

### 4. Write the status file

```json
{
  "goal_id": "<ts>-<slug>",
  "template": "comprehensive_user_testing",
  "spawned_at": "<iso>",
  "project_dir": "<abs path>",
  "prompt_file": "<abs path>",
  "status": "spawning",
  "budget_minutes": 180,
  "last_check": null,
  "verification_results": null,
  "thread_id": null,
  "respawn_count": 0
}
```

### 5. Spawn Codex

Use the hands-free path by default. It blocks until completion or budget
timeout, captures output, and updates `status.json` without a TUI keystroke.

```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/dispatch.py" \
  --project-dir "<project>" \
  --prompt-file "<project>/pipeline/goal-runs/<ts>/goal_prompt.md" \
  --status-file "<project>/pipeline/goal-runs/<ts>/status.json" \
  --headless
```

Omit `--headless` only when the user explicitly wants a visible Windows Terminal
session. That path seeds the TUI and requires `/goal use goal` interactively.
The headless path runs `cx long-unattended-build workspace-write` (model and effort from that lanes.json route) with
the plan inlined, so it needs no slash command and writes `codex-exec.log` plus `last-message.txt` in the run folder.

### 6. Arm the babysit cron for visible TUI runs

The user's standing instruction (2026-05-11): *"make sure it's doing a good job,
you are its baby sitter."* After every successful visible TUI dispatch, schedule
a recurring babysit check. Do not create a duplicate watcher for a blocking
`--headless` run; the dispatcher owns its timeout and state transitions.

**Reconciliation with AGENTS.md hard rule #1 ("no timers, no cron"):** a babysit cron armed here is *triggered autonomy*, not background spend — it exists only as a downstream effect of a user-triggered `/goal` dispatch and the user's standing 2026-05-11 babysit instruction, and it MUST stand down at goal outcome (step 8). It is the sanctioned carve-out to rule #1, not a violation of it.

Default cadence: **every 20 minutes**. Cron: `7,27,47 * * * *` (off-:00 spacing to avoid fleet collision).

The babysit prompt does:
1. Reads `status.json` and the goal-run's `attempts.log`
2. Confirms the Codex node process is alive (PID, CPU seconds, uptime via `Get-CimInstance Win32_Process`)
3. Lists files modified in the last 25 min (in the goal-run dir AND in `lib/`, `app/`, `tests/`, `src/` of the project)
4. Compares the verification matrix to the prior sweep — what moved pending→passed, what's in_progress, what looks stuck
5. Runs the changes-relay step (see § Changes relay)
6. Surfaces a one-line status

### 7. The IRON RULE — never kill the Codex process

**NEVER kill or restart the Codex process during babysitting.** If it looks stalled, surface to the user and **ASK first.** A working goal once got killed mid-run because low CPU was misread as "stuck" — that mistake must not repeat.

Codex is frequently network-bound (zero CPU for minutes between API calls). Low CPU alone is **not** evidence of stall.

**Real stall signals (require ALL three for 30+ min before even asking the user):**
- `status.json` last_check timestamp untouched
- `attempts.log` frozen (no new entries)
- No file modifications anywhere in the project working dirs

Even then, ask the user before any kill/restart action. Default to "keep watching" unless the user says otherwise.

### 8. Changes relay step

After every status/process check, run:

```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/relay_changes.py" --check <run_dir>
```

Exit codes:
- `0` → an unrelayed `changes.md` is sitting in the run dir (Codex finished or partial-finished). Relay it.
- `2` → a `changes.md` exists but was already relayed. Skip.
- `1` → no `changes.md` yet. Codex is still working. Keep babysitting.

If exit 0, relay:

```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/relay_changes.py" --relay <run_dir>
```

The script prints a **single line**: `[OK] <one-sentence outcome>`. The user's standing instruction (2026-05-12): do NOT enumerate files, steps, or verification details. Tell the user exactly the single line and nothing else. If they want the full manifest, they'll ask — the file is at `<run_dir>/changes.md`.

After relaying, stand down the babysit cron if outcome ∈ (completed, failed, aborted) and the user hasn't asked you to keep watching.

### 9. Handoff message

```
Goal dispatched: <goal_id>
Template: <template_name>
Status file: <abs path>
Codex window: new wt tab in <project_dir>
Babysit cron armed: <cron_job_id> firing every 20 min
```

Then move on.

---

## Headed Playwright for UI checks (default)

**Whenever a dispatched Codex goal runs Playwright against UI surfaces** (template `ui_audit`, `comprehensive_user_testing`, or any hand-authored prompt that drives Chromium), the launch MUST be **headed** with **slowMo** so the user can watch live. Workers MUST be `1`.

Applies to: dedicated UI audits, comprehensive user testing when it includes visual checks, accessibility audits that screenshot pages, any hand-authored prompt where Codex spawns `chrome-headless-shell.exe` or `@playwright/test`.

**Headed-mode contract** (must appear verbatim in `goal_prompt.md`):
- `playwright.config.ts` at project root with:
  ```ts
  use: {
    headless: false,
    slowMo: 200,
    screenshot: "on",
    video: "retain-on-failure",
    trace: "retain-on-failure"
  }
  ```
  and top-level `workers: 1`, `fullyParallel: false`
- Spec invocation: `npx playwright test <spec> --headed --workers=1`
- Spec authors must NOT bypass with `chromium.launch({ headless: true })`

Pure backend / non-UI goals (test coverage lift on backend, asset generation, bug bash on non-UI suites) are unaffected — they don't run Chromium.

---

## Status file schema (for dashboard / external readers)

```json
{
  "goal_id": "string",
  "template": "string",
  "spawned_at": "iso8601",
  "project_dir": "abs path",
  "prompt_file": "abs path",
  "status": "spawning|running|paused|completed|failed|aborted",
  "budget_minutes": "int",
  "last_check": "iso8601 | null",
  "verification_results": {
    "all_passed": "bool",
    "matrix": [{"id": "V1", "status": "passed|in_progress|pending|failed", "evidence": "..."}]
  },
  "thread_id": "string | null",
  "respawn_count": "int"
}
```

External readers (dashboards, project-orchestrator Phase 6 gate-keeper) poll this file for progress.

---

## No-self-rescue for goals

If a goal run hits its budget AND the verification matrix still has failing checks, do **NOT** auto-respawn with the same prompt. Write `awaiting-human.md` in the goal-run dir listing the failing checks + handoff report, surface to the user.

Counter persists in `status.json.respawn_count`. **Hard cap: 2.** After 2 respawns, escalate to the user.

---

## Shared activity log (Claude ⇄ Codex)

Every project gets a `<project_root>/.claude-codex-log.md` file that both Claude and Codex append to. Cheap audit trail — one line per action, ~80–200 tokens to tail.

**Claude side:** the PostToolUse hook `~/.claude/hooks/codex-claude-shared-log.ps1` appends a line after every Edit / Write / NotebookEdit / Bash / PowerShell call.

**Codex side:** templates instruct Codex to call `shared_log.py append` at end of every turn.

**Read it from Claude side** before any work in a project where a goal is running:

```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py" tail --project <dir> --lines 20
```

Gives the joint timeline in <600 tokens.

---

## See also

- [../SKILL.md](../SKILL.md) — the router mechanic (when to fire which wrench)
- [codex.md](codex.md) — synchronous Codex modes (review / challenge / consult)
- Templates and scripts live at `$HOME/.claude\skills\codex-goal-dispatcher\` (the live `codex-goal-dispatcher` skill — `templates/` and `scripts/`). This wrench dispatches to that skill's surface; it does not keep its own copy.
