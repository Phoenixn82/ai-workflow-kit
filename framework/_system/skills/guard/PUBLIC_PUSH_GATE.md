# Public Push Gate

The guard mechanic installs a global Git `pre-push` hook that blocks pushes to public GitHub repositories unless the public safety gate passes.

## What Is Gated

- Public GitHub push targets: gated fail-closed.
- Private GitHub repos: skipped, including missing repos that have not been created yet.
- Non-GitHub remotes: skipped.
- Private-to-public visibility flips: the guard `gh` wrapper (`bin/gh`, installed as `~/.local/bin/guard/gh` with that dir first on PATH) intercepts `gh repo edit ... --visibility public` and `gh api` calls whose args, `--input` file or stdin set `private=false` / `visibility=public`, runs `scripts/public_push_gate.py --mode publicize --repo <owner/repo>`, and only then calls the real `gh`. A failing gate refuses the call. Flag order does not matter (`gh repo --visibility public edit x/y` is caught), `-F key=@file`/`@-` values and JSON-escaped `--input` bodies are read, and a flip whose target cannot be identified (bare repo name, `repositories/<id>`, graphql) is refused outright. (`scripts/gh-publicize.ps1` is the legacy Windows equivalent.)

The gate is trigger-based. It runs only when a human pushes or asks the wrapper to flip visibility; it is not a background job.

## What It Checks

