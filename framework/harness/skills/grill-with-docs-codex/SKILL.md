---
name: grill-with-docs-codex
description: >-
  Harden an unresolved plan against living project documentation, then have
  Codex review the locked PLAN.md adversarially in a read-only session. Use for
  /grill-with-docs-codex, established domain terminology, CONTEXT.md, or ADRs
  that must evolve as decisions are made. Not for reviewing written code,
  already-frozen plans, or trivial changes.
---

# Grill-with-Docs-Codex — Grill Against Your Domain, Then Get Reviewed

Two acts. Act 1 aligns intent *and* keeps your living docs honest; Act 2 has a different model attack the result.

- **Act 1** is Matt Pocock's `grill-with-docs`, used under MIT (see `THIRD-PARTY-NOTICES.md`). It interrogates you, challenges your plan against `CONTEXT.md`/ADRs, and updates them inline.
- **Act 2** is the original Codex adversarial review loop — cross-model, read-only, bounded.

You enter at two points: answering the grill, and signing off the converged plan.

---

## ACT 1 — GRILL WITH DOCS (you ↔ Claude)

<what-to-do>

Interview me relentlessly about every aspect of this plan until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time, waiting for feedback on each question before continuing.

If a question can be answered by exploring the codebase, explore the codebase instead.

</what-to-do>

<supporting-info>

## Domain awareness

During codebase exploration, also look for existing documentation:

### File structure

Most repos have a single context:

```
/
├── CONTEXT.md
├── docs/
│   └── adr/
│       ├── 0001-event-sourced-orders.md
│       └── 0002-postgres-for-write-model.md
└── src/
```

If a `CONTEXT-MAP.md` exists at the root, the repo has multiple contexts. The map points to where each one lives:

```
/
├── CONTEXT-MAP.md
├── docs/
│   └── adr/                          ← system-wide decisions
├── src/
│   ├── ordering/
│   │   ├── CONTEXT.md
│   │   └── docs/adr/                 ← context-specific decisions
│   └── billing/
│       ├── CONTEXT.md
│       └── docs/adr/
```

Create files lazily — only when you have something to write. If no `CONTEXT.md` exists, create one when the first term is resolved. If no `docs/adr/` exists, create it when the first ADR is needed.

## During the session

### Challenge against the glossary

When the user uses a term that conflicts with the existing language in `CONTEXT.md`, call it out immediately. "Your glossary defines 'cancellation' as X, but you seem to mean Y — which is it?"

### Sharpen fuzzy language

When the user uses vague or overloaded terms, propose a precise canonical term. "You're saying 'account' — do you mean the Customer or the User? Those are different things."

### Discuss concrete scenarios

When domain relationships are being discussed, stress-test them with specific scenarios. Invent scenarios that probe edge cases and force the user to be precise about the boundaries between concepts.

### Cross-reference with code

When the user states how something works, check whether the code agrees. If you find a contradiction, surface it: "Your code cancels entire Orders, but you just said partial cancellation is possible — which is right?"

### Update CONTEXT.md inline

When a term is resolved, update `CONTEXT.md` right there. Don't batch these up — capture them as they happen. Use the format in [CONTEXT-FORMAT.md](./CONTEXT-FORMAT.md).

`CONTEXT.md` should be totally devoid of implementation details. Do not treat `CONTEXT.md` as a spec, a scratch pad, or a repository for implementation decisions. It is a glossary and nothing else.

### Offer ADRs sparingly

Only offer to create an ADR when all three are true:

1. **Hard to reverse** — the cost of changing your mind later is meaningful
2. **Surprising without context** — a future reader will wonder "why did they do it this way?"
3. **The result of a real trade-off** — there were genuine alternatives and you picked one for specific reasons

If any of the three is missing, skip the ADR. Use the format in [ADR-FORMAT.md](./ADR-FORMAT.md).

</supporting-info>

### Handoff to Act 2

When the decision tree is resolved, the glossary/ADRs are updated, and we're aligned, **write the agreed plan to `PLAN.md`** (use the canonical terms from `CONTEXT.md`), then run Act 2:

```markdown
# Plan: <task>
_Locked via grill-with-docs — by Claude + <user>. Terms per CONTEXT.md._

## Goal
<one paragraph, in the project's ubiquitous language>

## Approach
<numbered, concrete steps>

## Key decisions & tradeoffs
<the contestable choices the grill resolved — link any ADRs created>

## Risks / open questions
<anything still open>

## Out of scope
<bounds>
```

