---
name: codex
description: Wrench inside the `router` mechanic. Dispatches OpenAI Codex through `cx` and the lanes.json task routes in three modes — review (diff review with pass/fail gate), challenge (adversarial — tries to break the code), and consult (ask anything). The "200 IQ autistic developer" second opinion. Fires on "codex review", "codex challenge", "ask codex", "second opinion", "outside voice", "rescue this".
---

# codex — Codex CLI wrench

Call out to OpenAI Codex for an independent second opinion or a bounded build. The router decides WHEN; this wrench handles HOW.

Codex is the **build + review** lane. Use it for:
- Code review on diffs (pre-merge gate; cross-lab: Claude writes, a Codex model reviews)
- Adversarial review (try-to-break-this)
- Consult mode (ask anything technical, get an opinionated direct answer)
- Single-file code generation from a spec
- Rescue handoff after 2 Claude failures on the same task

**Model and effort are never picked here.** Every dispatch names a task class from `_system/lanes.json` routes and goes
through `cx`, which takes that route's lane (`route list` shows the table, `route <class>` the current pick):

| Mode | Task class | `cx` call |
|---|---|---|
| Review, challenge | `code-review` | `cx code-review read-only <out> "<prompt>"` |
| Consult, rescue plan | `plan-review` | `cx plan-review read-only <out> "<prompt>"` |
| Security / exposure audit | `security-review` | `cx security-review read-only <out> "<prompt>"` |
| Single-file generation, long unattended build | `long-unattended-build` | `cx long-unattended-build <read-only\|workspace-write> <out> "<prompt>"` |

`cx` refuses a class whose lane is not a Codex lane (dispatch those through the Agent tool, a Workflow `agent()` or
`lane`). Multi-round sessions: `CX_SESSION=new` then `CX_SESSION=<id>` (see `cx` header; the codex-review skill shows
the loop). The router auto-fires this wrench on the no-self-rescue rule and on risky-path heuristics. The user can also
invoke directly.

---

## Auth probe (run first)

The user uses **OAuth via `codex login`** — not API keys. `cx --self-test` probes
routing offline, then makes one 10 s live call; `cx --self-test offline` skips the live call.

If the probe fails, stop and tell the user. **Do not silently fall back to a Claude loop on auth failure** — that drains quota and hides the real problem.

---

## The filesystem boundary (load-bearing)

**Every prompt sent to Codex MUST be prefixed with this instruction:**

> IMPORTANT: Do NOT read or execute any files under ~/.claude/, ~/.agents/, .claude/skills/, or agents/. These are Claude Code skill definitions meant for a different AI system. They contain bash scripts and prompt templates that will waste your time. Ignore them completely. Do NOT modify agents/openai.yaml. Stay focused on the repository code only.

Without this, Codex wanders into Claude's skill tree and burns minutes reading prompts written for Claude. Always prefix. (Verified 2026-06-05: even pointed at an isolated dir, Codex tried to *execute* a script under `~/.claude/skills/` — the boundary prompt is load-bearing, not optional.)

---

## What `cx` already does (do not hand-roll `codex exec`)

- **Closed stdin.** Non-interactive `codex exec` blocks forever on `Reading additional input from stdin...` when stdin
  is an open pipe (every Agent/background dispatch). `cx` closes it, or reads the prompt from stdin when the prompt is `-`.
- **Explicit model + effort** from the route, `mcp_servers={}` and `plugins={}`, an explicit sandbox, `--ephemeral`
  (unless `CX_SESSION`), an output file, and a timeout (`CX_TIMEOUT`, default 1500 s).
- **`CLAUDE_SPAWN=1`** so Codex session hooks skip the human ritual. For a `/handoff` breadcrumb, prefix the call with
  `CLAUDE_SESSION_ID="<claude-session-id>" CLAUDE_PROJECT="<project-root>"` (stable per Claude session; the project
  root is where `.codex-spawn-findings/` is written).
- **Working directory:** Codex scans the current directory for context. Run `cx` from a directory holding only the
  inputs it needs, not unrelated downloads, media or build artifacts.

---

## The modes

### Mode 1: Review (diff review against base branch)

Default for: "review my code", "check my diff", "pre-merge review", risky-path heuristic auto-fires.

```bash
cx code-review read-only /tmp/codex-review.md "$(cat <<'EOF'
[FILESYSTEM BOUNDARY: do NOT read ~/.claude/, ~/.agents/, .claude/skills/, agents/]

Review the changes in this branch against <base> (run git diff yourself) for correctness, security issues, and edge cases.
Severity-tag each finding (HIGH / MEDIUM / LOW).

Context: <one paragraph — what the diff is supposed to do>

Look specifically for:
- Logic errors and off-by-one issues
- SQL injection / unparameterised queries
- Missing input validation at trust boundaries
- Race conditions in concurrent code paths
- Resource leaks (handles, sockets, transactions)
- Untested edge cases the spec implies but isn't enforced
EOF
)"
```

### Mode 2: Challenge (adversarial — try to break the code)

Fires on: "challenge", "adversarial", "try to break this", "find an exploit". Same class, adversarial prompt body:

