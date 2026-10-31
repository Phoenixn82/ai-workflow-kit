# DECISION_MAP — task → tool (rewritten 2026-10-12)

| Task | Use | Notes |
|---|---|---|
| Read a page, find pages, GitHub intel | `web read <url>`, native WebSearch then `web read`, `web gh` | the `web` skill; never claude-in-chrome, raw curl/Playwright/Scrapling, Firecrawl |
| Logged-in browser work | `web act` + `web login` (Sign in with Google) | CAPTCHA/2FA → `web handoff` (the user taps on their phone) |
| Native desktop app | computer-use-linux MCP, then desk-control | pixel loop only for canvas/games |
| Debug the user's own web app | chrome-devtools MCP (`evaluate_script`, screenshots) | prove a CSS fix live before editing source |
| Plan a feature | superpowers 7.0 `brainstorming` → `writing-plans`; lens checklists in vault `Reference/planning-lenses.md` | Astra review via `cx plan read-only <out> <prompt>` |
| Pick a model for any dispatch | `route list`, then `route <class>` (Agent alias, Workflow `agent()` {model, effort}, `cx` or `lane` command) | no default lane; the table lives in `_system/lanes.json` routes |
| Dispatch Codex | `~/.local/bin/cx <class|plan|check|build> <sandbox> <outfile> <prompt>` | models from lanes.json via `route --cx`; a non-Codex class exits 2; long unattended builds → `cx build` (long-unattended-build route) |
| Fan out agents | Workflow tool (ultracode) inside `agents.slice`; Agent tool for one-offs | model and effort per agent from `route <class>`; 20–50 agents are fine on power-saver |
| UI / visual design | `/design` (artboards), `frontend-design` plugin for anti-AI-look guidance, `/impeccable critique|audit|polish` for review | the user's own system: vault `Reference/design-system-you.md`; decks = Slides artifact type |
| SEO | `seo-*` agents (plugin) | the old `seo` mechanic is archived |
| Public push / make a repo public | automatic: guard pre-push gate + `gh` flip wrapper (betterleaks + history-wide denylist + Codex review) | `GUARD_ALLOW_PUBLIC_PUSH=1` skips only the Codex step |
| Session memory | automatic: harness-memory (vault) + handoff-v2 distillation at SessionEnd | `/handoff` = optional deep pass |
| Find a note in the vault | `vault-recall "<question>"` (hybrid search; the prompt hook injects the top 3 hits automatically) | `rg` for exact strings; never read the whole index |
| Needs thor (Ollama, FreeLLMAPI, backups) | run `thor-up` first | thor suspends after 60 min idle |
| Heat / fan-out safety | `host-heat` (1 min timer), power-saver profile stays on | measure with cgroup `cpu.stat` |
| Backups | restic-odin nightly 03:10 → thor `/srv/work/backups/odin` | restore drill: `restic restore latest --include <path>` |
| Model/lane choices | `_system/lanes.json`, kept current by `lane-intel` (daily 05:40: Theo uploads, their X posts, slopalytics) | changes logged in `Reference/lane-intel/changes.md` |
| Weekly workflow review | `weekly-review` (Sundays 03:17) | report → `Projects/workflow/weekly/` + Review Queue; only safe-class fixes auto-applied |
