---
name: careful
description: Wrench inside the `guard` mechanic. **Opt-in only — never default.** the user runs with `dangerously-skip-permissions` enabled; this wrench's hook NEVER auto-fires. Only activates when the user explicitly invokes "be careful" / "safety mode" / "prod mode" / "I'm touching prod". When active, warns before destructive bash commands (rm -rf, DROP TABLE, force-push, git reset --hard, kubectl delete, docker rm -f, TRUNCATE, etc.) and lets the user override each warning.
---

# careful — destructive command warner (opt-in)

When the user touches prod, debugs a live system, or works in a shared environment, careful mode catches destructive bash commands before they fire. Each warning is overridable — careful is a speed bump, not a wall.

**Default state: OFF.** the user runs with `dangerously-skip-permissions` so routine tool calls go through without any "ask" prompt. This wrench only fires when the user explicitly invokes a careful-flavoured trigger. No automatic activation.

When active, the hook returns `permissionDecision: "ask"` on a matched destructive pattern, meaning the user sees the proposed command + the destruction it would cause and chooses to proceed or cancel. Patterns that don't match run normally. When inactive (the default), the hook doesn't run at all.

---

## Activating

```bash
mkdir -p ~/.claude/guard-state
touch ~/.claude/guard-state/careful-active.flag
echo "Careful mode active."
```

Tell the user:

> "Careful mode active. Destructive bash commands (rm -rf, DROP TABLE, force-push, git reset --hard, kubectl delete, docker rm -f, etc.) will pause for confirmation before running. To deactivate, run `careful --off` or end the session."

The PreToolUse Bash hook must already be registered in `~/.claude/settings.json` pointing at `_system/skills/guard/scripts/check-careful.sh` (the script exists; it gates on `~/.claude/guard-state/careful-active.flag`, so it allows everything until `careful` is invoked). If the registration isn't present, tell the user the hook wiring is opt-in and not yet registered.

---

## Deactivating

```bash
rm -f ~/.claude/guard-state/careful-active.flag
echo "Careful mode off."
```

The hook script reads the active flag on every Bash invocation. With the flag absent, the hook is a no-op — zero overhead.

---

## What gets caught

| Pattern | Example | Why it's dangerous |
|---|---|---|
| `rm -rf` / `rm -r` / `rm --recursive` (outside safe-exceptions) | `rm -rf /var/data` | Recursive delete of real content |
| `DROP TABLE` / `DROP DATABASE` | `DROP TABLE users;` | Schema/data loss |
| `TRUNCATE` | `TRUNCATE orders;` | Data loss |
| `git push --force` / `git push -f` | `git push -f origin main` | History rewrite, can lose teammate commits |
| `git reset --hard` | `git reset --hard HEAD~3` | Uncommitted work loss |
| `git checkout .` / `git restore .` (without specific paths) | `git checkout .` | Uncommitted work loss |
| `kubectl delete` (any form) | `kubectl delete pod` / `kubectl delete -f manifest.yaml` | Production impact |
| `docker rm -f` / `docker system prune` / `docker volume prune` | `docker system prune -a` | Container / image / volume loss |
| `pg_dump --clean` + restore variations | (less common) | Data loss on restore |
| `chmod -R 000` / `chmod -R 777` over real paths | (less common) | Permission disaster |

The hook script's pattern list lives at `_system/skills/guard/scripts/check-careful.sh`. Add patterns there as new dangerous commands surface.

---

## Safe exceptions (allowed without warning)

These match `rm -rf <name>` where `<name>` is one of:

- `node_modules`
- `.next`
- `dist`
- `__pycache__`
- `.cache`
- `build`
- `.turbo`
- `coverage`
- `.venv`
- `venv`
- `target` (Rust build output)
- `out`
- `.parcel-cache`
- `.vite`

Universally rebuildable from `package.json` / `requirements.txt` / `Cargo.toml`. Warning on these would be friction with no payoff.

---

## Warning format

When a pattern matches, the hook returns a `permissionDecision: "ask"` with a structured warning:

```
DESTRUCTIVE COMMAND DETECTED

Command: <the full command the user is about to run>
Pattern: <which destructive pattern matched>
Risk: <one-line description of what could happen>

Proceed?
  [Y] Yes, I know what I'm doing
  [N] Cancel
```

The user's [Y] runs the command. [N] cancels. Either way, careful logs the decision to `~/.claude/guard-state/careful-log.jsonl` for audit (append-only, one line per warning).

---

## When to invoke

- Working on a production server or prod-connected dev env
- Debugging a live system where state matters
- Working in a shared repo where force-push would hurt teammates
- The user explicitly says "be careful" / "prod mode" / "I'm touching live data"
- Mid-session if the user realizes the work just became risky

When in doubt, invoke. The overhead is one extra confirmation per matched command; the downside of NOT having it is unrecoverable.

---

## What careful does NOT do

- **Does not catch destruction via non-Bash tools.** A Python script doing `os.system("rm -rf ...")` runs unmonitored — that's a `sed -i` / `python -c` / sub-shell escape. Careful is the bash-tool layer of defense, not exhaustive.
- **Does not catch destruction inside the workflow.** A migration script that drops a column won't be caught — careful only sees the top-level command the user is about to run.
- **Does not block — only asks.** Every warning is overridable. If the user needs a hard block, use `freeze` for files or stop the work.
- **Does not survive across sessions automatically.** The flag persists in `~/.claude/guard-state/careful-active.flag` but each session reads it on first invocation. To keep careful on across sessions, leave the flag in place.

---

## See also

- [../SKILL.md](../SKILL.md) — the guard mechanic
- [freeze.md](freeze.md) — directory edit boundary (paired safety wrench)
