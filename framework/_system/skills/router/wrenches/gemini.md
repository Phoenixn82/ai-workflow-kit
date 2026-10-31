---
name: gemini
description: Wrench inside the `router` mechanic. Delegates to Google Gemini for READ + ANALYSIS tasks Claude is the wrong tool for — 1M-token context window, native multimodal (one CLI call sees frames AND hears audio AND reads embedded text), grounded research with citations. Gemini's strengths are long-context reads, multimodal, grounded research, and scoping; it can also author code when it fits — no code restriction. For big multi-file work it often reads the whole codebase and outputs a per-file spec that any lane implements. Fires on "analyse this video", "huge PDF", "200 pages", "1M tokens", "deep research", "@gemini", or auto when the router detects multimodal source > 3 MB or single-task context > 300K tokens.
---

# gemini — Gemini READ + ANALYSIS wrench

Hand off to Google Gemini when the task is too big or too visual for Claude. Gemini is the **long-context, multimodal, grounded-research** lane — strongest on long-context reads, multimodal, and scoping, and it authors code too when that's the best fit.

## Auth on Linux

Gemini CLI has two free paths, and only one works headless:
- `oauth-personal` ("Gemini Code Assist for individuals") no longer serves this client. Headless calls fail with `IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals ...`, whatever the model. A Gemini Advanced chat subscription does not grant CLI or API access either.
- **`gemini-api-key` (Google AI Studio)** works on the free tier. Put the key in `~/.gemini/.env` as `GEMINI_API_KEY=...` (mode 0600; the Google key FreeLLMAPI's `google` provider uses works too, copied machine to machine, never through chat) and set `~/.gemini/settings.json` to `"security": {"auth": {"selectedType": "gemini-api-key"}}`. Check: `gemini -p "reply with exactly one word: OK"`.
- On thor (image work): install the CLI globally with the same `.env` and settings. Headless image analysis: `GEMINI_CLI_TRUST_WORKSPACE=true gemini -p "Look at @image.jpg ..."`.
- **Pro models have no free-tier quota:** `gemini -m pro` answers `429 ... limit: 0`. Enabling billing on the AI Studio project fixes that and is a spending decision; the Flash default needs no billing.

---

## Gemini's strengths (code included)

Gemini shines on long-context reads, multimodal, grounded research, and scoping large codebases — and it can author code when that's the best fit. There is no Gemini code restriction.

**A handy (optional) multi-file pattern when Gemini's 1M-token window is the reason to involve it:**
1. **Gemini reads** the whole codebase
2. **Gemini outputs a written spec** — numbered list of files + the exact change needed per file as prose
3. **Claude reviews** the spec
4. **Any lane writes** per-file from the spec (Codex is often handy for the mechanical per-file passes)

Use this because it helps on huge repos, not because Gemini is barred from writing.

---

## Use cases (READ + ANALYSIS only)

| Task | Why Gemini |
|---|---|
| Analyse a video / audio file / PDF | Native multimodal — one call sees frames AND hears audio AND reads embedded text |
| Read a 200-page document or 50,000-line log | 1M-token context window — fits in one shot |
| Audit a 200K-token codebase for a specific code smell | Claude would need 20 Read cycles; Gemini surfaces it in one (output: written report, NOT a diff) |
| Identify all callers of a function across the codebase | Outputs the list + per-caller change spec; Codex implements per-file |
| Plan a multi-file refactor | Outputs the file list + the per-file spec; Codex executes per-file |
| Deep research dossier with citations | Gemini Deep Research (via gemini.google.com) returns grounded multi-source synthesis |
| Bulk image analysis (alt text, OCR, scene description) | Multimodal at scale |
| Summarise a 200-page PDF with citations to page numbers | Long-context + grounded |

The router auto-fires Gemini when:
- A `.mp4 .mov .mkv .webm .mp3 .wav .m4a .pdf` over 3 MB is involved
- A single-task context estimate exceeds 300K tokens
- Multi-file code work needs to be SCOPED (Gemini outputs the spec; Codex implements)

---

## Two invocation paths

### Path A: CLI (default)

```bash
# Analyse a video — native multimodal: visual + transcript + embedded text in ONE call
gemini --skip-trust -p "Analyse the video at <PATH>. Return: (1) scene-by-scene visual notes with timestamps, (2) full transcript, (3) any on-screen text or captions you can read. Combine all three into one timestamped report."

# Hand off a giant PDF / log
gemini --skip-trust -p "Summarise this 200-page document at <PATH>: <what you want extracted>. Cite page numbers for every claim."

# Multi-file code SCOPING (NOT writing) — Gemini reads + identifies, Codex writes
gemini --skip-trust -p "Read every file under src/ and identify where X should change to Y. Output a numbered list: filename, line range, and the change as PROSE. DO NOT output code or a diff — this is a planning spec for Codex to implement per-file."
```

`--skip-trust` is one way to bypass the trusted-folder prompt for a single call; on odin, `$HOME/AI_Projects` is already registered in `~/.gemini/trustedFolders.json` as `TRUST_FOLDER`, so calls from inside it don't need the flag. `GEMINI_CLI_TRUST_WORKSPACE=true` remains the env-var alternative for other directories (e.g. CI-style headless runs elsewhere).

---

## Video + audio: native multimodal in one call

Google Gemini's multimodal is native — **one CLI call ingests visual frames, audio transcript, and any on-screen text simultaneously.** No separate steps for "first get transcript, then analyse frames" — Gemini does it all in one pass.

```bash
gemini -p "Analyse the video at <PATH>. Return:
  1. Scene-by-scene visual notes (what's on screen, timestamps)
  2. Full audio transcript (timestamped, speaker-tagged if possible)
  3. Any on-screen text, captions, or UI elements you can read
  4. A 3-sentence executive summary

Combine all four into one timestamped report."
```

This makes Gemini the right tool for: video summarization, podcast transcription with visual context, screen-recording analysis (UI + narration together), tutorial parsing (steps + spoken explanation), interview transcription with body-language notes.

For raw text-only transcription where visual doesn't matter, the `video-scan` mechanic (yt-dlp + native YouTube captions + Whisper fallback) is cheaper. Use Gemini when visual + audio together matter.

### Path B: MCP `@gemini` syntax

If `~/.claude/mcp.json` has the gemini MCP server wired, Claude can delegate mid-session by addressing Gemini directly:

```
@gemini Review this codebase for performance issues
@gemini Summarize this 50,000 line log file
@gemini Analyze these files and suggest refactors
```

The MCP config:

```json
{
  "mcpServers": {
    "gemini": {
      "command": "npx",
      "args": ["-y", "https://github.com/RLabs-Inc/gemini-mcp.git"],
      "env": {
        "GEMINI_API_KEY": "${GEMINI_API_KEY}"
      }
    }
  }
}
```

Restart Claude Code after wiring. Verify with `claude mcp list`. This path already needs `GEMINI_API_KEY` (an AI Studio key, same one as above) — it was never on the OAuth path.

---

## Model policy (pinned 2026-09-04, `~/.gemini/settings.json`)

| Alias | Model ID | thinkingLevel | When |
|---|---|---|---|
| default (`model.name`) | `gemini-3.8-flash` | HIGH | Every headless/interactive call unless overridden — the low-tier + research lane's workhorse |
| `pro` (`gemini -m pro`) | `gemini-3.1-pro-preview` | HIGH (built into the CLI's `chat-base-3` default) | Deep research, anything `gemini-3.8-flash` gets wrong on retry |

`gemini-3.8-flash` is not yet in this CLI version's (0.58.0) built-in `modelConfigs.modelDefinitions`/`aliases` registry (its own bundled docs top out at `gemini-3.5-flash`), so it is wired as a `modelConfigs.customAliases` entry extending the built-in `chat-base-3` preset (temperature 1, thinkingLevel HIGH) rather than a raw unaliased model string — that is what actually turns thinkingLevel HIGH on for it. `pro` is a thin alias over the CLI's already-built-in `gemini-3.1-pro-preview` (which already defaults to `chat-base-3`, so no override was needed).

**Upgrade rule:** whenever a newer Gemini model ships, re-pin to it at the highest available effort. Run `~/.local/bin/gemini-upgrade-check` (manual, not scheduled) to see whether a newer CLI/model roster exists, then update `model.name` and the two `customAliases` entries above and re-verify with `gemini -p` / `gemini -m pro -p`.

---

## Deep Research sub-path

Deep Research returns grounded multi-source dossiers with inline citations. It's the right tool for "research X and tell me what you find with sources" — but it's **only exposed via gemini.google.com** (the web app, which needs a Gemini web subscription — this part is unaffected by the CLI/Code-Assist deprecation above), not the CLI.

Drive it via `web act` — the identity browser on odin, signed into the user's Google account (the act lane from the `web` skill; chrome-devtools-mcp doesn't keep a login, so it's no longer used here):

```
1. web act open https://gemini.google.com/app
2. web act snapshot -i            → find the "Deep Research" toggle, prompt box, submit refs
3. web act click @<toggle-ref>
4. web act fill @<prompt-ref> "<research question>"
5. web act click @<submit-ref>
6. web act wait … / web act snapshot   until the report renders
7. web act get text body          → return the dossier
8. web act close
```

If Google asks to re-verify, run `web handoff "<reason>"` — the user taps it on their phone; never type a password. **Use the browser path sparingly** — every interaction costs Claude tokens. For most research, the CLI path is enough (once the auth fix above is live).

**Fires on:** "deep research", "research dossier", "grounded synthesis", "multi-source synthesis", "research X with citations".

---

## Output handling

Gemini returns prose. For long-context tasks the output can itself be long. Two patterns:

**Pattern 1: Output to file.** If Gemini's output is > 5KB (full transcripts, long analyses, multi-file change specs), pipe to disk:

```bash
gemini -p "<spec>" > /tmp/gemini-output.txt
```

Then Claude reads the file, extracts what's needed, and acts.

**Pattern 2: Inline (small output).** For one-paragraph summaries, status checks, simple verdicts — let it come back inline.

**By default, prefer a prose SPEC from Gemini over code or a diff.** For most code work Gemini's strongest output is the spec (prose: "in `src/auth.ts` line 47, change the token check from X to Y because Z"); Claude reads the spec, and Codex executes the per-file write. This is a task-fit default, not a prohibition — ask Gemini for code directly when that's genuinely the best fit.

---

## Cost discipline

`gemini-3.8-flash` / `gemini-3.1-pro-preview` via a Google AI Studio `GEMINI_API_KEY` run on that product's free tier — rate-limited, not billed, not the same thing as the (CLI-unusable) Gemini Advanced consumer subscription. If AI Studio free-tier limits are ever hit, the fallback is the multi-tier chain below (Codex, then Claude) — do not silently switch to a paid/metered key without saying so.

Per the router's cost ordering: **freellm → Gemini → Codex → Claude.** Use Gemini before Codex/Claude on long-context READ or multimodal work, once the auth fix is live.

---

## Lane fallback

When Gemini fails (auth dead, quota, timeout), the router falls through to the `research-synthesis` route (lanes.json; images in a headless pipeline go to `bulk-vision` instead):

```bash
if ! gemini -p "$PROMPT" < "$INPUT" > "$OUTPUT"; then
  echo "[router] Gemini failed; falling back to the research-synthesis route"
  read -r M E < <(route --json research-synthesis | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["model"], d["effort"])')
  { printf '%s\n\n' "$PROMPT"; cat "$INPUT"; } | claude -p --model "$M" --effort "$E" > "$OUTPUT"
fi
```

This fallback chain is the router mechanic's job to orchestrate, not this wrench's. The default `gemini-3.8-flash` lane runs on the free tier; the `pro` alias needs billing (see Auth above), so fall through to the research-synthesis route for anything that specifically needed `pro`, not for default-lane calls.

---

## See also

- [../SKILL.md](../SKILL.md) — the router mechanic
- [codex.md](codex.md) — escalation lane when Gemini fails
- [freellmapi.md](freellmapi.md) — the cheap bulk lane (when Gemini Flash is overkill, and the provider that likely already has a reusable Google API key)
