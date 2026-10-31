---
name: router
description: Adaptive multi-brain dispatcher across Claude / Codex / Gemini / freellm. Loads every session as always-core. Detects available lanes, reroutes on loss, enforces no-self-rescue (2 Claude fails on the same sub-task → Codex). Routes by cost and capability. Fires on every non-trivial conversation start, every major task transition, and on phrases like "ask all three / four", "consensus", "debate", "second opinion", "rescue this", "deep research", "long PDF", "grunt work", "classify these", "bulk".
---

# router — multi-brain dispatcher

One prompt, four lanes, the right one tagged in for the job. Claude orchestrates; the router decides which brain executes.

| Lane | Job | Cost |
|---|---|---|
| **Claude** (you) | Architecture, planning, judgment, tight-context implementation, orchestration | Claude subscription |
| **Codex** | High-throughput implementation, tests, refactors, dispatched goals, diff-quality review | Codex subscription; use it liberally if your tier allows |
| **Gemini** | Long-context **READ + ANALYSIS**, multimodal (native frames + transcript in one call), grounded research, large-context scoping. Full lane — picks up code by task fit. | Gemini API key (free tier or paid) |
| **freellm** | Grunt work — bulk classification, repetitive transforms, low-stakes per-item processing | $0, bounded quality, ~20 free providers via local router |

**Cost ordering (cheapest → most expensive per equivalent task):** freellm → Gemini → Codex → Claude. Use a free lane only where `lane admission` shows the class admitted; otherwise Claude or Codex. Private/PII → `auto:local` only.

**Which model and effort: the task routes, never this page.** `_system/lanes.json` "routes" maps every task class to a lane (model + effort); there is no default lane. `route list` prints the table, `route <class>` how to dispatch it. Agent tool and Workflow `agent()` calls take the route's model (and effort); Codex goes only through `cx <class|plan|check|build> <sandbox> <out> <prompt>`; `claude -p` gets the route's `--model` and `--effort`; free and private work goes through `lane`.

This mechanic sits ABOVE the `codex`, `gemini`, `codex-goal`, `freellmapi` wrenches — it does not duplicate them; it decides which one to call and when.

---

## When this fires (always-on)

The router fires automatically when any of these conditions is true. **No user prompt needed.**

1. **Multimodal source over 3 MB** (`.mp4 .mov .mkv .webm .mp3 .wav .m4a .pdf`) → **Gemini** lane.
2. **Single-task context window > 300K tokens** (whole-repo reads, long logs, big PDFs, multi-hour transcripts) → **Gemini**.
3. **Deliverable is a code artifact** (`.py .ts .tsx .js .go .rs .sql .yml .yaml .json Dockerfile .tf` etc.) → choose **Claude or Codex by task fit**. Use Codex for high-volume, independent, or long-running work; use Claude for small or context-tight changes where dispatch is overhead. See § Code routing rule.
4. **Bounded grunt-work signal** — map the work to a `lane` class, check `lane admission`, then run `lane <class> --tier public|business|business-pii|private` only when admitted. Private/PII stays `auto:local`; otherwise Claude or Codex. See § The Grunt Rule.
5. **Repetitive / batch-shaped ask** (again-and-again work, >10 similar iterations, run-until-criterion) → run the `loop-sense` checklist before grinding.
6. **Same sub-task failed twice on Claude** → forced **Codex rescue**. No third Claude attempt. See § The no-self-rescue rule.
7. **Consensus phrases** (see table below) → **parallel debate** across the relevant lanes.
8. **Planning / scope / judgment / coordination** → pick the task class from `route list` (e.g. `research-synthesis`, `hard-engineering`, `verify-judge`) and dispatch on its lane.

If none of the above hit and the task is plain build/refactor/explain → **stay on Claude**. Do not route trivial work. Trivial routing adds latency without payoff.

---

## Explicit-phrase triggers

