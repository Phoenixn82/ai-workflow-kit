---
name: freeze
description: Wrench inside the `guard` mechanic. Restricts file edits to a specific directory for the session. Edits to files outside the boundary are **blocked**, not just warned. Pair the `--clear` flag to remove the boundary (formerly the separate `unfreeze` skill, now folded in). Fires on "freeze edits to <path>", "restrict edits", "only edit <folder>", "lock down edits", "unfreeze", "unlock edits", "remove freeze", "allow all edits".
---

# freeze — directory edit boundary

Lock file edits to a specific directory. Any Edit or Write targeting a file outside the boundary is **blocked** (the PreToolUse Edit/Write hook returns `permissionDecision: "deny"`). Reads, globs, grep, and bash commands are unaffected.

Use when debugging to prevent accidental "fixing" of unrelated code, or when scoping changes to one module. Especially useful when working in a multi-project repo where it's easy to drift.

---

## Two operations: set + clear

### Set / change the boundary

When the user says "freeze edits to <path>" or "lock edits to this folder":

1. Ask for the path (if not provided in the invocation). Use AskUserQuestion with a text input — no multiple choice — labelled *"Which directory should I restrict edits to?"*
2. Resolve to absolute path and ensure trailing slash:
   ```bash
   FREEZE_DIR=$(cd "<user-provided-path>" 2>/dev/null && pwd)
   FREEZE_DIR="${FREEZE_DIR%/}/"
   ```
3. Write to state file:
   ```bash
   mkdir -p ~/.claude/guard-state
   echo "$FREEZE_DIR" > ~/.claude/guard-state/freeze-dir.txt
   ```
4. Tell the user:
   > *"Edits restricted to `<path>/`. Any Edit or Write outside this directory will be blocked. To change the boundary, run `freeze` again with a new path. To remove it entirely, run `freeze --clear`."*

If a boundary is already set when `freeze` is invoked with a new path, the new boundary replaces the old. Tell the user the swap: *"Previous boundary was `<old>`, now `<new>`."*

### Clear the boundary (formerly `unfreeze`)

When the user says "unfreeze" / "unlock edits" / "remove freeze" / "allow all edits" / `freeze --clear`:

1. Read the previous boundary (for telling the user what was cleared):
   ```bash
   PREV=$(cat ~/.claude/guard-state/freeze-dir.txt 2>/dev/null || echo "")
   ```
2. Remove the state file:
   ```bash
   rm -f ~/.claude/guard-state/freeze-dir.txt
   ```
3. Tell the user:
   - If `$PREV` was set: *"Freeze boundary cleared (was: `<previous>`). Edits allowed everywhere."*
   - If no boundary: *"No freeze boundary was set."*

---

## Boundary semantics

The trailing `/` is load-bearing:

| Boundary | File path | Result |
|---|---|---|
| `~/projects/foo/` | `~/projects/foo/bar.ts` | Allowed (starts with boundary) |
| `~/projects/foo/` | `~/projects/foo-old/bar.ts` | **Blocked** (does NOT start with boundary) |
| `~/projects/foo/` | `~/projects/foo/sub/baz.ts` | Allowed |
| `~/projects/foo/` | `~/elsewhere/file.ts` | Blocked |

Without the trailing slash, `/projects/foo` would match `/projects/foo-old` — exactly the accident the boundary is meant to prevent.

The hook script resolves Edit/Write `file_path` to absolute, then string-prefix checks against the boundary. Symlinks are followed; symlink-out attacks aren't a concern because this is accident-prevention, not a security boundary.

---

## State file

Single file: `~/.claude/guard-state/freeze-dir.txt`. Contains exactly the absolute path with trailing slash, no newline padding. Absent file = no boundary = all edits allowed.

The hook script reads this file on every Edit / Write / MultiEdit / NotebookEdit invocation. Read cost is negligible (<1 KB, hot cache).

---

## What freeze does NOT do

- **Does not block reads, globs, or grep.** the user can still read anywhere. Only mutations are restricted.
- **Does not block Bash commands that modify files.** `sed -i ...`, `python -c "open(...).write(...)"`, `git checkout`, `cp`, `mv` — all run unrestricted. Freeze is the Edit/Write tool layer of defense, not exhaustive. Use `careful` for bash-side guardrails (separate wrench).
- **Does not survive across sessions.** State file persists, but each new session reads it on first invocation. To re-activate the same boundary next session, the file is already there — invoking `freeze` with no args could re-affirm the existing boundary (if the user wants this UX, document it as a default).
- **Does not validate the path exists or is a directory.** The hook just does a string-prefix match. If the user sets `~/typo-path/` as the boundary, every edit gets blocked because no real file starts with that prefix. Tell the user to verify the path resolved correctly after setting.
- **Does not nest.** Only one boundary at a time. Setting a new boundary replaces the previous.

---

## When to invoke

- Debugging a bug in one module — freeze to that module so the bugfix can't accidentally rewrite an adjacent module
- Refactoring one feature — freeze to its directory so the refactor stays scoped
- Working in a monorepo with multiple apps — freeze to the app you're touching
- Reviewing a PR and wanting to make safe inline edits — freeze to the PR's changed paths' common parent
- Anytime the user wants "only this folder, nothing else"

When unsure of scope, **don't freeze**. False blocks are friction. Freeze when the scope is clear.

---

## Hook wiring (opt-in)

The PreToolUse Edit / Write / MultiEdit / NotebookEdit hooks point at `_system/skills/guard/scripts/check-freeze.sh`. The script:

1. Reads `~/.claude/guard-state/freeze-dir.txt` (returns ok if absent)
2. Reads `file_path` from the tool input JSON
3. Resolves `file_path` to absolute
4. If `file_path` starts with the boundary → return `permissionDecision: "allow"`
5. Else → return `permissionDecision: "deny"` with message *"freeze: <file_path> is outside the boundary <boundary>"*

The script exists at `scripts/check-freeze.sh` (Phase 5, acceptance-tested 2026-05-28). It is intentionally NOT wired into `settings.json` — guard is opt-in, so the hook is registered only when the user invokes `freeze`.

---

## See also

- [../SKILL.md](../SKILL.md) — the guard mechanic
- [careful.md](careful.md) — destructive Bash command warner (paired safety wrench)
