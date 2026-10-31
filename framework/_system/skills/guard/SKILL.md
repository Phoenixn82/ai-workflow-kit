---
name: guard
description: "Safety mechanic — destructive command warnings + directory-scoped edit boundaries. **Fully opt-in, never default.** the user runs with `dangerously-skip-permissions` enabled, so guard's hooks NEVER activate automatically — only when the user explicitly invokes a mode (touching prod, debugging live systems, working in shared infra). Always-core means the mechanic is loaded at session start so the knowledge is oriented, but the hooks sit dormant until invoked. Two modes run separately or together. `careful` warns before rm -rf / DROP TABLE / force-push / git reset --hard / kubectl delete and the rest. `freeze` blocks file edits outside a chosen directory. Fires on explicit invocation only: \"guard mode\", \"be careful\", \"safety mode\", \"prod mode\", \"lock it down\", \"maximum safety\", \"freeze edits\", \"restrict edits to <path>\", \"only edit this folder\", \"unfreeze\", \"unlock edits\"."
---

# guard — safety rails (opt-in only)

Three protections, one mechanic:

| Mode | What it does | How it blocks |
|---|---|---|
| **careful** | Warns before destructive bash commands (rm -rf, DROP TABLE, force-push, etc.) | PreToolUse Bash hook returns `permissionDecision: "ask"` — the user can override |
| **freeze** | Restricts file edits to a chosen directory; files outside are **blocked** | PreToolUse Edit/Write hook returns `permissionDecision: "deny"` — hard block |
| **public-push gate** | Blocks pushes or private-to-public flips for public GitHub repos until the secret scanner, denylist and Codex review pass | Global Git `pre-push` hook and the guard `gh` wrapper (`bin/gh`); private repos are skipped |

`careful` and `freeze` can run alone or together. The combined `guard` mode activates both.

The public-push gate is different from `careful` and `freeze`: it is a trigger-based Git safety rail, not a Claude tool hook. It only runs when a human pushes to a GitHub remote or uses the visibility-flip wrapper, so it does not violate the "no background work" rule.

## Fully opt-in — never the default

Per AGENTS.md hard rule #10, the user runs with `dangerously-skip-permissions` enabled. The harness-level `settings.json` already has `skipDangerousModePermissionPrompt: true`. **Guard's hooks are NEVER auto-wired into `settings.json`.** They only fire when the user invokes a mode, and even then they're scoped to that session.

"Always-core" here means:
- ✅ The mechanic's SKILL.md and wrench docs load at session start (so Claude knows what guard is and how to invoke it if asked)
- ❌ The hooks do NOT auto-register in `settings.json`
- ❌ No `permissionDecision: "ask"` ever fires by default
- ❌ No `permissionDecision: "deny"` ever fires by default

The user's explicit "guard mode" / "be careful" / "freeze edits to X" turns the protections on for the current session. Without that explicit invocation, every tool call goes through clean.

---

## When to fire which mode

| Phrase | Mode |
|---|---|
| "be careful" / "prod mode" / "careful mode" / "watch out" | `careful` only |
| "freeze edits to <path>" / "restrict edits" / "only edit <folder>" / "lock down edits" | `freeze` only |
| "guard mode" / "full safety" / "lock it down" / "maximum safety" | both (combined) |
| "unfreeze" / "unlock edits" / "remove freeze" / "allow all edits" | `freeze --clear` |

If the user touches prod, is debugging a live system, or working in a shared environment without specifying which mode, default to **both** (the combined guard mode). Better to over-protect than under.

---

## State files (one per protection)

