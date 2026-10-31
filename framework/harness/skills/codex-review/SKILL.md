---
name: codex-review
description: >-
  Run a bounded adversarial review of an existing implementation plan: Claude
  revises PLAN.md while Codex critiques it read-only in one persistent session.
  Use for /codex-review, "Codex review my plan", or high-stakes plans involving
  auth, schema, concurrency, migrations, or payments. Use grill-me-codex when
  requirements still need interviewing. Not for reviewing written code or
  trivial changes.
---

# Codex-Review — Adversarial Plan-Review Loop

Two models, one plan, a bounded argument. **Claude is the builder and orchestrator. Codex is a read-only critic** that can read the repo and the plan but cannot touch a single file. They communicate strictly through `PLAN.md` + a Codex session that persists across rounds. The human enters at exactly two points: kickoff and final sign-off.

This is a **deliberate, high-stakes tool** — reach for it on auth, data models, concurrency, migrations, payments, anything expensive to get wrong. Skip it for obvious/cheap work.

## Prerequisites (verify once, fast)

- Dispatch Codex only through `cx` (`cx --self-test offline` checks it). Never call raw `codex exec`: `cx` pins the routed model and effort, closes stdin (or Codex hangs), turns MCP and plugins off and enforces a timeout.
- Codex authenticated: a prior `codex login` (ChatGPT account is fine). If a run returns an auth/model error, surface it to the user — do not silently retry.
- The model is not yours to pick: this skill is task class `plan-review` in `_system/lanes.json` routes (`route plan-review` prints its lane, model and effort). Don't pass `-m`.
- **Echo the routed model before Round 1** so the user can confirm: state the `route plan-review` line with the resolved tunables. If the user objects, stop before burning a round.
- **Read-only every round.** Always pass `read-only` to `cx`. On round 1 it becomes `-s read-only`; on a resume (`codex exec resume` rejects `-s`) `cx` passes `-c sandbox_mode="read-only"`, because `config.toml` may default `sandbox_mode` to `danger-full-access` (+ `approval_policy="never"`) — which would let Codex WRITE files mid-loop. This is the single most important safety detail in this skill (verified 2026-06-04; `cx --self-test offline` checks the resume mapping).

## Tunable variables (read from skill args, else default)

