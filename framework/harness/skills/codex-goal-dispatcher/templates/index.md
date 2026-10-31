# Template index

Trigger phrase → template file. Match is case-insensitive, substring-aware. First match wins. If no match, use `generic.md`.

| Trigger phrases | Template |
|---|---|
| ui audit, ui sizing audit, playwright ui check, audit the ui, ui pass with playwright, sizing-first audit, visual quality audit | `ui_audit.md` |
| comprehensive user testing, qa the whole app, test this end to end, full qa pass | `comprehensive_user_testing.md` |
| lift coverage, test coverage to, get tests passing, coverage to N% | `test_coverage_lift.md` |
| accessibility audit, a11y pass, fix all the a11y, pa11y clean | `accessibility_audit.md` |
| generate all assets, batch image gen, create all the sprites, image gen pipeline | `asset_generation_batch.md` |
| fill all pages, programmatic page generation, seo page fill, location pages build | `seo_page_fill.md` |
| bug bash, fix every bug, grind on bugs, zero failures | `bug_bash.md` |
| always-on watcher, continuous frontend testing, watch the frontend, keep testing this | `always_on_watcher.md` |
| phase gate, lock in phase, ratchet phase criteria | `phase_gate.md` |
| use codex goal, set this up as a goal, /goal this, codex goal dispatch | `generic.md` |

When a template is loaded:
1. Read its frontmatter for `budget_default_minutes` + `required_context_files`
2. Fail fast if required context files don't exist in the project dir
3. Substitute `{{ placeholders }}` from project state (CLAUDE.md, plan.md, scanned repo)
4. Write the filled prompt to `<project>/pipeline/goal-runs/<ts>/goal_prompt.md`
