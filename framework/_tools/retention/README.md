# retention (audit 2026-10-12 H4 + the H5 Claude pin)

## retain.py — archive-first retention

Moves, never deletes, into `~/.local/share/archive/<YYYY-MM-DD>/<original absolute path>`; each run
appends `class, source, dest, size, mtime, sha256` to `<date>/manifest.jsonl` *before* each move.

| class | what | age gate |
|---|---|---|
| `bak` | `*.bak*` files under `~/.claude ~/.codex ~/AI_Projects/_tools ~/AI_Projects/_system` | idle > 14 d |
| `codex-img` | entries of `~/.codex/generated_images` | idle > 21 d |
| `codex-arch` | entries of `~/.codex/archived_sessions` | idle > 21 d |
| `pre-merge` | `~/.claude/projects/*/memory.pre-merge-2026-10-12` dirs | none (per PLAN) |

- "Idle" = newest mtime **or ctime** over the entry's whole tree: `cp -p` backups keep the source's old
  mtime, so a backup made today is never swept the same day.
- Skipped: anything tracked by an enclosing git repo (`git ls-files` of every repo above it), and `.git` /
  `node_modules` trees (vendored files).
- A run moving > 1 GiB stops (exit 3) until rerun with `--over-1gb-ok` after the user says yes.
- Cross-device moves copy, verify the sha256, and only then remove the source.

```bash
retain.py --dry-run                       # plan + per-class totals (also version check + repatch, dry)
retain.py                                 # archive, record the version check, re-apply plugin patches
retain.py restore <manifest> [path]       # move back everything, one path, or everything under a dir;
                                          # never overwrites, verifies sha256 before and after; --dry-run
retain.py version-check [--dry-run]
retain.py --self-test                     # temp tree: move (incl. EXDEV path) + restore byte-identical
```

Timer: `units/retain.{service,timer}`, Sundays 05:30 Etc/UTC, `agents.slice`, nice 19, idle IO.
`./install.sh --dry-run` shows the install; `./install.sh` installs without enabling; `--enable` enables.
Not enabled as of 2026-10-12.

## Claude CLI pin (no auto-update)

Before: Omarchy's `~/.local/bin/claude` did `export MISE_MINIMUM_RELEASE_AGE=0; mise use -g --quiet claude`
on every launch. A bare `mise use -g claude` means `@latest`: it rewrote any pin in
`~/.config/mise/config.toml` back to `"latest"` (proven on a temp config) and installed new releases
on first launch, with no N-1 left to roll back to.

Now (2026-10-12, H4):
- `~/.config/mise/config.toml`: `claude = "2.1.296"` (via `mise use -g claude@2.1.296`).
- `~/.local/bin/claude`: only `exec mise x "claude" -- "claude" "$@"`, so the config line is the one
  source of truth; no per-launch config write, no network lookup, no leaked `MISE_MINIMUM_RELEASE_AGE=0`.
  Originals: `config.toml.bak-20261012-H4`, `claude.bak-20261012-H4`.
- Omarchy can regenerate the wrapper (`omarchy-mise-install claude` in a future migration). The weekly
  version check records `wrapper_pinned: false` when a live `mise use` line is back; re-apply by restoring
  the 1-line body above.

Weekly check (part of `retain.py`, never installs): pinned value, `mise which claude` → `--version`, and
`npm view @anthropic-ai/claude-code version`, appended to `~/.local/state/retention/versions.jsonl`
with `update_available`. The wrapper itself is not executed by the check.

Upgrade on demand (keeps the old version installed = N-1):

```bash
mise install claude@X.Y.Z            # prefix MISE_MINIMUM_RELEASE_AGE=0 for a same-day release
mise use -g claude@X.Y.Z             # flips the pin; the old dir stays in ~/.local/share/mise/installs/claude
claude --version
claude -p --output-format json 'Reply with exactly: ok'   # result "ok", modelUsage has claude-opus-5-5
python3 $HOME/AI_Projects/_tools/retention/repatch.py && chrome-devtools-repatch
```

Upgrades: keep the previous version as N-1; mise's release-age gate holds back releases that are only hours old.
Codex: `mise install codex@X.Y.Z` moves `installs/codex/latest` and keeps the old install. With
`upgrade.auto_prune = true`, never `mise upgrade codex` while a codex run is live: it deletes the old install dir
those runs exec from. A native-install host uses `claude install X.Y.Z`; a thin client can float on
Omarchy's stock wrappers (`MISE_MINIMUM_RELEASE_AGE=0 mise upgrade claude codex`, auto_prune off). Jail checks after:
another app `tests/jail_probe.py codex|claude`, a real `run_agent` per CLI, `triage.py --self-test`, a scratch `--dry-run`.

Rollback: `mise use -g claude@<previous>`. Prune only N-2 and older: `mise uninstall claude@<old>`.
Not done here (rest of H5): pinning codex (its shim execs `installs/codex/latest` directly, so it does not
float today), the other Omarchy wrappers (gh, gemini, opencode, …) that still float, a tracked upgrade
script with the smoke test, a second installed Claude version (none exists yet).

## Plugin-cache patches

See `PATCHES.md` (Ponytail SubagentStart removal, chrome-devtools `--executablePath`), `repatch.py`.
