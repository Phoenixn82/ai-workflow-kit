# MCP servers, plugins and helpers

Only what earns its place is installed. Every MCP server, plugin and listed skill costs context on **every** turn, and every subagent pays it again. The setup this kit came from measured about 75k tokens loaded before the first prompt, of which about 31k could be cut: 452 MCP tool names (most of them from one rarely used connector), 12 KB of server instructions, and 52 listed skills that never fired. Measure your own before and after each change: start a fresh session, look at the first turn's input tokens, change one thing, repeat.

## MCP servers

| Server | What it's for | Install |
|---|---|---|
| **computer-use-linux** | Drive native GTK/Qt/Electron apps through the accessibility tree (first choice for desktop apps) | `npm i -g @agent-sh/computer-use-linux` → `computer-use-linux mcp` |
| **desk-control** (in this kit) | Visible, stoppable screenshot-and-click loop for pixel-only surfaces (canvas, games), with a sticky Stop and an "eco mode" that hands steps to a small model | [`framework/_tools/desk-control/`](../framework/_tools/desk-control/) → `launch serve Claude` |
| **firefox-devtools** | Backup browser automation on a *separate* agent Firefox/Zen profile, never your personal one | `npm i @mozilla/firefox-devtools-mcp` (see `desk-control/firefox-mcp/`) |
| **chrome-devtools** | Debug and profile *your own* web apps (Lighthouse, console, network); not a scraping lane | plugin below, or `npx chrome-devtools-mcp@latest --isolated` |

Codex gets the same servers in `~/.codex/config.toml`, so both agents have the same tools; [`tool_parity.py`](../framework/_system/tool-parity/) checks that they match.

## Claude Code plugins

| Plugin | Why | Marketplace repo |
|---|---|---|
| **superpowers** | brainstorm → plan → TDD → subagent-driven build → verify before claiming done | `obra/superpowers-marketplace` |
| **chrome-devtools-mcp** | the DevTools server plus debugging skills (LCP, a11y, memory leaks, cookies) | `anthropics/claude-plugins-official` |
| **frontend-design** | design guidance for UI work | `anthropics/claude-plugins-official` |
| **impeccable** | `critique`, `audit` and `polish` passes for UI | `pbakaus/impeccable` |
| **ponytail** | forces the laziest working solution; a review skill for over-engineering | `DietrichGebert/ponytail` |

The exact list, with marketplaces, is `enabledPlugins` and `extraKnownMarketplaces` in [`settings.reference.json`](../framework/harness/settings.reference.json). A plugin you keep but rarely use can still be trimmed: `skillOverrides` sets a skill to `"off"` or `"name-only"` (listed by name without its description), which is how the reference config silences the plugin skills that never fired.

## Switching servers off, per project

- **For one project:** run `/mcp` in that project and disable the server. Claude Code stores it in `~/.claude.json` under `projects["<project path>"].disabledMcpServers`, so other projects keep it.
- **Everywhere:** `disabledMcpServers` in `~/.claude/settings.json` (the reference config lists the ones it turns off). This works for servers you configured, but it does **not** switch off claude.ai account connectors; those still load or show as "needs auth".
- **Account connectors:** disconnect the ones you don't use in your claude.ai settings (Settings → Connectors). Candidates to switch off unless you use them weekly: hosting and database connectors with hundreds of tools (Vercel, Supabase), meeting notes, calendar, scraping services, and `claude-in-chrome` if you use the `web` CLI. Mail connectors, if you keep them, are for **drafts**, never sending.

## CLIs and helpers

| Tool | What | Where |
|---|---|---|
| `web` | all web work | [web](web.md) |
| `route`, `cx` | which model and effort a job gets; Codex dispatch through those routes | [agents and lanes](agents-and-lanes.md), [`framework/harness/bin/`](../framework/harness/bin/) |
| `lane` | free/local model work with data tiers | [agents and lanes](agents-and-lanes.md) |
| `vault-recall` | search your vault from the shell | [memory](memory.md) |
| `thor-up` | wake the GPU box before using it | [fleet](fleet.md) |
| `tg` | one Telegram chat per alert topic (optional) | [`framework/_tools/telegram/`](../framework/_tools/telegram/) |
| `codex` | the second coding agent | Codex CLI |
| `gemini` | long-context and multimodal reads | Gemini CLI |
| `gh`, `betterleaks` / `gitleaks` | GitHub and secret scanning | package manager or release binary |
| `uv`, `mise` | Python tools and venvs; pinned CLI versions | astral.sh/uv, mise.jdx.dev |
| `herdr` | terminal multiplexer that shows each agent's state per pane | herdr |
| `motion` | motion graphics from code (HTML → MP4) with a self-critique loop | [`framework/_tools/motion/`](../framework/_tools/motion/) |
| `freellm-keydrop` | move an API key from clipboard into the router without printing it | [`framework/harness/bin/`](../framework/harness/bin/) |

`install/install.sh` installs these per profile; `install/tools.json` records each one's homepage and license.

## Output style

[`framework/harness/output-styles/signal.md`](../framework/harness/output-styles/signal.md) puts the result first and skips narration. Drop it in `~/.claude/output-styles/`.

## Harness wiring

[`framework/harness/settings.reference.json`](../framework/harness/settings.reference.json) shows the exact hook wiring, plugins, overrides and env. Use it as a reference; don't copy it blind. `install/install.sh --harness` merges the parts you pick into your own settings, after a backup.