| Phrase from the user | Route |
|---|---|
| "ask all four" / "ask all three" / "debate" / "consensus" / "what do they all think" | Parallel — see § Parallel consensus |
| "second opinion" / "review my code" / "is this right" / "rescue" / "outside voice" | `codex` wrench (review or challenge mode) |
| "analyse this video" / "watch this clip" / "frame by frame" / "transcript + visuals" | `gemini` wrench |
| "huge PDF" / "long doc" / "200 pages" / "big context" / "1M tokens" / "whole repo" | `gemini` wrench |
| "deep research" / "research dossier" / "grounded synthesis" | `gemini` wrench → Deep Research sub-path (gemini.google.com via `web act`, the signed-in identity browser) |
| "classify these" / "tag each" / "bulk process" / "for each of these N" / "grunt work" / "low-stakes batch" | Map to a `lane` class, check `lane admission`, then run `lane <class> --tier ...` |
| "do this repeatedly" / "again and again" / "batch this until done" / "run until it passes" / "loop-shaped" | `loop-sense` wrench |
| "/goal" / "kick off a codex goal" / "comprehensive user testing" / "lift coverage to N%" / "a11y pass on every page" / "bug bash" / "grind on this until tests pass" | `codex-goal` wrench |
| "use Codex" / "use Gemini" | Honour the user verbatim |
| "use freellm" / "use the local router" | Map to a `lane` class and tier; free use still requires `lane admission`, and private/PII stays `auto:local` |

---

## Routing decision tree

```
                          ┌────────────────────────────┐
   user prompt ─────────► │  Bounded grunt work?       │
                          │  (bulk classify, per-item  │
                          │  transform, low-stakes     │
                          │  batch, N > 5 items)       │
                          └─────────────┬──────────────┘
                                        │
                             yes ◄──────┴──────► no
                              │                   ▼
                         lane            ┌────────────────────────┐
                    (admitted class +    │  Multimodal, > 300K    │
                         data tier)      │  context, or deep      │
                         Codex fallback  │  context, or deep      │
                         on junk         │  research?             │
                                         └────────┬───────────────┘
                                                  │
                                       yes ◄──────┴──────► no
                                        │                   ▼
                                    Gemini       ┌─────────────────────────┐
                                    (paid sub)   │  Code generation,       │
                                    Codex on     │  refactor, build?       │
                                    failure      └────────┬────────────────┘
                                                          │
                                               yes ◄──────┴──────► no
                                                │                   ▼
                                      ┌─────────────────┐  ┌─────────────────┐
                                      │ Single file?    │  │ Pure question   │
                                      │ Clean spec?     │  │ or analysis?    │
                                      └────┬────────────┘  └────────┬────────┘
                                           │                        │
                                    yes ◄──┴──► no             yes ◄┴──► no
                                     │          │               │        │
                                  Codex     Gemini-spec      Claude   Ask the user
                                  (1-shot)  → Codex writes   answers  to clarify
                                            (1M read,per-file)
                                     │
                                     ▼
                          ┌──────────────────────┐
                          │ Risky path?          │
                          │ (auth/crypto/        │
                          │ payments/migrations/ │
                          │ secrets, SQL, .tf,   │
                          │ workflow YAML)       │
                          │ → auto codex review  │
                          │ before merge         │
                          └──────────────────────┘
```

**Risky-path heuristic** — auto-invoke a Codex review (`cx code-review read-only ...`, see `wrenches/codex.md`) when edits touch any of:
- Anything under `auth/`, `crypto/`, `payments/`, `migrations/`, `secrets/`
- Any file matching `*.sql`, `*.tf`, `Dockerfile`, `*.yml` in `.github/workflows/`
- Anything that handles user-supplied input, JWTs, sessions, or shells out to a subprocess

---

## The code-routing rule (choose by task fit)

Claude and Codex are both first-class code lanes. Do not route from the file
extension alone and do not force a round trip merely because the output is
code.

