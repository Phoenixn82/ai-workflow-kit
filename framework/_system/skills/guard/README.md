# guard: public-push gate hardening (2026-10-12), snippets for the orchestrator

Changes live in `scripts/public_push_gate.py`, `scripts/scanner.toml`, `bin/gh`, `scripts/test_public_push_gate_e2e.py`
and the gate docs (`PUBLIC_PUSH_GATE.md`, `SKILL.md`). Already installed outside the repo:

- `~/.local/bin/betterleaks`: v1.9.0 linux x64 release binary. sha256 of the tarball
  `f8b185a39ffcece2a1ca82bf3a4e7435cd81963ffd16b7a9128daf75f35f6de7` matched both `checksums.txt` and the GitHub
  asset digest. Check the sigstore bundle too where cosign is installed. `/usr/bin/gitleaks` 8.30.1 stays as the fallback.
- `~/.local/bin/guard/gh`: symlink to `bin/gh`. It does nothing until that dir comes first on PATH (step 1).

## 1. PATH (~/.bashrc), needed for the gh flip gate

Add this line as the **last line** of `~/.bashrc`. Lines further up already prepend `~/.local/bin` and pnpm, so the
guard dir has to be prepended last:

```bash
PATH="$HOME/.local/bin/guard:${PATH//$HOME\/.local\/bin\/guard:/}"; export PATH  # guard gh wrapper: public-flip gate
```

Optionally add the same line just above `[[ $- != *i* ]] && return` so non-interactive bash also gets it. The line
removes any earlier copy, so having it twice is harmless. To check it worked, open a new shell, then:
`bash -ic 'command -v gh'` and `bash -lc 'command -v gh'` should both print `~/.local/bin/guard/gh`.

## 2. One-line correction for ~/.claude/CLAUDE.md (GitHub public-push safety gate section)

Replace:

> (1) a one-time typed **override** — set `GUARD_ALLOW_PUBLIC_PUSH=1` for a single `git push` / `gh-publicize.ps1` to bypass the whole gate (prints a loud banner; use ONLY when they KNOW a flag is wrong), then unset it;

with:

> (1) a one-time typed **override** — `GUARD_ALLOW_PUBLIC_PUSH=1` for a single `git push` / guarded `gh` flip skips ONLY the Codex review (loud banner); betterleaks + the denylist still run and any hit still blocks, so a denylist false positive goes through the allowlist below and a real secret must be removed from history; unset it afterwards;

Also in that section: replace "`gitleaks` (secrets)" with "`betterleaks` (secrets; gitleaks fallback)". The flip
wrapper is now `_system/skills/guard/bin/gh` (PATH-first `gh`), not `gh-publicize.ps1`.

## 3. Same drift in files outside guard (not edited here)

- `AGENTS.md` rule 14 still names `gitleaks` as the required scanner. Change it to betterleaks (gitleaks fallback).
- `DECISION_MAP.md:14` says "gitleaks + denylist + Codex review". Change it to betterleaks.
- Vault `Actions/public-github-safety.md:34` calls the override a "deliberate bypass". It now skips only the Codex step.

## 4. Commit note

Parallel commit `27a2b22` (audit batch 3) picked up an **intermediate** copy of these files. That copy still runs
`git log -p --text` over the whole history, which stalls on large binary repos. Commit the current
working tree of `skills/guard/` on top of it.

## 5. Verify

```bash
python3 _system/skills/guard/scripts/test_public_push_gate_allowlist.py
python3 _system/skills/guard/scripts/test_public_push_gate_e2e.py      # --skip-codex skips the real Codex review
```