Initialize `PLAN-REVIEW-LOG.md`:
```markdown
# Plan Review Log: <task>
Act 1 (grill-with-docs) complete — plan locked, CONTEXT.md/ADRs updated. MAX_ROUNDS=<n>.
```

---

## ACT 2 — REVIEW (Claude ↔ Codex)

Hand the locked plan to Codex for adversarial review. Mechanics verified end-to-end (2026-06-04).

### Prerequisites
- Dispatch Codex only through `cx` (`cx --self-test offline` checks it), never raw `codex exec`: `cx` pins the routed model and effort, closes stdin (or Codex hangs), turns MCP and plugins off and enforces a timeout.
- Codex authenticated (`codex login`; ChatGPT account fine). On auth/model error, surface it — don't silently retry.
- The reviewer model is task class `plan-review` in `_system/lanes.json` routes (`route plan-review` prints its lane, model and effort). Don't pass `-m`.
- **Echo the routed model before Round 1** so the user can confirm: state the `route plan-review` line with the resolved tunables. If the user objects, stop before burning a review round.

### Tunables (args, else default)
| Var | Default | Meaning |
|-----|---------|---------|
| `MAX_ROUNDS` | `3` | Hard cap. Raise to `5` only for security, permission or data-loss risk (auth, secrets, sandboxing, migrations, deletes, backups). Loop ALWAYS terminates here. |
| `PLAN_FILE` | `PLAN.md` | The plan from Act 1. |
| `LOG_FILE` | `PLAN-REVIEW-LOG.md` | Append-only argument transcript. |

Invoked with e.g. `rounds=3` → use it. A `rounds=N` arg or the user's own words (e.g. "until it's perfect") override this budget. Echo resolved values first.

### Review prompt (each round)
> You are an adversarial reviewer for an implementation plan. Be skeptical and specific — your job is to find what breaks, not to be agreeable. Read the plan at `PLAN.md` (and `CONTEXT.md`/ADRs for the domain language) and any repo files you need (you are read-only). Identify concrete flaws: security holes, race conditions, missing edge cases, schema conflicts, domain-language mismatches, wrong assumptions, observability gaps, simpler alternatives. For each, tag it P1 (blocks implementation), P2 (should fix) or P3 (nit) and give a one-line fix. Do NOT modify any files. End with EXACTLY one line: `VERDICT: APPROVED` or `VERDICT: REVISE`.

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

### Each round
1. Read verdict file; append `## Round <n> — Codex` + critique to `LOG_FILE`.
2. Last line verdict: `APPROVED` → Resolution (converged); `REVISE` with only P3 nits → stop early, log the P3s as open, Resolution (converged); `REVISE` → Claude decides what's worth acting on (final arbiter), revise `PLAN_FILE`, append `### Claude's response` (what changed/rejected + why), increment.
3. round > `MAX_ROUNDS` → Resolution (deadlock).

### Resolution (you sign off)
- **APPROVED:** present final plan + 3-bullet summary of what the two acts improved + round count. Ask: implement now — Codex builds it (`/codex-build`), Claude builds it, or stop? No code during either act.
- **Deadlock (cap hit, no APPROVED):** list unresolved points + Claude's counter-position; hand to user. Don't fake convergence.
- **Either way,** end `LOG_FILE` with `## Review summary`: rounds used / cap, reviewer model (the `route plan-review` line), final verdict (APPROVED, APPROVED with open P3s, or deadlock), and the open P2/P3s. A PR built from this plan copies it into its body (`codex-build` Step 5).
- **Act 3 (optional):** user picks Codex → invoke the `codex-build` skill with `SPEC_FILE=PLAN.md` and the same `LOG_FILE`. Roles flip: Codex writes with full access, Claude reviews the diff + runs the proof; build rounds append to the same log.

---

## Hard rules
- Act 1 precedes Act 2. `CONTEXT.md` stays a glossary only — no implementation details.
- Codex read-only EVERY round (pass `read-only` to `cx` every round; it becomes `-s` first and `-c sandbox_mode` on resume). Never writes.
- Loop ALWAYS terminates at `MAX_ROUNDS`. Claude is final arbiter on REVISE (reject with logged reason). Code only after sign-off. `LOG_FILE` is the deliverable.

## What NOT to do
- Don't review already-written code (`/codex:review`). Don't call raw `codex exec` or pin `-m` (`cx` + route `plan-review`). Don't let Codex edit files. Don't skip Act 1.
