---
name: asset_generation_batch
triggers: ["generate all assets", "batch image gen", "create all the sprites", "image gen pipeline"]
budget_default_minutes: 90
required_context_files: ["CLAUDE.md", "plan.md"]
verification_kind: machine
---

# Goal: generate full asset set — {{project_name}}

## Plan
Produce every asset listed in the brief. Each asset must be unique, transparent where required, named per the manifest.

**Asset manifest** (from `{{plan_file}}` or work order):
{{asset_manifest}}

**Provider:** image gen 2 (built into Codex), or Higgsfield CLI if specified in CLAUDE.md.

**What to do per asset:**
1. Read asset spec from manifest (name, size, style, alpha required?)
2. Generate via Codex's built-in image tool OR Higgsfield CLI
3. Save to `{{assets_dir}}/<name>.png`
4. Verify: file exists, dimensions match, alpha channel present if required
5. Update manifest with `generated: true`

## Verification
- [ ] Every asset in manifest exists at expected path
- [ ] Every asset matches required dimensions
- [ ] Every asset with `alpha_required: true` has alpha channel (use `identify` or PIL to check)
- [ ] No two assets are byte-identical (no copy-paste)
- [ ] Style consistency: brief states `{{style_directive}}` — visual audit confirms
- [ ] `manifest.json` updated with `generated_at` timestamp per asset

## Budget
- `max_minutes`: {{budget_minutes|90}}
- `respawn_count_cap`: 1 (image gen is deterministic given prompt; if it fails, the prompt needs human edit)

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
- If an asset fails generation 3 times, write the failure + prompt used to `goal-runs/{{ts}}/failed-assets.md` and skip
- Don't regenerate assets that already exist with correct dimensions — idempotent
- Style consistency matters more than absolute fidelity to brief