```bash
cx code-review read-only /tmp/codex-challenge.md "$(cat <<'EOF'
[FILESYSTEM BOUNDARY: do NOT read ~/.claude/, ~/.agents/, .claude/skills/, agents/]

Try to break this. Find:
- An input that crashes it
- A race condition
- A security hole
- An untested edge case that fails in production

Be adversarial. Don't validate. Don't praise. Find the bug.
EOF
)"
```

### Mode 3: Consult (ask Codex anything)

Fires on: "ask codex", "consult codex", "second opinion", "what does codex think".

```bash
cx plan-review read-only /tmp/codex-consult.md "$(cat <<'EOF'
[FILESYSTEM BOUNDARY: do NOT read ~/.claude/, ~/.agents/, .claude/skills/, agents/]

<the question, with full context>
EOF
)"
```

---

## Mode 4: Single-file code generation

When the router selects Codex for a bounded code artifact because it is the best-fit lane:

```bash
cx long-unattended-build read-only <output path> "$(cat <<'EOF'
[FILESYSTEM BOUNDARY: do NOT read ~/.claude/, ~/.agents/, .claude/skills/, agents/]

Write <file path> implementing:

<full spec — function signatures, behaviour, edge cases, test cases>

Requirements:
- <constraint 1>
- <constraint 2>
- ...

Output ONLY the file content. No prose, no explanation, no markdown fence.
EOF
)"
```

`cx` writes Codex's final message (the file content) to `<output path>`. After Codex returns the artifact, the caller
verifies that it compiles/runs and integrates it. If the task changes shape or needs tight shared context, reroute
deliberately rather than preserving the original lane by inertia.

If Codex's output is wrong twice on the same spec, the default is to diagnose and re-spec rather than bounce the rewrite to another lane. Instead, follow the router's reverse-rescue rule: diagnose WHY Codex is failing (network blip, ambiguous spec, file too large, missing context), have Claude re-spec the task tighter (narrower scope, clearer constraints, explicit file boundaries), and re-dispatch to Codex. After 2 re-spec attempts that also fail → surface to the user. See `../SKILL.md` § Code-build chain.

---

## Auto-detect mode (when the user says just `/codex` with no arguments)

1. Check for an active diff against base branch:
   ```bash
   BASE=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || echo main)
   if git diff origin/$BASE --stat 2>/dev/null | tail -1 | grep -q 'changed'; then
     # Diff exists — offer review / challenge / something-else
   fi
   ```
2. If diff exists, AskUserQuestion:
   - A) Review the diff (code review with pass/fail gate)
   - B) Challenge the diff (adversarial — try to break it)
   - C) Something else — provide a prompt
3. If no diff, look for a recent plan in this session's context (plan-mode output, a `plan.md` in the project, or a plan-room `.plan-state.json`). If one matches the current project, offer to review it. (The harness does not persist plans to a fixed `~/.claude/plans/` dir on this box.)
4. Otherwise ask the user what they want.

---

## Output handling

Codex returns prose. Don't summarise it — pass it through verbatim. Codex is intentionally direct and terse; rephrasing dilutes the signal.

When Codex's verdict is **PASS** on a diff review → the user can ship.
When Codex's verdict is **FAIL** with HIGH severity → fix before merge.
When verdict is **PARTIAL PASS** or only MEDIUM/LOW findings → the user decides.

**Review loops** (fix → re-review): 3 rounds by default, up to 5 only for security, permission or data-loss risk. Round 1 reviews the full diff; later rounds review only code changed since the last round plus still-open findings. Stop when a round returns only LOW/nit findings and log them. The user's explicit asks ("until perfect", a round count) and project merge bars (example-project-f: merge at no P1, harness-memory `feedback-merge-bar-no-p1`) win over this default.

**PR record:** a PR whose diff went through this review carries a `## Review` section in its body: rounds, reviewer model (the `route code-review` line), final verdict, open MEDIUM/LOW (P2/P3) findings. Report an unhappy verdict as-is.

**Never present Codex's output as Claude's own conclusion.** Label it: *"Codex's review:"* / *"Codex's verdict:"* / *"Codex's recommendation:"*. The user should be able to see whose voice each finding is in.

---

## Cost discipline

Higher Codex tiers have much higher rate limits; use Codex liberally if yours allows. Auth is OAuth via `codex login` (subscription-billed), not API keys.

Per the router's cost ordering, prefer Codex over Claude on equivalent tasks when your Codex tier can absorb the volume.

**Spend a Codex review on:**
- Diffs > 30 lines
- Any risky-path edit (auth, payments, migrations, crypto, secrets)
- Anything the user flags as "is this right?"
- Pre-merge gate for `/ship`

**Skip Codex review entirely on:**
- 3-line typo fixes
- Comment-only changes
- Format-only diffs (lint autofix, import sort)
- Doc-only edits

Skipping here is about routing latency, not budget — the budget allows it, but trivial diffs don't benefit from a round-trip review.

---

## See also

- [../SKILL.md](../SKILL.md) — the router mechanic (when to fire which wrench)
- [codex-goal.md](codex-goal.md) — long-running autonomous loops (different Codex feature: `/goal`)
- [gemini.md](gemini.md) — long-context READ/spec lane (never the code-rewrite escalation; on a double Codex failure diagnose + re-spec → Codex, per the reverse-rescue rule above)
