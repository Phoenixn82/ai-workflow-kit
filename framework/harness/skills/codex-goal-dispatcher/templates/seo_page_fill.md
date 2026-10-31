---
name: seo_page_fill
triggers: ["fill all pages", "programmatic page generation", "seo page fill", "location pages build", "build out all the location pages"]
budget_default_minutes: 240
required_context_files: ["CLAUDE.md", "strategy/seo-plan.md", "strategy/content-clusters.md"]
verification_kind: machine
---

# Goal: build out programmatic page set — {{project_name}}

## Plan
Generate every page in the data source, deduped, schema-validated, sitemap-updated.

**Data source:** `{{data_source}}` (CSV / JSON / DB query — N rows = N pages)
**Template:** `{{page_template}}`
**Output dir:** `{{pages_output_dir}}`
**Page count target:** {{page_count}}

**What to do per iteration:**
1. Read next row from data source
2. Render template with row data
3. Write to `{{pages_output_dir}}/<slug>.{tsx|mdx|html}`
4. Validate: title present, h1 present, meta description present, schema.org JSON-LD valid
5. Update `sitemap.xml` to include new URL
6. Update internal link graph (every page links to at least 3 hub pages from `content-clusters.md`)
7. Repeat for all rows

## Verification
- [ ] Page count in `{{pages_output_dir}}` equals row count in `{{data_source}}` (allowing for dedup of slug collisions)
- [ ] Every page has unique `<title>` and `<meta name="description">`
- [ ] Every page has valid schema.org JSON-LD (validate with `npx schema-org-validator`)
- [ ] Every page passes lighthouse SEO ≥ 95
- [ ] `sitemap.xml` lists every generated URL
- [ ] Every page has ≥ 3 internal links to hub pages
- [ ] No two pages have byte-identical body content (template fill must use row data)
- [ ] `npm run build` succeeds on the full page set

## Budget
- `max_minutes`: {{budget_minutes|240}}
- `respawn_count_cap`: 2
- `on_budget_hit`: handoff.md lists last completed row + dedup conflicts

## Coordination with Claude (REQUIRED every turn)

A shared activity log lives at `{{project_dir}}/.claude-codex-log.md`. Claude appends to it automatically after every Edit/Write/Bash via a PostToolUse hook. You must append at end-of-turn so Claude sees what you did. Both sides stay aware via this single file — no extra polling needed.

**At the START of every turn**, tail recent Claude activity:
```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py" tail --project "{{project_dir}}" --lines 15
```
If Claude touched a file you were about to change, reconcile before continuing.

**At the END of every turn**, emit one summary line (<180 chars):
```bash
python "$HOME/.claude/skills/codex-goal-dispatcher/scripts/shared_log.py" append --project "{{project_dir}}" --actor codex --action turn --summary "<verb> <object> — <outcome>"
```
Example: `fixed a11y label in QuickCapture — axe clean`.

## Important
- Slug collisions: append `-2`, `-3` suffix; log to `goal-runs/{{ts}}/slug-collisions.md`
- If row data has missing required fields, skip + log to `goal-runs/{{ts}}/skipped-rows.md`
- Hold to `karpathy-guidelines` — no over-abstracted template machinery
- Re-running this goal should be idempotent: skip pages that already exist with matching content hash