**Prefer Codex when:**
- The work is high-volume, mechanical, independently specifiable, or likely to run for a long time
- A bounded goal has machine-verifiable stop conditions
- A second-model diff review adds value
- Claude has failed the same approach twice

**Prefer Claude when:**
- The change is small or tightly coupled to context already in the session
- Debugging depends on shared conversation state or Claude-only tools
- Writing a dispatch-quality specification costs more than implementing the change
- The user explicitly selects Claude

Use Gemini to read or analyse context that benefits from its window; it often
outputs a per-file specification, and it may write repository files too when
that's the best fit.

| Sub-task type | Best-fit lane |
|---|---|
| Project intake, scope shaping, architecture, conflict reconciliation | **Claude** |
| Small or context-tight implementation and debugging | **Claude** |
| Mechanical refactors, test generation, broad implementation, long goals | **Codex** |
| Code review or adversarial second opinion | **Codex** |
| Multi-file work with context too large for the active lane | **Gemini reads/specifies → Claude or Codex implements by task fit** |
| Multimodal (video/audio/PDF), >200K-token non-code context, bulk extraction | **Gemini** wrench |
| Grounded web research with citations | **Gemini** wrench (Deep Research sub-path on gemini.google.com via `web act`) |

**Reverse rescue:** if Codex output is wrong twice on the same spec, the default is to diagnose and re-spec rather than bounce the rewrite to another lane. Instead: **diagnose WHY Codex is failing** — network blip, timeout, ambiguous spec, file too large, missing context — then either (a) fix the root cause and retry Codex, or (b) have Claude re-spec the task more tightly (tighter constraints, clearer file boundaries, narrower scope) and re-dispatch to Codex. After 2 re-spec attempts that also fail, surface to the user — don't grind.

When dispatching sub-agents (the `Agent` tool or a Workflow `agent()`), name the task class and set the model (and effort) its route gives (`route <class>`). Inheriting silently skips the routing table.

---

## The Grunt Rule (Claude does not run per-item loops)

**If the task is a per-item loop of N > 5 bounded, low-stakes operations, Claude does not run the loop.** Map it to a `lane` class, check `lane admission`, then use `lane <class> --tier ...` only where admitted. Claude reads the output and decides what to do with it.

Examples that route through `lane` after admission:
- Classify 200 public catalog items → `lane classify --tier public`
- Extract fields from 300 public documents → `lane extract-json --tier public`
- Apply a regex transform to 80 public strings → `lane regex --tier public`
- Tag 1,000 vault notes → `lane label-text --tier private` (`auto:local` only)
- Extract customer leads → `lane lead-extract --tier business-pii` (`auto:local` only)

Judgment work such as clickability scoring does not map to an admitted checkable class; keep it in Claude or Codex.

**Rule of thumb:** if the output is text the user would commit to a code repo, Claude is not the author. If the work is the same operation done N times on bounded inputs, Claude is not the executor.

---

## The no-self-rescue rule (load-bearing)

Claude does not reliably recognise its own failures. The router enforces this rule mechanically:

```
attempt_count[task_id] += 1
if attempt_count[task_id] >= 2 and same_root_cause_as_last_attempt:
    REQUIRED: hand off to Codex (Gemini-spec-then-Codex for multi-file) before any further Claude attempt
```

**Same task =** same file + same error class + same general approach. If Claude pivots strategy completely (regex → AST parsing, for instance), the counter resets.

**Cross-vendor fallthroughs (freellm → Codex, Gemini → Codex) do NOT count as self-rescue.** Those are quota management. The counter tracks Claude→Claude retry cycles only.

### Handoff pattern

```bash
cx plan-review read-only /tmp/codex-rescue.md "$(cat <<'EOF'
Claude has failed twice on this task. Original ask:
<one-paragraph restatement of what the user wanted>

What Claude tried and why it failed:
- Attempt 1: <approach> → <error>
- Attempt 2: <approach> → <error>

Files involved: <paths>

Question for you: what is the correct approach? Be opinionated.
Return a concrete plan, not just a critique.
EOF
)"
```

