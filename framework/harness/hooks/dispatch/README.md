# hooks/dispatch: one hook process per event (audit 2026-10-12, item G)

The dispatcher preserves existing hooks and adds the web-taint guard. `dispatch.sh <Event>` runs the hooks that `settings.json.hooks.original`
lists for that event, in the same order, each only when its own matcher applies, and prints one merged result:

- **PreToolUse**: the hooks are Python (webcred-guard.sh is a bash wrapper around `python3 -I -c "$PROG"`),
  so `run.py` runs them in **one** `python3 -I -S` interpreter via runpy (script dir on sys.path, own stdin/stdout/argv,
  SystemExit honoured, per-hook timeout via SIGALRM). If webcred-guard.sh ever grows other bash, run.py runs it the old way.
- **UserPromptSubmit / SessionStart** (only in `proposed-all`): each bash hook is sourced in a subshell (fork, no
  new bash) with `$0` and its args as before.
- Merge (code.claude.com/docs/en/hooks): one hook spoke -> its bytes pass through; 2+ -> deny > defer > ask > allow,
  deny reasons joined with `\n`, every additionalContext kept (joined with a blank line), plain stdout is context only on
  UserPromptSubmit/SessionStart, output of a hook that exits non-0/2, times out or prints bad JSON is dropped --
  except a **safety hook** (path contains secret-firewall, block-secrets, webcred-guard or odin-model-guard; `SAFETY`
  in run.py): then the call is denied with "safety hook <name> failed: <why>" (fail closed, astra round 1 #6).

| file | what |
|---|---|
| `dispatch.sh` | entry point; `--self-test` |
| `run.py` | PreToolUse runner (`PRE` table), `--merge EVENT`, `--self-test` |
| `settings.json.hooks.original` | per-hook reference chain, including route and web-taint guards |
| `settings.json.hooks.proposed` | **recommended**: only PreToolUse points at the dispatcher |
| `settings.json.hooks.proposed-all` | PreToolUse + UserPromptSubmit + SessionStart dispatched (measured: no gain, see below) |
| `tests/run.sh` | parity diff on 12 recorded inputs (both proposals) + 20-iteration cost bench; `--self-test` |

## What it saves (measure your own with tests/run.sh)

One interpreter for the before-hooks roughly halves the CPU time of each Bash and Read call and starts two
processes instead of four or five. Bash wall time stays about the same: the hooks used to start in parallel, now they
share one interpreter. UPS/SessionStart hooks spawn their own interpreters (jq, python heredocs, handoff module), which a
bash dispatcher cannot merge without editing them, so dispatching those events only adds a merge step.
**Recommendation: apply `settings.json.hooks.proposed` (PreToolUse only).**

## Cutover

The `injection-a-hooks` branch carries the final settings and guard registration.
Keep it in the worktree until whole-branch review. Merge web first, then the tools
branch, then this hooks branch; apply the staged Codex files afterward. Commands
retain their final `$HOME/.claude` paths. No live settings are edited during
implementation. The original per-hook snapshot is a reference chain for parity,
not a claim that the live pre-cutover settings already include the new guard.

At cutover, use a new scratch Claude session to read the hostile fixture. Check
that Bash, git status/push, and self-clear are denied; Read/Grep/Glob remain available.
The user clears the state with `web untaint` in their own terminal. Record rule ids only.

## Known differences (deliberate, `# ponytail:` in code)

1. Hooks run sequentially in one process, not in parallel. Outer timeout = sum of the hooks' timeouts (35 s); each hook
   keeps its own timeout in-process, but a hang inside C code (e.g. a regex) cannot be interrupted, so the outer kill
   would drop every PreToolUse decision for that call (before: only the hung hook's). A safety hook's own in-process
   timeout, crash, missing file or bad output (incl. a JSON object merge() cannot read) denies the call. The settings
   command is `bash "$HOME/.claude/hooks/dispatch/dispatch.sh" PreToolUse || { echo ... >&2; exit 2; }` (kit S12):
   a dispatcher that cannot start or run (missing, unreadable, run.py syntax error, crash) exits 2, which blocks
   the call; a home path with spaces or quotes still finds it. Only this outer kill still fails open.
   `tests/run.sh` checks the guard (launchguard) against the live settings.json command.
2. A sub-hook's non-blocking error (crash, exit 1, timeout) no longer shows its own "hook error" notice; its stderr
   still reaches the debug log through the dispatcher.
3. When 2+ hooks speak, their strings are joined into one, so the 10,000-char cap applies to the joined string
   (before: per hook). PreToolUse outputs are < 1 KB. For SessionStart (proposed-all) boot (<= 9.5 KB) + handoff
   brief (<= 3.6 KB) could cross it: one more reason to leave SessionStart alone.
4. Adding or removing a PreToolUse hook now means editing `PRE` in run.py as well as settings (keep the dispatcher wildcard matcher).
5. PreToolUse hooks must not write to fd 1 from child processes (run.py captures Python-level stdout only). None do.
   They must also stay stdlib-only and never call bare `exit()`/`quit()`: run.py is `/usr/bin/python3 -I -S` (no
   site-packages, no site builtins), so such a hook would crash, exit 1 and have its output dropped (fail open; a safety hook would deny every call).
6. No `if` filters / `onFailure` are introduced (none exist today); Codex `hooks.json`, PostToolUse (single hook) and
   plugin hooks (ponytail, superpowers) are untouched.

## Web-taint guard and D8

PreToolUse uses `*` so unknown tools and MCP tools always reach `web-taint/guard.py`.
A tainted session keeps exactly `Read`, `Grep`, `Glob`, `TodoWrite`, `AskUserQuestion`,
and `ToolSearch`. Every other tool gets this reason, with sorted rule ids:

`Session is read-only: it read text addressed to AI agents (rules <ids>). The user clears it with "web untaint" in their own terminal.`

There are no shell-command or MCP-name prefix allowances. A clean session can repair the
hooks or maintain exemptions subject to the existing guards and D8. The web-taint guard
is outside `SAFETY`: missing code, load errors, exceptions, nonzero exits, timeouts, and
malformed output trigger an independent dispatcher state check. State exists or cannot
be checked: deny. State absent: allow. Existing safety hooks keep their failure behavior
and their earlier deny reasons. Invalid session identities fail closed.

D8 applies even in clean sessions. Read/Grep/Glob paths, including symlinks, are resolved
against the event cwd and refused under `/var/lib/example-intake`. Bash mentioning that path
or switching to the service user is refused. Command words match by basename anywhere
in the normalized command, including absolute user-switch tools and wrapper paths.
Tokens that resolve to directories or contain `/projects/example-intake` are directory
arguments rather than commands. Every wrapper occurrence with arguments is validated:
public command names and plain arguments only; shell expansion, compound commands,
review, trips, confirm, release, and `--show`/`--cells`/`--confirm`/`--release` flags deny.
Bare wrapper-name mentions remain allowed. The wrapper remains responsible for its
closed response schema.

Variables, encoded ANSI-C strings, xargs, and eval-built strings can construct commands
that this limited text matcher misses. These accepted residual bypasses are covered by
the OS user boundary, not this hook. Plan B Task 2's real Unix EACCES matrix must prove
that boundary; D8 is defense in depth and is never OS-isolation evidence.

Guard logs contain only `ts`, `session`, `tool`, and `rule`. Unknown tool names are recorded
as `OTHER`. Explicit `--allow-private` requests are logged as `ALLOW_PRIVATE`; no command,
path, page, buyer text, or exception detail is logged. Log failure never removes a deny.

The outer dispatcher timeout is 45 seconds, covering the new guard plus existing hooks.
Legacy fixture parity and new guard expectations are checked separately by `tests/harness.py`.
`settings.json.hooks.proposed-all` remains the legacy performance comparison, not the
recommended registration. Tests set `WEB_PKG_DIR`, `WEB_TAINT_STATE_DIR`, and
`WEB_TAINT_EXEMPT_FILE` to worktree code and temporary stores.

`dispatch.sh` and `tests/run.sh` still hardcode live paths. Worktree verification uses a
temporary copy with those paths redirected to the worktree; no live file is edited.
The live scratch-session check waits for cutover. The Unix EACCES matrix explicitly skips
with `waits for Plan B Task 2 deployment`. Plan B must enable it with real deployed
Unix readers and a real MCP reader adapter; hook matching is never filesystem proof.
