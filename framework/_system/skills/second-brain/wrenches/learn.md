---
name: learn
description: Wrench inside the `second-brain` mechanic. Manage the body of stored learnings after they've accumulated. Review what's stored, search across the vault, prune stale entries, export for backup or migration. Replaces the old `learn` skill. Fires on "what have we learned", "show learnings", "prune stale learnings", "export learnings", "review what I know about X", "what patterns has the vault picked up".
---

# learn — manage the body of stored knowledge

The vault grows. This wrench keeps it healthy. Review what's accumulated, surface active patterns, prune stale entries, export for backup.

Use this when:
- You want a synthesis of what the vault has learned (per-project or vault-wide)
- An entry feels stale and you want to check if it's still load-bearing
- The vault is getting noisy and you want to prune
- You're migrating projects or backing up

---

## Mode 1: Review (synthesis)

Fires on: "what have we learned", "show learnings", "what patterns has the vault picked up", "what do I know about <topic>".

Walks the relevant vault subset and produces a synthesis — NOT a file dump. Format:

```markdown
## What the vault knows: <topic / project / vault-wide>

### Active preferences (Actions/)
- **scraping-tier** — all web work via the `web` skill: `web read` ladder for pages, `web act` + `web login` for logged-in work, `web handoff` for CAPTCHAs. Measured 2026-09-30. High confidence.
- **gemini-no-code-files** — Gemini reads, Codex writes. Stated 2026-05-27. High confidence.
- [...]

### Recurring decisions (Projects/*/decisions.md)
- one skill family consistently chosen for cutting across several audit reviews
- "agree all" pattern: the user prefers batch-approve when proposals are pre-voted
- [...]

### Error patterns (Projects/*/errors.md)
- PowerShell wrapper exit codes ≠ inner CLI exit codes (seen Nx, YYYY-MM-DD)
- [...]

### Token-expensive patterns (Projects/*/tokens.md)
- Reading full SKILL.md when offset+limit would suffice — observed Nx
- [...]

### Open loops (Projects/*/open-loops.md)
- guard mechanic not built (2026-05-27)
- Codex helper scripts for router not written (2026-05-27)
- [...]

### Active people (People/)
[only if requested]

### Live ideas (Ideas/)
[only if requested]
```

Scoped — if the user says "what have we learned about scraping", scope to scraping-related entries only. If "vault-wide", everything.

---

## Mode 2: Search

Fires on: "find learnings about X", "what does the vault say about Y", "search for Z".

Same as `recall.md` Mode 2 (find/search) but specifically scoped to learning-type entries: decisions, preferences, errors, token-notes, corrections.

```bash
# Filesystem path (or mcp-obsidian search)
Grep(pattern="<query>", path="<vault>", glob="**/{decisions,errors,tokens,corrections}*.md", -i=true)
Grep(pattern="<query>", path="<vault>/Actions", -i=true)
```

Return top hits with one-line excerpts + file paths. Offer to expand any.

---

## Mode 3: Prune stale

Fires on: "prune stale learnings", "clean up the vault", "vault health check".

Walks the vault looking for entries that have decayed:

