# AI_Projects — session loader

This file auto-loads for any session whose working directory is under `AI_Projects/`. It only orients; the rules live in `~/.claude/CLAUDE.md`. Keep it thin.

## Orientation

`AGENTS.md` (Codex twin of `~/.claude/CLAUDE.md`), `SKILLS_INDEX.md` (the menu), `DECISION_MAP.md` (routing) are reference docs — open the one you need, never all three. (A read-all-at-start instruction was almost never obeyed and is retired.)

## Tools

**Reference mechanics (`_system/skills/`, read as docs):** `guard` (gate), `second-brain` (end-session/recall), `router` (Codex/FreeLLMAPI wrench docs). Planning = superpowers `brainstorming` → `writing-plans` + `_system/second-brain/Reference/planning-lenses.md`; design = `/design`, `frontend-design`, `/impeccable critique|audit|polish` + `Reference/design-system-you.md`. Mechanics that never fired were archived; `SKILLS_INDEX.md` is regenerated from the filesystem.

**On-demand:** `web` skill (all scraping / browser / computer use), `cx` for Codex, Workflow scripts in `.claude/workflows/`, `open-loops` (Workflow; `_tools/open-loops/README.md`).

## Hard reminders (full rules in `~/.claude/CLAUDE.md`)

- Autonomy is opt-in: the user may stand up unlimited automations; the brake is the `HALT` kill-switch (or `budget.json` ceiling 0) — see `_system/automations/`. No per-day token cap (the 10M/day breaker was removed 2026-07-17). Skills still never self-schedule.
- Codex and Claude are both capable code lanes — pick whichever fits the task, no forced split (Codex is handy for high-volume or long unattended runs).
- Web work (scraping, browsers, logins, computer use): use the `web` skill — `web read` for pages (Scrapling ladder → zendriver; it never clicks a checkbox or solves a CAPTCHA: Cloudflare pages pass on browser fingerprint, and Scrapling's `--solve-cloudflare` flag is banned because that flag clicks the Turnstile box), native search then `web read`, `web gh`, `web act` (Chromium identity browser) + `web login` (Sign in with Google by default — the user's preference; a stored password only as fallback, never a password in chat) for logged-in work, `web handoff` for any CAPTCHA / checkbox / 2FA (the user taps it on their phone; the agent never solves or clicks one). Native apps: computer-use-linux first, hypruse backup (desk bake-off 2026-10-04). All on odin; scheduled scrapes on heimdall. Firecrawl decommissioned, Cloak retired. Source: `_tools/web/skill/SKILL.md`; summary: `_system/second-brain/Actions/scraping-tier.md`.
- No permission prompts by default. Never return `permissionDecision: "ask"` from a default-on hook.
- The vault (`_system/second-brain/`) is canonical. Session distillation is automatic (handoff-v2 at SessionEnd);
  `/handoff` is the optional deep pass on the user's command. Open loops: the `open-loops` Workflow, only when asked.
- Free/local model work goes through `lane` (admitted classes only); models and efforts per `_system/lanes.json`.

The user's global `~/.claude/CLAUDE.md` still takes precedence over everything here.