| Var | Default | Meaning |
|-----|---------|---------|
| `MAX_ROUNDS` | `3` | Hard cap on review rounds. Raise to `5` only when the plan carries security, permission or data-loss risk (auth, secrets, sandboxing, migrations, deletes, backups). The loop ALWAYS terminates at this. |
| `PLAN_FILE` | `PLAN.md` | Where the evolving plan lives (repo root). |
| `LOG_FILE` | `PLAN-REVIEW-LOG.md` | Append-only transcript of the argument (every round's critique + what changed). The artifact. |

If the user invoked the skill with an argument like `rounds=3`, use that for `MAX_ROUNDS`. A `rounds=N` arg or the user's own words (e.g. "until it's perfect") override this budget. Echo the resolved values back before starting.

## Flow

### Step 0 — Kickoff (human gate #1)

The invocation itself is the kickoff. Confirm scope in one line: what is being planned. If the user gave no task, ask for it (one question). Then proceed — do NOT ask for approval round-by-round; that comes at the end.

### Step 1 — Claude plans

Do real planning: read the relevant code, think through the approach, surface decisions and tradeoffs. Then write the plan to `PLAN_FILE` in this structure:

```markdown
# Plan: <task>
_Round 0 — initial draft by Claude_

## Goal
<one paragraph>

## Approach
<numbered steps, concrete>

## Key decisions & tradeoffs
<the contestable choices — name them explicitly so Codex has something to bite>

## Risks / open questions
<what you're unsure about>

## Out of scope
<bounds>
```

Initialize `LOG_FILE`:
```markdown
# Plan Review Log: <task>
Started <stamp the user's local time if known, else "session start">. MAX_ROUNDS=<n>.
```

Show the user the plan inline and say you're sending it to Codex for adversarial review.

### Step 2 — The loop

Maintain `ROUND` (start 1) and `THREAD_ID` (empty until round 1 returns).

**The review prompt** sent to Codex each round (adjust the task line):

> You are an adversarial reviewer for an implementation plan. Be skeptical and specific — your job is to find what breaks, not to be agreeable. Read the plan at `PLAN.md` (and any repo files you need; you are read-only). Identify concrete flaws: security holes, race conditions, missing edge cases, schema conflicts, wrong assumptions, observability gaps, simpler alternatives. For each, tag it P1 (blocks implementation), P2 (should fix) or P3 (nit) and give a one-line fix. Do NOT modify any files. End your reply with EXACTLY one line: `VERDICT: APPROVED` if the plan is sound enough to implement, or `VERDICT: REVISE` if it still has material problems.

**Round 1** (creates the session; `cx` writes its id to `<outfile>.thread`):

```bash
CX_SESSION=new cx plan-review read-only /tmp/codex-verdict.txt "$(cat REVIEW_PROMPT)"
THREAD_ID=$(cat /tmp/codex-verdict.txt.thread)
```
The critique text lands in `/tmp/codex-verdict.txt` (Codex's last message). Read that file. The event stream is in `/tmp/codex-verdict.txt.jsonl` if you need to debug.

> Confirm success by a non-empty verdict file + a `.thread` file. If either is missing or `cx` exits non-zero, the run failed (auth/model) — stop and tell the user.
>
> **Timeout guard:** pass `timeout: 600000` on the Bash tool call (the default 2-minute tool timeout kills real reviews). For a large plan, launch with `run_in_background: true` and read the verdict file when it exits. `CX_TIMEOUT` (default 1500 s) is `cx`'s own hard ceiling. If a ceiling trips, treat it as a failed run: stop and tell the user rather than retrying blind. Applies to the resume call below too.

**Rounds 2..MAX** (resume the SAME session — Codex remembers its earlier critiques, won't re-litigate settled points):

```bash
# cx maps read-only to -c sandbox_mode="read-only" on a resume (resume rejects -s), so Codex
# cannot inherit config.toml's danger-full-access. Echo THREAD_ID first: a wrong id must be visible.
echo "resuming $THREAD_ID"
CX_SESSION="$THREAD_ID" cx plan-review read-only /tmp/codex-verdict.txt \
  "I revised the plan. Re-review PLAN.md: only the sections changed since your last round plus your still-open findings; don't re-audit settled parts. Same rules. End with VERDICT: APPROVED or VERDICT: REVISE."
```

`cx` exits 3 if Codex ran any session other than `THREAD_ID` (a bad id can silently fall back to the newest session); treat that round as failed.

**Each round, after Codex returns:**
1. Read `/tmp/codex-verdict.txt`. Append to `LOG_FILE`: `## Round <n> — Codex` + the full critique.
2. Grep the last line for the verdict token.
   - `VERDICT: APPROVED` → break the loop, go to Step 3 (converged).
   - `VERDICT: REVISE` with only P3 findings → stop early: log the P3s as open items and go to Step 3 (converged).
   - `VERDICT: REVISE` → Claude reads the critique, decides **what's actually worth acting on** (Claude has final say — Codex advises, it does not command). Revise `PLAN_FILE`. Append to `LOG_FILE`: `### Claude's response` + what you changed and what you rejected and why. Increment `ROUND`.
3. If `ROUND > MAX_ROUNDS` → break to Step 3 (deadlock).

### Step 3 — Resolution (human gate #2)

**If APPROVED:** Present to the user — the final `PLAN_FILE`, a 3-bullet summary of what the argument improved, and the round count. Ask: *"Plan survived N rounds of Codex. Implement it now — Codex builds it (`/codex-build`), Claude builds it, or stop here?"* Only on a yes is code written. **No code is written during the loop.** If the user picks Codex, invoke the `codex-build` skill with `SPEC_FILE=PLAN.md` and the same `LOG_FILE` — roles flip (Codex writes, Claude reviews the diff) and the build rounds append to the same log.

**If MAX_ROUNDS hit without APPROVED (deadlock):** Do NOT pretend it converged. Surface the unresolved disagreements explicitly: list each point Codex still flags and Claude's counter-position. Hand it to the human to break the tie. This is a legitimate, useful outcome — a flagged disagreement beats a false "approved."

**Either way,** end `LOG_FILE` with `## Review summary`: rounds used / cap, reviewer model (the `route plan-review` line), final verdict (APPROVED, APPROVED with open P3s, or deadlock), and the open P2/P3s. A PR built from this plan copies it into its body (`codex-build` Step 5).

## Hard rules

- Codex is read-only EVERY round — pass `read-only` to `cx` on the first call and on every resume (`cx` turns it into `-s` or `-c sandbox_mode`). It never writes. If you're tempted to give it write access, stop — that's a different skill.
- The loop ALWAYS terminates at `MAX_ROUNDS`. No unbounded recursion.
- Claude is the final arbiter on every REVISE — incorporate good critiques, reject bad ones *with a reason logged*. Don't cave to Codex on everything (that defeats the cross-model check) and don't ignore it (that defeats the point).
- Code only after human gate #2.
- `LOG_FILE` is the deliverable — it tells the whole story of the argument. Keep it complete.

## What NOT to do

- Don't use this to review existing code — that's `/codex:review`.
- Don't call raw `codex exec` or pin `-m` — `cx` and the `plan-review` route pick the model.
- Don't skip the log — the argument transcript is the most valuable artifact.
- Don't let Codex edit files. Read-only, always.