| File | Purpose | Lifecycle |
|---|---|---|
| `~/.claude/guard-state/freeze-dir.txt` | Holds the active freeze boundary path. Absent = no boundary | Set by `freeze`, cleared by `freeze --clear` (or end of session — the file persists but hooks aren't registered next session unless guard is invoked again) |
| `~/.claude/guard-state/careful-active.flag` | Marker that careful is on. Absent = inactive | Set by `careful` invocation, cleared by the user or end of session |

**Migrating from gstack:** the old `${CLAUDE_PLUGIN_DATA:-$HOME/.gstack}/freeze-dir.txt` state lives outside the new structure. New state goes under `~/.claude/guard-state/` for portability (no plugin-dir dependency).

---

## Hook wiring (Phase 4+ deliverable)

The mode hooks need to be registered in `~/.claude/settings.json` for them to fire. **The scripts exist and are acceptance-tested (`scripts/check-careful.sh`, `scripts/check-freeze.sh`, Phase 5 / 2026-05-28); they are intentionally NOT wired into `settings.json`** — guard is opt-in, so registration happens only when the user invokes a mode. When wired, the hook config looks like:

```json
"PreToolUse": [
  {
    "matcher": "Bash",
    "hooks": [
      {
        "type": "command",
        "command": "bash $HOME/AI_Projects/_system/skills/guard/scripts/check-careful.sh"
      }
    ]
  },
  {
    "matcher": "Edit|Write|MultiEdit|NotebookEdit",
    "hooks": [
      {
        "type": "command",
        "command": "bash $HOME/AI_Projects/_system/skills/guard/scripts/check-freeze.sh"
      }
    ]
  }
]
```

The hook scripts (`check-careful.sh` and `check-freeze.sh`) read the relevant state file, check the incoming tool input, and return the right `permissionDecision`. Both are written and live at `scripts/` (acceptance-tested 2026-05-28).

When the hooks are live, the mechanic's modes are session-scoped: invoking `careful` writes the active flag; ending the session leaves the flag in place (so next session resumes the same posture), or the user can clear with `careful --off`.

---

## Setup flow (when the user invokes a mode)

### `careful` invocation
1. Touch `~/.claude/guard-state/careful-active.flag`
2. Tell the user: *"Careful mode active. Destructive bash commands (rm -rf, DROP TABLE, force-push, git reset --hard, kubectl delete, docker rm -f, etc.) will require confirmation before running. To deactivate, run `careful --off` or end the session."*

### `freeze` invocation
1. Ask the user which directory (text input via AskUserQuestion, no choices — typed path)
2. Resolve to absolute path, ensure trailing slash
3. Write the path to `~/.claude/guard-state/freeze-dir.txt`
4. Tell the user: *"Edits restricted to `<path>/`. Any Edit or Write outside this directory will be blocked. To change the boundary, run `freeze` again with a new path. To remove it entirely, run `freeze --clear`."*

### Combined `guard` invocation
1. Activate careful (as above)
2. Activate freeze (as above)
3. Tell the user: *"Guard mode active. (1) Destructive commands warn before running. (2) Edits restricted to `<path>/`. To remove the edit boundary: `freeze --clear`. To deactivate everything: end the session or clear both individually."*

### `freeze --clear` (formerly `unfreeze`)
1. Read previous boundary from state file
2. Delete `~/.claude/guard-state/freeze-dir.txt`
3. Tell the user: *"Freeze boundary cleared (was: `<previous>`). Edits allowed everywhere."*

---

## What guard does NOT do

- **Does not auto-activate.** the user turns it on. The mechanic loads at session start (always-core), but the protections sit dormant until invoked.
- **Does not protect against scripted side-effects.** Bash commands like `sed -i`, `python -c "open(...).write(...)"`, `git filter-branch`, or anything that mutates files via a tool other than Edit/Write will bypass the freeze hook. Freeze is an accident-prevention boundary, not a security boundary.
- **Does not block reads, globs, or non-mutating commands.** Read / Glob / Grep are unaffected. Bash commands that aren't on the destructive-pattern list are unaffected.
- Credential filename deny patterns for future shell-read parity are documented in [reference/credential-file-patterns.md](reference/credential-file-patterns.md); no runtime shell-read hook exists yet.
- **Does not replace the destructive-action confirmation in the system prompt.** Claude already pauses for confirmation on rm -rf and similar from the base instructions. Guard layers a programmatic check on top so the confirmation happens even when Claude would have proceeded.
- **Does not validate file paths against per-project guardrails.** The freeze boundary is one directory per session. Per-project rules (like "don't touch /lib/db without a migration") belong in the project's CLAUDE.md or a custom skill.

---

## Public GitHub push gate

The global Git hook lives at `githooks/pre-push` and is installed with:

```bash
git config --global core.hooksPath "$HOME/AI_Projects/_system/skills/guard/githooks"
```

Behavior:
- Non-GitHub remotes are skipped.
- Private GitHub repos and missing/not-yet-created repos are skipped.
- Public GitHub repos are gated fail-closed.
- `GUARD_ALLOW_PUBLIC_PUSH=1` skips only the Codex review for one deliberate operation and prints a loud banner; the secret scanner and denylist still run and a hit still blocks.
- After the gate passes, the hook chains any repo-local `.git/hooks/pre-push` so Husky, secret-firewall, and project hooks still run.

For private-to-public flips, the guard `gh` wrapper (`bin/gh`, symlinked at `~/.local/bin/guard/gh`, that dir first on PATH) intercepts `gh repo edit ... --visibility public` and `gh api` calls that set `private=false` or `visibility=public`. It runs `scripts/public_push_gate.py --mode publicize --repo <owner/repo>` from a local clone of that repo (the gate fetches it and scans every ref) and calls the real `gh` only on PASS. `scripts/gh-publicize.ps1` is the legacy Windows path.

See `PUBLIC_PUSH_GATE.md` for operator details and test controls.

---

## Boundary semantics (for freeze)

The trailing `/` on the freeze directory is load-bearing:
- `freeze ~/projects/foo` becomes boundary `~/projects/foo/`
- File path `~/projects/foo/bar.ts` → starts with boundary → allowed
- File path `~/projects/foo-old/bar.ts` → does NOT start with boundary → blocked
- File path `~/projects/foo/sub/baz.ts` → starts with boundary → allowed

Without the trailing slash, `/projects/foo` would match `/projects/foo-old` and that's exactly the accident the boundary is meant to prevent.

---

## Destructive command patterns (for careful)

The `careful` mode catches these patterns in Bash tool input:

| Pattern | Example | Why it's dangerous |
|---|---|---|
| `rm -rf` / `rm -r` / `rm --recursive` | `rm -rf /var/data` | Recursive delete |
| `DROP TABLE` / `DROP DATABASE` | `DROP TABLE users;` | Schema/data loss |
| `TRUNCATE` | `TRUNCATE orders;` | Data loss |
| `git push --force` / `git push -f` | `git push -f origin main` | History rewrite |
| `git reset --hard` | `git reset --hard HEAD~3` | Uncommitted work loss |
| `git checkout .` / `git restore .` | `git checkout .` | Uncommitted work loss |
| `kubectl delete` | `kubectl delete pod` | Production impact |
| `docker rm -f` / `docker system prune` | `docker system prune -a` | Container/image loss |

**Safe exceptions** (allowed without warning — these are universally rebuildable):
- `rm -rf node_modules`
- `rm -rf .next`
- `rm -rf dist`
- `rm -rf __pycache__`
- `rm -rf .cache`
- `rm -rf build`
- `rm -rf .turbo`
- `rm -rf coverage`
- `rm -rf .venv`

When a destructive pattern fires, the hook returns `permissionDecision: "ask"` with a warning. The user can proceed or cancel.

---

## Wrenches

| Wrench | Path | What it does |
|---|---|---|
| **careful** | `wrenches/careful.md` | Destructive command warner. Active flag + PreToolUse Bash hook |
| **freeze** | `wrenches/freeze.md` | Directory edit boundary. Set / change / clear (formerly `freeze` + `unfreeze`, now one wrench with `--clear`) |

---

## Helper scripts

PreToolUse hook bodies at `scripts/` (built in Phase 5, acceptance-tested 2026-05-28). Hooks return `{"permissionDecision":"allow"|"ask"|"deny"}` per the contract; NEVER `ask` from default-on path.

| Script | Hook contract | What it does |
|---|---|---|
| `scripts/check-careful.sh` | PreToolUse Bash | Pattern-matches destructive commands (rm -rf, git push --force, DROP TABLE, kubectl delete, kill -9, etc.). Emits `ask` on match, `allow` otherwise. NEVER `deny`. The user overrides at the ask prompt. |
| `scripts/check-freeze.sh` | PreToolUse Edit/Write | Reads `~/.claude/guard-state/freeze-dir.txt`. If absent → allow. Else checks if `file_path` is under the scope path via Python `os.path.commonpath` (cross-platform safe). Emits `deny` on out-of-scope. |
| `scripts/public_push_gate.py` | Git pre-push / publicize wrapper | Queries GitHub visibility with `gh`; skips private/missing repos; fail-closed gates public targets with `betterleaks` (gitleaks fallback, hardened via `scripts/scanner.toml`), denylist scans over the full pushed history, and a `codex exec` review whose model and effort come from the lanes.json `security-review` route (`GUARD_CODEX_MODEL` overrides the model). |
| `bin/gh` | PATH-first `gh` wrapper | Runs the publicize gate before any `gh repo edit --visibility public` or `gh api` private=false/visibility=public call; everything else passes straight to the real `gh`. |
| `scripts/gh-publicize.ps1` | Legacy Windows visibility wrapper | Runs the same gate against the current repo, then flips GitHub visibility public only on PASS. |

The `freeze` wrench writes `~/.claude/guard-state/freeze-dir.txt`; `freeze --clear` removes it. Hooks are NOT auto-wired into `settings.json` — fully opt-in via wrench invocation per [[Actions/permission-defaults]].

---

## See also

- [wrenches/careful.md](wrenches/careful.md) — destructive command warner
- [wrenches/freeze.md](wrenches/freeze.md) — directory edit boundary (set + clear)
- [`AGENTS.md`](../../../AGENTS.md) — operating manual (executing actions with care rules)
- [`router/SKILL.md`](../router/SKILL.md) — sister always-core mechanic
- [`second-brain/SKILL.md`](../second-brain/SKILL.md) — third always-core mechanic
- `build/wrenches/onboarding-guard.md` — semantically related (redirect-logic checklist) but folded to `build`, not here, because it's code-design discipline not file-edit safety
