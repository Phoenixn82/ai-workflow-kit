---
name: grill-me-codex
description: >-
  Harden an unresolved plan in two acts: Claude interviews the user one question
  at a time, then Codex adversarially reviews the locked PLAN.md read-only until
  approval or a round cap. Use for /grill-me-codex, "grill me then have Codex
  review", or high-stakes work that needs both alignment and a second-model
  check. Use grill-with-docs-codex when CONTEXT.md or ADRs must evolve too.
---

# Grill-Me-Codex — Get Grilled, Then Get Reviewed

Two acts, two different jobs:

- **Act 1 fixes the #1 failure mode: building the wrong thing.** Claude interrogates *you* until intent is locked — no guessing at ambiguity. (This act is Matt Pocock's `grill-me`, used under MIT — see `THIRD-PARTY-NOTICES.md`.)
- **Act 2 fixes the #2 failure mode: a plan that sounds right but breaks.** A *different model* (Codex) adversarially attacks the locked plan. Cross-model = no echo chamber.

You enter at two points only: answering the grill, and signing off the converged plan. Codex is read-only the whole time and never touches a file.

---

## ACT 1 — GRILL (you ↔ Claude)

> Interview me relentlessly about every aspect of this plan until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.
>
> Ask the questions one at a time, waiting for my answer before continuing.
>
> If a question can be answered by exploring the codebase, explore the codebase instead.

When the decision tree is resolved and we're aligned, **write the agreed plan to `PLAN.md`** in this structure, then move to Act 2:

```markdown
# Plan: <task>
_Locked via grill — by Claude + <user>_

## Goal
<one paragraph — reflects what the grilling actually settled>

## Approach
<numbered, concrete steps>

## Key decisions & tradeoffs
<the contestable choices the grill resolved — name them so Codex has something to bite>

## Risks / open questions
<anything still genuinely open>

## Out of scope
<bounds the grill established>
```

Initialize `PLAN-REVIEW-LOG.md`:
```markdown
# Plan Review Log: <task>
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=<n>.
```

---

## ACT 2 — REVIEW (Claude ↔ Codex)

Now hand the locked plan to Codex for adversarial review. Same engine, mechanics verified end-to-end (2026-06-04).

### Prerequisites (verify once, fast)
- Dispatch Codex only through `cx` (`cx --self-test offline` checks it), never raw `codex exec`: `cx` pins the routed model and effort, closes stdin (or Codex hangs), turns MCP and plugins off and enforces a timeout.
- Codex authenticated (prior `codex login`; ChatGPT account is fine). On auth/model error, surface it — don't silently retry.
- The reviewer model is task class `plan-review` in `_system/lanes.json` routes (`route plan-review` prints its lane, model and effort). Don't pass `-m`.
- **Echo the routed model before Round 1** so the user can confirm: state the `route plan-review` line with the resolved tunables. If the user objects, stop before burning a review round.

### Tunables (read from args, else default)
| Var | Default | Meaning |
|-----|---------|---------|
| `MAX_ROUNDS` | `3` | Hard cap on review rounds. Raise to `5` only when the plan carries security, permission or data-loss risk (auth, secrets, sandboxing, migrations, deletes, backups). The loop ALWAYS terminates here. |
| `PLAN_FILE` | `PLAN.md` | The plan Act 1 produced. |
| `LOG_FILE` | `PLAN-REVIEW-LOG.md` | Append-only argument transcript. The artifact. |

If invoked with e.g. `rounds=3`, use that for `MAX_ROUNDS`. A `rounds=N` arg or the user's own words (e.g. "until it's perfect") override this budget. Echo resolved values before starting.

### The review prompt (sent each round)
> You are an adversarial reviewer for an implementation plan. Be skeptical and specific — your job is to find what breaks, not to be agreeable. Read the plan at `PLAN.md` and any repo files you need (you are read-only). Identify concrete flaws: security holes, race conditions, missing edge cases, schema conflicts, wrong assumptions, observability gaps, simpler alternatives. For each, tag it P1 (blocks implementation), P2 (should fix) or P3 (nit) and give a one-line fix. Do NOT modify any files. End your reply with EXACTLY one line: `VERDICT: APPROVED` if the plan is sound enough to implement, or `VERDICT: REVISE` if it still has material problems.