> `cx` bakes in what raw `codex exec` gets wrong in a non-TTY dispatch: closed stdin (or it hangs on `Reading additional input from stdin...`), the route's explicit model and effort, no MCP servers or plugins, an output file and a timeout. Never call `codex exec` by hand.

After Codex returns a plan, Claude resumes execution — but the failure counter resets only when the bug is verified fixed.

### State file (when scripts land)

The counter persists at `~/.claude/state/attempt-counters.json` (the path `track_attempt.py` actually uses) so it survives session compaction, sub-agent dispatch, and overnight loops. (Migrated off the old `~/.gstack/three-brain/attempts.json` location.) Use the helper script `track_attempt.py` rather than editing JSON directly.

`<task_id>` convention: `<project>/<file_basename>/<error-class-slug>` (e.g. `example-app/auth.ts/type-mismatch`). Keep it stable across attempts so increments are detected.

---

## Multi-tier fallthrough chains

Each lane has a defined fallback when its provider misbehaves. Falls through silently for transient failures (quota, timeout, auth blip) and announces the swap in one sentence.

### Grunt-work route: `lane` → Claude on the `bulk-sweep` route

1. Map the task to a class from `_tools/lanes/config/classes.yaml`.
2. Run `lane admission`; use the free lane only when that class is admitted.
3. Run `lane <class> --tier public|business|business-pii|private`.
4. For rejected or invalid Public/Business output, fall through to the `bulk-sweep` route (its lane is the routed pick for extraction and classification over many items). Private and Business-PII stay `auto:local` and never fall through to cloud.

### Long-context / multimodal **READ** chain: Gemini → Claude on the `research-synthesis` route (Gemini's strength here is the report/spec; any lane can implement)

```bash
# Tier 1: Gemini (paid sub) — for READ / ANALYSIS / TRANSCRIPT / SUMMARY tasks
if ! gemini --skip-trust -p "$PROMPT" < "$INPUT" > "$OUTPUT"; then
  echo "[router] Gemini failed; falling back to the research-synthesis route"
  # Tier 2: the route's Claude lane (model + effort from `route research-synthesis`; no Codex lane is routed for reads)
  read -r M E < <(route --json research-synthesis | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["model"], d["effort"])')
  { printf '%s\n\n' "$PROMPT"; cat "$INPUT"; } | claude -p --model "$M" --effort "$E" > "$OUTPUT"
fi
```

This chain is for analysis / summarization / extraction — Gemini producing a written report. **If the task is code authorship, this chain does not apply** — code work routes through the code-build chain below.

### Code-build chain: Codex → diagnose Codex → Claude re-spec → Codex retry

After 2 Codex failures on the same spec, the router's default is to re-spec and retry rather than bounce the rewrite to a different lane. Instead:

1. **Diagnose** — was it a network blip? Timeout? Ambiguous spec? File too large for Codex's window? Missing context Codex couldn't infer?
2. If diagnosis points to spec issue → **Claude re-specs** the task tighter (narrower scope, clearer constraints, explicit file boundaries, more inline examples) and re-dispatches to Codex
3. If diagnosis points to infra issue → fix and retry
4. After 2 re-spec attempts also fail → **surface to the user.** Don't grind.

The point: when Codex is the right tool but failing, fix the dispatch quality, don't switch lanes. Claude's role here is to re-spec, not to write the code itself.

**Junk that triggers fallthrough** (within a single lane attempt):
- Empty content / whitespace only
- Content that clearly doesn't follow the requested format (asked for JSON, got prose; asked for one-line summary, got 20 paragraphs)
- A refusal or "I cannot" response on a benign request
- Provider-level error in the response wrapper

**Do not** silently fall through on a Codex/Claude-grade task that was misrouted to freellmapi in the first place. If the prompt was wrong-tier, fix the routing — don't paper over with retries.

---

## Parallel consensus mode

