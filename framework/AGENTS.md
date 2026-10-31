# AI_Projects — Codex workspace loader

Codex reads this file only when its cwd is `$HOME/AI_Projects` itself (no git root here). It only orients; every rule lives in `~/.codex/AGENTS.md`, which Codex loads in every cwd (same facts as `~/.claude/CLAUDE.md`, the source of truth). Keep this file thin: rules go there, not here.

## Layout (relative paths in the rules resolve here)
- `_system/`: `lanes.json` (lanes and routes), `second-brain/` (the vault; schema in its `AGENTS.md`), `automations/` (REGISTRY.md, HALT), `skills/` (reference mechanics read as docs: `guard`, `second-brain`, `router`), `tool-parity/`.
- `_tools/` (CLIs; the `web` skill source is `_tools/web/skill/SKILL.md`), `pipeline/`, `projects/<p>/`, `_archive/`.
- `.claude/workflows/` holds the Workflow scripts (Claude-side).
- `SKILLS_INDEX.md` (the menu, regenerated from the filesystem), `DECISION_MAP.md` (routing): reference docs; open one only when choosing a tool.
- The older, longer manual is archived in the vault; it is history, not rules.
