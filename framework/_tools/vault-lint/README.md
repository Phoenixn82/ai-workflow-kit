# vault-lint

One stdlib script that enforces the vault schema (`_system/second-brain/AGENTS.md`). No LLM, no pip.

```
python3 $HOME/AI_Projects/_tools/vault-lint/vault_lint.py report [--json]
python3 $HOME/AI_Projects/_tools/vault-lint/vault_lint.py fill   --dry-run | --apply
python3 $HOME/AI_Projects/_tools/vault-lint/vault_lint.py rotate --dry-run | --apply [--days 30] [--what all|history|logs]
python3 $HOME/AI_Projects/_tools/vault-lint/vault_lint.py --self-test
```
`--vault PATH` (before the subcommand) points at another vault. `report` always exits 0; read `counts`.

## Rules (report)
Walks every file except dot paths (`.git/ .sync/ .obsidian/ .claude-codex-log.md .codex-session-changes/`).
- raw = `history.md`, `sessions/*`, `journal.ndjson`, rotated `_archive/history-*.md`, handoff-v2's `_archive/resume-overflow.md` and `_archive/resume-closed.md`: no frontmatter rules, size rule 300 KB.
- control = `AGENTS.md` (≤200 lines), `CLAUDE.md`, `MEMORY.md` (≤200 lines, ≤25 KB), `boot.md` (≤4,000 chars): budget only.
- `harness-memory/*.md`: needs `name`, `description`, `metadata.type`.
- everything else: frontmatter with `type` and `description`; ≤100 KB.
- dead `[[link]]`: basename (after `/`, before `|`/`#`) matches no file or folder name, case-insensitive; code spans ignored.
  Folder names count as found because the distiller writes project links as `[[<project>]]`.
  A harness memory's `name:` slug counts too: memories link each other as `[[<name>]]`, not by filename.
- `status: active` + `updated` older than 90 days; duplicate basenames outside the per-project standard set.

## fill
Wiki pages missing frontmatter, `type` or `description` get only the missing schema fields appended:
type from filename/folder, project from `Projects/<p>/` (skips `_archive/`, `_parked/` containers) or `_archive/projects/<p>/`, else `system`,
`updated` = last git commit date, description = existing `summary:` > `title:` > first heading > first line (≤120 chars, JSON-quoted).
Deviation from the work order: pages under `_archive/` get `status: done`, under `_parked/` `status: parked` (else `active`),
so the stale-active rule does not fire on archived pages. Existing values are never changed. Idempotent.

## rotate
For `history.md` > 300 KB: `## YYYY-MM-DD` entries older than `--days` (default 30) are appended to
`<project>/_archive/history-<oldest>-to-<newest>.md`; the header (everything before the first dated entry) stays.
Undated `## ` sections stay with the entry above them. `--apply` holds handoff-v2's own per-project flock
(`pipeline/handoff-v2/state/locks/`, opened read-only, same key as `store.project_lock`) for the read-rewrite,
because the distiller rewrites history.md whole under that lock; if a distill holds it for 60 s the file is skipped.

## rotate, logs (`--what logs`)
For `Projects/<p>/errors.md | lessons.md | decisions.md` > 100 KB: the oldest dated units (by date, file order on ties)
move to `Projects/<p>/_archive/<stem>-<YYYY>.md` until the live page is <= 70 KB; the newest unit always stays.
A unit is a `## YYYY-MM-DD` section plus its undated subsections, plus the standalone `<!-- job:<id> -->` line that
handoff-v2's `errors_block` writes above a job, plus that job's further same-date sections, so a job's replay marker
always moves with all of its sections. handoff-v2's `lessons_block` writes the marker above bare bullets with no
heading: each such block is one unit, dated by its first `_(YYYY-MM-DD, session <sid>)_` stamp (a block without
one takes the date of the unit before it; one before any date stays in the header and never moves, which is also
where an unstamped block left first on the live page by a rotation ends up). New archives get frontmatter (live
page's `type`, `project`, `status: done`, `updated`, `description`); an existing archive gets the units merged in
date order and `updated` bumped.
Text is only moved: the multiset of sections (whole pages, live + touched archives, cut at every dated heading and job
marker, each page's preamble left out, so neither a date-sorted merge that regroups units nor a block re-parsed into
the header reads as a loss) is checked equal before writing and again
after reading the files back; archives are written before the live page. A unit whose sections are all already in its
archive (matched section by section, so it does not matter how the archive's date-sorted merge regrouped them) is
not copied twice, so a run interrupted between the archive and live writes recovers cleanly on the next run
(`already_archived` in the record); a unit only partly there (a hand-edited archive) stops the file, nothing written.
Rotated lessons leave `handoff.cli lessons`, which lists live `lessons.md` only. Live frontmatter and preamble are kept.
Same handoff-v2 project flock as history. Idempotent (a rotated page is under 100 KB).
handoff-v2 `render._append_once` also looks in `_archive/<kind>-*.md` for the job marker, so `cmd_refile` replays
do not resurrect rotated sections (history or logs). `report` does not count `<stem>-<YYYY>.md` as duplicate basenames.

## Snippets for the orchestrator
One-time apply (after reviewing the dry-runs; commit before, so `git diff` shows exactly what changed):
```
cd $HOME/AI_Projects/_system/second-brain && git add -A && git commit -m "pre vault-lint fill"
python3 $HOME/AI_Projects/_tools/vault-lint/vault_lint.py fill --apply
python3 $HOME/AI_Projects/_tools/vault-lint/vault_lint.py rotate --apply --days 7
```
Pick `--days` so the page ends under the cap: a page whose entries are all recent may need `--days 7`.

Weekly-review job (Sundays), report into the weekly note and rotate raw files:
```
python3 $HOME/AI_Projects/_tools/vault-lint/vault_lint.py report --json > "$WEEKLY_DIR/vault-lint.json"
python3 $HOME/AI_Projects/_tools/vault-lint/vault_lint.py rotate --apply --days 7
```
JSON shape: `{vault, date, pages, counts: {rule: n}, offenders: {rule: [str, ...]}}` (all offenders, not just 20).

Candidates for the standard duplicate set (per-project files the work order did not list): `_README.md`,
`PLAN.md`, `PLAN-REVIEW-LOG.md`, `pre-refile-2026-09-28-RESUME.md`. Edit `STANDARD` in the script if wanted.