Triggered by "ask all four", "ask all three", "debate", "consensus", or "what do they all think". Run the relevant lanes on the same question with the **same prompt template** so answers are comparable. Use the `Agent` tool to run Codex / Gemini / freellmapi in parallel; Claude answers in-process.

**Default fan-out:**
- "ask all three" → Claude + Codex + Gemini (the classic; omit freellm — its free providers add noise, not signal, on judgment questions)
- "ask all four" → include `lane <class> --tier ...` only when the question maps to an admitted class and its data tier is known; otherwise use Claude + Codex + Gemini
- "consensus" / "debate" → Claude + Codex + Gemini; include `lane <class> --tier ...` only for an admitted, tier-known checkable class

**Cap at 5 parallel agents.** Diminishing returns past that.

**Output template:**

```markdown
## Question
<verbatim the user question>

## Claude's answer
<...>

## Codex's answer
<from `cx plan-review read-only /tmp/consensus-codex.md "<question>"`>

## Gemini's answer
<piped from `gemini --skip-trust -p "<question>"`>

## lane answer (admitted class + tier only)
<piped from `lane <class> --tier ...` only when the prompt is admitted and its tier is known>

## Verdict
<2-3 sentence synthesis: where they agree, where they diverge, what's the strongest argument, what should the user actually do>
```

---

## Lane availability detection

The router only routes to lanes that are currently alive. Before any cross-vendor dispatch:

```bash
# Cheap filesystem + HTTP check (~5ms)
cx --self-test offline              # Codex dispatch + routes resolve (no model call)
gemini --version 2>/dev/null         # Gemini CLI installed?
lane doctor                                                     # lane CLI and router readiness
```

If any lane is broken, refuse cross-vendor routing for that session and either ask the user to re-auth / restart, or stay on Claude. **Never silently degrade.** A downed lane at 3 AM should fail loud, not silently fall back to a Claude loop that drains the quota.

A deeper smoke test (~30s) verifies the user's auth and that each lane can actually return content. Run at session start or after re-auth, not on every call.

---

## Cost discipline & subscriptions

Example stack:

| Lane | Subscription | Notes |
|---|---|---|
| **Claude** | subscription plan | Thinking, planning, orchestration, tight-context coding, and spec-compliance review. Use directly when it is the shortest reliable path. |
| **Codex** | subscription plan | High-throughput implementation, diff review, and autonomous `/goal` loops. Skip dispatch on trivial work because of latency, not capability. |
| **Gemini** | API key or subscription | Use Pro for anything multimodal or > 100K tokens. Strongest on read/analysis/scoping and grounded research; authors code too when it fits (no restriction). For big multi-file work it often outputs a written spec that any lane implements per-file. Deep Research has its own per-day allowance via the web UI. |
| **freellmapi** | $0 (local) | Effectively unlimited across ~20 free providers via health-aware fallback. Grunt work only — bounded, low-stakes per-item. Never one-shot critical output. |

**How each lane authenticates:**
- Codex: `codex login` → OAuth via your ChatGPT account
- Gemini: `GEMINI_API_KEY` in `~/.gemini/.env` (see the gemini wrench)
- Claude: already authenticated (Claude Code session)

Codex and Claude need no API key. Gemini uses `GEMINI_API_KEY` for the CLI and for the optional MCP delegation path (`@gemini` inside Claude Code).

**Cost ordering (cheapest → most expensive per equivalent task):** freellmapi → Gemini → Codex → Claude. Use a free lane only where `lane admission` shows the class admitted; otherwise Claude or Codex. Private/PII → `auto:local` only.

---

## What the router does NOT do