- Secret scanner: `betterleaks` (`~/.local/bin/betterleaks`, v1.9.x), falling back to `gitleaks` when betterleaks is missing; the gate prints which one ran. It scans the pushed range's full history (root commits and merge-introduced content included) and always runs hardened: `--config scripts/scanner.toml` (guard-owned; extends the default rules and adds a bare AWS key ID rule), `--ignore-gitleaks-allow`, an empty `--gitleaks-ignore-path`, and the repo's git dir as the scan source, so a committed `.gitleaks.toml`/`.betterleaks.toml`, `.gitleaksignore`/`.betterleaksignore` or `gitleaks:allow` comment cannot switch detection off.
- Personal-data denylist over every file path and every blob version added in the range (intermediate and later-deleted files included), the per-commit `git log -p` history, and commit metadata.
- Every blob version is also staged for the secret scanner's dir mode under a neutral `blob-<n>.txt` name (text as is, binaries as their printable strings), because both scanners' default rules skip names like `package-lock.json`, `*.svg` or `go.sum`; findings still name the original path. Archives (zip, including a zip behind a stub such as a self-extracting exe or a shell-launched jar, tar, gzip, bzip2, xz, and zstd on Python 3.14+) are opened by the gate to depth 8: every member's content is staged the same way and gets the denylist, and every member name and link target gets the denylist. An archive the gate cannot open (7z, rar, lz4, encrypted, corrupt), or one nested deeper, blocks.
- Every commit object in the range and every annotated tag object being pushed is staged whole the same way and gets the denylist: the scanners' git mode reads patches only, so a secret in a commit or tag message, or a tagger email, would otherwise reach no scan.
- What is already public: for a new remote branch the base is the tip the push URL (the hook's argv[2]) serves for the default branch right now, fetched from that URL. Never a local ref and never the remote name, whose fetch URL can be a different (private) repo when `remote.<name>.pushurl` is set. No verifiable base means the whole pushed ancestry is scanned.
- Fail closed: a blob git cannot read (shallow or partial clone) blocks the gate with a message instead of being skipped.
- Fail closed on the ref updates too: the hook keeps git's stdin in a private `mktemp -d` directory and blocks when it cannot capture it; the gate blocks a missing `--stdin-file` or a malformed ref record instead of reading it as nothing to scan. Empty and deletion-only pushes still pass.
- Publicize mode refuses unless a local remote points at the target repo, enumerates every advertised ref with `git ls-remote` (including custom namespaces and PR merge refs), fetches them into a unique temporary namespace, refuses a shallow clone, then scans `--all`. Direct blob/tree refs are scanned too. A ref that cannot be fetched or changes during the fetch blocks; temporary refs are removed on success and failure without updating local branches, tags or tracking refs.
- Publicize also paginates GitHub Actions runs and PRs, fetches any Actions head or closed-PR head/merge SHA not already reachable from the scanned refs, and includes it in the history scan. API errors, malformed responses, unavailable SHAs and incomplete Actions inventories block. There is no item cap; the existing command timeouts still fail closed.
- Issue/PR titles, bodies and conversation comments, PR review comments and review bodies, release names/notes, and repository description/homepage/topics are staged through the same denylist and secret scanner as commit metadata. Findings identify the issue/PR number, comment/review ID, release ID or repository field. Existing repo-scoped allowlist rules apply only to denylist findings. Publicize now requires API read access to these endpoints, including Actions; the human override still skips only Codex review. Pre-push mode is unchanged.
- This is a snapshot check, not an atomic GitHub visibility transaction. Unadvertised dangling objects absent from retained Actions runs and closed PRs cannot be exhaustively discovered. Actions logs/artifacts, release asset files, wiki repositories, discussions and metadata edit history are outside this scan.
- Scanner health: leaks use exit code 99; any other non-zero exit, an `ERR` log line (partial scan) or a missing/unparseable JSON report blocks the gate.
- Codex non-interactive review (read-only sandbox) for fuzzy PII or safety concerns that deterministic scans may miss. It gets every annotated tag being pushed (tagger, message) and every commit in the scanned range: author, committer, full message, and each file the commit changed (new files whole, modified files as a diff, binary and deleted files named only), so content added and deleted again before the push is still reviewed. The material goes out in batches of at most 200 KB, one Codex call each, and a FAIL on any batch blocks. The material sits between per-run random markers, so repository content cannot fake its end. Nothing is truncated: material needing more than 20 batches (`GUARD_REVIEW_MAX_BATCHES`) blocks before any call; review it yourself and use the override below.

Any deterministic secret or denylist hit is an automatic failure. Codex prose cannot override those findings.

## Allowlist (Repo-Scoped Exceptions)

`scripts/denylist.local.json` may include an optional top-level `allowlist` array for deliberately public contact info in specific repos. Each entry has a `regex`, optional `label`, optional `case_sensitive`, and optional `repos` list such as `["owner/repo"]`.

Allowlist entries suppress only personal-data findings emitted by the configured denylist. Phones are matched only through enumerated denylist entries, such as the user's own phone number, not a catch-all phone heuristic. Allowlist entries never suppress secret-scanner findings. Repo names are compared case-insensitively; omit `repos` only for an exception that is safe in every repo.

## Override

For a single deliberate push or publicize operation:

```bash
GUARD_ALLOW_PUBLIC_PUSH=1 git push origin main
```

This skips **only the Codex review** and prints a loud banner. The secret scanner and the denylist still run, and any hit still blocks. For a denylist false positive use the repo-scoped allowlist above; there is no escape for a secret-scanner hit (remove the secret from history instead). Do not set this variable globally.

## Disable

To disable the global hook path:

```bash
git config --global --unset core.hooksPath
```

To re-enable:

```bash
git config --global core.hooksPath "$HOME/AI_Projects/_system/skills/guard/githooks"
```

## Local Hook Chaining

Because `core.hooksPath` overrides each repo's `.git/hooks`, the global hook chains the repo-local `.git/hooks/pre-push` after the public gate passes. If the local hook exits non-zero, the push still fails.

## Test Hooks

The gate accepts test-only controls:

- `GUARD_FORCE_VISIBILITY=PUBLIC|PRIVATE|MISSING`
- `GUARD_FORCE_OWNER_REPO=owner/repo`

They are honoured only for non-GitHub remotes (local bare repositories); a real `github.com` remote is always checked live and the gate says it ignored them. Never push planted secrets or PII to a real GitHub repository.

Self-tests (nothing reaches GitHub):

```bash
python3 scripts/test_public_push_gate_allowlist.py
python3 scripts/test_public_push_gate_e2e.py   # add --skip-codex to skip the one real Codex review
```
