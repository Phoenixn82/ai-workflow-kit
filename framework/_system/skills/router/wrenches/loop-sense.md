---
name: loop-sense
description: Wrench inside the `router` mechanic. Detects loop-shaped tasks, routes them to the right lane, and proposes budget-declared loop runbooks as drafts. It never starts work without the user's explicit go.
---

# loop-sense - loop-shaped work detector

Detect repeated work before Claude grinds through it. This wrench proposes a ready-to-review loop draft; it does not start `/goal`, `/loop`, Workflow, ScheduleWakeup, or any other automation.

## DETECT

Use this 5-point checklist exactly. Any failure -> one-shot work, not a loop.

1. machine-checkable success
2. >=3 listable verification checks
3. >10 similar iterations or run-until-criterion
4. no mid-loop human judgment
5. budget declarable

## ROUTE

Use live-session probes, never assertions. At proposal time, check the CURRENT session's actual skill/tool surface for each lane:

- Is `/loop` present in the current skill or command list?
- Are ScheduleWakeup or Workflow tools present in the current toolset?
- Does `codex --version` pass, and does the current Codex auth probe pass?
- Is the task asking for Claude-owned judgment, Codex-owned implementation, cheap bounded grunt work, or fan-out coordination?

Fallback ladder:

- Interval loop -> `/loop` if present; else Workflow loop if present; else propose a manual runbook.
- Self-paced wakeup loop -> ScheduleWakeup if present; else `/loop`; else propose a manual runbook.
- Fan-out loop -> Workflow if present; else sequential batches with explicit checkpoints.
- Codex implementation loop -> Codex `/goal` runbook when the checklist passes and the owner is Codex.
- Bare `/goal` with no owner -> ask the user whether the owner is Codex, Claude, or another lane before drafting.

Codex `/goal` drafts are runbooks, not bare prompt bodies. They must include the interactive activation step, babysit arming, and stand-down instructions.

## CRAFT

Delegate draft shaping to the right existing surface:

- Codex loops -> use the `codex-goal-dispatcher` template interview, falling back to `templates/generic.md` when no specific template matches.
- Claude loops -> use `plan-room` to shape the loop spec before drafting.

Every draft must be budget-declared: state the mandatory declared max budget and the stop conditions as text. This is a text contract; code enforcement arrives with the Phase-0b preflight patch.

Use the job-card field set:

```text
JOB:        what does this loop own?
INPUTS:     what does it inspect?
ALLOWED:    what may it change or do?
FORBIDDEN:  what must it never do?
OUTPUT:     what exists after a good run?
EVALUATION: how do I know it did well?
```

Add risk-scaled verification:

- At least 3 machine-checkable checks.
- More checks for high-blast-radius work.
- Explicit stop conditions for success, budget hit, tool failure, and human-judgment needed.

## Proposal Output

Return one inline sentence to the user:

```text
This is loop-shaped; I drafted a budget-declared runbook at pipeline\loop-drafts\<yyyy-mm-dd>-<slug>.md for your go/no-go.
```

Save the full draft at `pipeline\loop-drafts\<yyyy-mm-dd>-<slug>.md` from the project root. Use a short lowercase slug from the task. The draft is a proposal only.

Hard rule: propose only. Nothing starts without the user's explicit go.