- It does not replace the wrenches — `codex`, `gemini`, `codex-goal`, `freellmapi` remain the execution layer.
- It does not auto-route trivial tasks. One-liners, typo fixes, single-file reads → Claude alone.
- It does not silently swap brains mid-task. Every handoff is announced in one short sentence: *"Routing to Codex for review"* / *"Handing the video to Gemini"* / *"Bulk classification → freellm"*.
- It does not route important one-shot work to freellm. The free lane is for bounded grunt only.
- It does not auto-fire on grunt signals when the work is one-shot critical. Auto-fire applies when the prompt is per-item AND bounded AND low-stakes.

---

## Wrenches

| Wrench | Path | What it does |
|---|---|---|
| **codex** | `wrenches/codex.md` | Codex CLI: review (diff review) / challenge (adversarial) / consult (ask anything). Auth probe + filesystem boundary instruction baked in |
| **codex-goal** | `wrenches/codex-goal.md` | Long-running autonomous loop dispatcher. Template → spec → spawn Codex `/goal` → babysit → handoff. IRON RULE: never kill the Codex process |
| **loop-sense** | `wrenches/loop-sense.md` | Detects loop-shaped tasks and proposes budget-declared runbook drafts before any loop starts |
| **gemini** | `wrenches/gemini.md` | Gemini CLI + MCP delegation. 1M context window, multimodal, deep research via gemini.google.com (`web act` drive) |
| **freellmapi** | `wrenches/freellmapi.md` | Local FreeLLMAPI router reached through `lane <class> --tier ...`; free work requires `lane admission`, and private/PII stays `auto:local` |

---

## Setup checklist (one-time)

If any lane is not yet ready:

```bash
# Codex (OpenAI)
npm install -g @openai/codex@0.162.1
codex login          # OAuth via existing ChatGPT account

# Gemini CLI
npm install -g @google/gemini-cli@0.63.0
gemini               # reads GEMINI_API_KEY from ~/.gemini/.env (see the gemini wrench)
```

**freellmapi** is reached through `lane`; verify the CLI and router with `lane doctor`, then check `lane admission` before a task run.

---

## State files

Helper scripts live at `scripts/` (built in Phase 5, acceptance-tested 2026-05-28):

| Script | Purpose | State file |
|---|---|---|
| `scripts/preflight.py [--lane <name>] [--json]` | Deep auth + lane health check (claude/codex/gemini/freellm). Run at session start or after re-auth. Exit 0 if all healthy, 1 if any down. | n/a (probes live) |
| `scripts/track_attempt.py {get\|inc\|reset\|list} <task_id> [--max N]` | No-self-rescue counter. `inc` returns new count + exit 2 when count ≥ max (default 2) — signals "forced Codex rescue." | `~/.claude/state/attempt-counters.json` |
| `scripts/log_decision.py --lane X --task "..." --reason "..." [--project <slug>]` | Append routing decision to vault: `Actions/routing-defaults.md § Routing log` by default, `Projects/<slug>/decisions.md` with `--project`. 60s dupe-skip. | (writes to vault directly) |

State files (governed by `~/.claude/state/` per the no-permission-prompts rule):

| File | Purpose | Atomic? |
|---|---|---|
| `~/.claude/state/attempt-counters.json` | No-self-rescue counter (written by track_attempt.py) | Yes (temp + rename) |

**Migrating from old gstack paths:** the old `~/.gstack/three-brain/` state is read-only-archive; the new router uses `~/.claude/state/` (shared with `guard`'s freeze-scope file).

---

## See also

- [wrenches/codex.md](wrenches/codex.md) — Codex CLI wrapper, 3 modes
- [wrenches/codex-goal.md](wrenches/codex-goal.md) — long-running autonomous loops
- [wrenches/loop-sense.md](wrenches/loop-sense.md) — loop-shaped work detector and draft proposer
- [wrenches/gemini.md](wrenches/gemini.md) — Gemini delegation
- [wrenches/freellmapi.md](wrenches/freellmapi.md) — local FreeLLMAPI router
- [`AGENTS.md`](../../../AGENTS.md) — operating manual (Claude/Codex split, hard rules)
- [`DECISION_MAP.md`](../../../DECISION_MAP.md) — task → tool routing