**Staleness signals:**
- **Confidence: speculation** older than 90 days with no follow-up activity → candidate for removal or status update
- **Recency markers** on facts older than 90 days that the user is still acting on → flag for verification
- **Resolved open-loops** that were never marked closed → close them
- **Errors** that haven't recurred in 6 months → consider archiving to `_archive/` (still searchable, not in active set)
- **Token-notes** for approaches that no longer apply (e.g., a script got replaced) → archive
- **Corrections** older than 30 days whose underlying preference is now stable in `Actions/` → archive the correction (preference remains)
- **People** notes with no interaction recorded in 6+ months → flag as inactive (don't delete)
- **Ideas** with status `parked` older than 12 months → ask the user to graduate, cull, or keep parked

**Default action: NEVER delete without the user's approval.** Show the candidate list. The user decides per-item (or `prune all flagged`). The vault is precious — false negatives (keeping stale) are recoverable; false positives (deleting load-bearing entries) are not.

**Output format:**

```markdown
## Vault health pass — 2026-05-27

Scanned: <N> notes across <M> projects + Actions + People + Ideas
Total size: <X> KB

### Recommendations

**Resolve (10 items):**
- [list of open-loops that look closed but were never marked]

**Update recency (5 items):**
- An old plan and price in tokens.md, stale after the user changed plans. Update it to the current plan.

**Archive (3 items):**
- 2026-01 token-note about gstack-update-check timing — gstack is cut, no longer applies
- [2 more...]

**Verify (2 items):**
- "Vercel limits 100 deployments/day" (as of 2025-12) — older than 90 days, the user is about to ship; verify before relying on it.

**Cull candidates (1 item):**
- Person note for "John Smith" — no interaction in 18 months. Inactive flag or full removal?

**No action (124 items):** healthy.

Apply recommendations? (y / per-item / skip)
```

---

## Mode 4: Export

Fires on: "export learnings", "back up the vault", "give me a vault dump".

Two export formats:

### Format A: Single concatenated markdown

```bash
# Output: _archive/vault-export-<date>.md
# Actions + Projects(top-level) + Mechanics notes in a single file, with frontmatter intact
# (does NOT include index/log/_CLAUDE/dev-registry/Reference or sessions/ handoffs — not a full backup)
python <scripts>/vault-export.py --format=markdown --out=_archive/vault-export-$(date +%Y-%m-%d).md
```

Good for: backup, migration to another vault tool, reading offline.

### Format B: Flat JSON index

```bash
# Output: _archive/vault-export-<date>.json
# Each exported note as {path, frontmatter, body, links}. Machine-readable.
# (same scope as Format A: Actions + Projects(top-level) + Mechanics; not a full vault dump)
python <scripts>/vault-export.py --format=json --out=_archive/vault-export-$(date +%Y-%m-%d).json
```

Good for: feeding another agent, building a RAG index, programmatic processing.

**Export selector flags (the real argparse interface):**
- `--project <slug>` — only one project
- `--axis actions` — only `Actions/` preferences (other axes: `projects`, `mechanics`, `all` (default))
- `--since YYYY-MM-DD` — only notes dated on/after this date
- `--tag <tag>` — only notes carrying this frontmatter tag
- `--format markdown|json` and `--out <path>`

(`vault-export.py` shipped at `../scripts/` and was acceptance-tested 2026-05-28. Note its "entire vault" export covers Actions + Projects(top-level) + Mechanics only — it does not include index/log/_CLAUDE/dev-registry/Reference or `sessions/` handoffs.)

---

## Mode 5: Promotion (idea → project)

Fires on: "graduate this idea", "make this a project", "promote this".

When an entry in `Ideas/<slug>.md` reaches the threshold of "the user is going to actually build it":

1. Read `Ideas/<slug>.md`
2. Create `Projects/<slug>/_README.md` seeded from the idea's content (north star, what problem it solves, why it might work, why it might not)
3. Create empty `Projects/<slug>/NOTES.md`, `status.md`, `errors.md`, `tokens.md`, `open-loops.md`
4. Update `Ideas/<slug>.md` frontmatter: `status: promoted`, add link to new project
5. Add the new project to `index.md`
6. Append to `log.md`: "Promoted [[<slug>]] from idea to project"

If `plan-room` mechanic is invoked next, it picks up the new project's _README as starting context.

---

## What learn does NOT do

- **Does not auto-prune.** the user's approval is required for every removal. Period.
- **Does not write learnings.** Capture is `capture` wrench's job. End-session ritual is `end-session`. learn just manages what's already there.
- **Does not delete files outside the vault.** Only operates within `_system/second-brain/`.
- **Does not synthesize without scope.** Vault-wide synthesis on a large vault is noise. Always scope first.

---

## Helper scripts

The export mode (Mode 4 in the wrench body above) shells out to:

| Script | What it does |
|---|---|
| `../scripts/vault-export.py [--axis <name>] [--project <slug>] [--since YYYY-MM-DD] [--tag <tag>] [--format markdown\|json] [--out <path>]` | Concatenates filtered vault content as portable markdown or JSON with frontmatter parsed. Supports all 4 export selectors. |

Spec: `PHASE_5_DISPATCH.md` § 1.2 (not shipped). Acceptance-tested 2026-05-28.

---

## See also

- [../SKILL.md](../SKILL.md) — the second-brain mechanic
- [capture.md](capture.md) — what creates the entries this wrench manages
- [end-session.md](end-session.md) — the bulk write path
- [recall.md](recall.md) — read side (find/search/restore overlap with this wrench's Modes 1-2)