### Round 1 — fresh session (`cx` writes its id to `<outfile>.thread`)
```bash
CX_SESSION=new cx plan-review read-only /tmp/codex-verdict.txt "$(cat REVIEW_PROMPT)"
THREAD_ID=$(cat /tmp/codex-verdict.txt.thread)
```
The critique is in `/tmp/codex-verdict.txt` (event stream in `/tmp/codex-verdict.txt.jsonl`). No verdict file, no `.thread` file or a non-zero `cx` exit = failed run (auth/model) → stop and tell the user.

### Rounds 2..MAX — resume the SAME session (Codex remembers its prior critiques)
```bash
# Pass read-only every round: on a resume (which rejects -s) cx sends -c sandbox_mode="read-only", or Codex would
# inherit config.toml (possibly danger-full-access) and could WRITE files. Critical safety line, verified 2026-06-04.
echo "resuming $THREAD_ID"
CX_SESSION="$THREAD_ID" cx plan-review read-only /tmp/codex-verdict.txt \
  "I revised the plan. Re-review PLAN.md — only the sections changed since your last round plus your still-open findings: check whether those are addressed and flag anything new in the changes; don't re-audit settled parts. End with VERDICT: APPROVED or VERDICT: REVISE."
```
`cx` exits 3 if Codex ran any session other than `THREAD_ID` (a bad id can fall back to the newest session); treat that round as failed.

**Timeout guard (both rounds):** pass `timeout: 600000` on the Bash tool call (the default 2-minute tool timeout kills real reviews); for a large plan launch with `run_in_background: true` and read the verdict file when it exits. `CX_TIMEOUT` (default 1500 s) is `cx`'s own ceiling. If a ceiling trips, treat it as a failed run: stop and tell the user rather than retrying blind.

### Each round, after Codex returns
1. Read `/tmp/codex-verdict.txt`; append to `LOG_FILE`: `## Round <n> — Codex` + the full critique.
2. Grep the last line for the verdict:
   - `VERDICT: APPROVED` → break to Resolution (converged).
   - `VERDICT: REVISE` with only P3 findings → stop early: log the P3s as open items, break to Resolution (converged).
   - `VERDICT: REVISE` → Claude decides **what's actually worth acting on** (Claude is final arbiter — Codex advises, doesn't command). Revise `PLAN_FILE`. Append `### Claude's response` to `LOG_FILE`: what changed, what was rejected, why. Increment round.
3. If round > `MAX_ROUNDS` → break to Resolution (deadlock).

### Resolution (you sign off — final gate)
- **APPROVED:** present the final `PLAN_FILE`, a 3-bullet summary of what the two acts improved, and the round count. Ask: *"Grilled + survived N rounds of Codex. Implement it now — Codex builds it (`/codex-build`), Claude builds it, or stop here?"* Code only on a yes. **No code is written during either act.**
- **MAX_ROUNDS hit without APPROVED (deadlock):** do NOT fake convergence. List each unresolved point + Claude's counter-position; hand it to the user to break the tie. A flagged disagreement beats a false "approved."
- **Either way,** end `LOG_FILE` with `## Review summary`: rounds used / cap, reviewer model (the `route plan-review` line), final verdict (APPROVED, APPROVED with open P3s, or deadlock), and the open P2/P3s. A PR built from this plan copies it into its body (`codex-build` Step 5).

### ACT 3 (optional) — BUILD (Codex ↔ Claude, roles flipped)

If the user picks Codex: invoke the `codex-build` skill with `SPEC_FILE=PLAN.md` and the same `LOG_FILE` — it appends `## Act 3 — Build` to the log, so one artifact tells the whole story (grilled → reviewed → built → verified). Roles flip: Codex writes the code with full access, Claude reviews the diff and runs the proof. If the user picks Claude, implement directly as usual.

---

## Hard rules
- Act 1 always precedes Act 2 — don't write `PLAN.md` until the grill has actually resolved the decision tree with the user.
- Codex is read-only EVERY round — pass `read-only` to `cx` on the first call and every resume (`cx` turns it into `-s` or `-c sandbox_mode`). It never writes.
- The loop ALWAYS terminates at `MAX_ROUNDS`.
- Claude is final arbiter on every REVISE — incorporate good critiques, reject bad ones *with a logged reason*. Don't cave to everything (defeats the cross-model check) and don't ignore it (defeats the point).
- Code only after the user's final sign-off.
- `LOG_FILE` is the deliverable — keep the whole argument.

## What NOT to do
- Don't review already-written code — that's `/codex:review`.
- Don't call raw `codex exec` or pin `-m` — `cx` and the `plan-review` route pick the model.
- Don't let Codex edit files. Read-only, always.
- Don't skip Act 1 — the grill is half the value.
