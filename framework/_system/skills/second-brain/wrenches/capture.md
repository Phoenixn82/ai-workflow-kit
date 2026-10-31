---
name: capture
description: Wrench inside the `second-brain` mechanic. Mid-session quick writes to the vault. Lightweight — no ceremony, no end-of-session ritual. AI-first rule still applies (preamble + frontmatter + wikilinks). Fires on "save this", "remember this", "log this", "note this", "capture this decision / person / idea", "log this error", "this approach burned too many tokens".
---

# capture — mid-session writes

When something worth remembering surfaces during work, write it to the vault without breaking flow. No ceremony, no draft-and-approve cycle (that's the end-session ritual's job). Just write it, keep going.

The mechanic's AI-first rule applies on every capture: preamble + frontmatter + wikilinks + recency markers. See `../SKILL.md` § AI-first rule. When a capture has frontmatter, set `importance` (1-10, default 5) and `last-touched` (today's YYYY-MM-DD).

---

## Capture categories

Each category writes to a specific location in the vault. Use the category that fits the content.

| Category | Vault location | When |
|---|---|---|
| **decision** | `Projects/<slug>/NOTES.md` (appends) | the user made a choice with reasoning ("ship as one PR not two", "use `web read`, not a model-driven browser") |
| **preference** | `Actions/<slug>.md` (creates or appends) | the user corrected my behaviour OR confirmed a non-obvious approach — anything that should now be a standing default |
| **error** | `Projects/<slug>/NOTES.md` (appends) | Hit a bug and rooted-caused it (must have what / cause / fix / prevention) |
| **token-note** | `Projects/<slug>/NOTES.md` (appends) | An approach was token-expensive and a cheaper path exists |
| **person** | `People/<name>.md` (creates or appends) | A new person mentioned with context worth keeping (client, collaborator, vendor) |
| **idea** | `Ideas/<slug>.md` (creates) | A project idea or feature idea the user isn't building yet but wants saved |
| **status** | `Projects/<slug>/RESUME.md` (automatic — read only) (overwrites) | A project's state changed materially (blocked → unblocked, done → in flight) |
| **open-loop** | `Projects/<slug>/NOTES.md` (appends) | Something is waiting on the user, on a third party, or on an upstream event |
| **mechanic-state** | `Mechanics/<mechanic>/state.md` (appends or updates a section) | A mechanic gained a wrench, lost a wrench, surfaced a known gap, or had its trigger phrases tuned. Read by future-Claude during skill-intake Step 0 |

If the user says "save this" without specifying a category, infer from context. If genuinely ambiguous, ask in one line ("decision or preference?"). Don't ceremony it.

---

## Format per category

Every capture has the AI-first frontmatter + "For future Claude" preamble + body.

### decision

```markdown
## 2026-10-01 — Route web work through the `web` skill

**For future Claude:** Use `web read` for pages (it climbs Scrapling → stealth → zendriver itself, with no checkbox clicking), `web act` + `web login` for logged-in work, and `web handoff` for CAPTCHAs.

**Decision:** [decided what]
**Reasoning:** [why this over alternatives]
**Alternatives considered:** [...]
**Confidence:** stated

**Links:** [[scraping-tier]] [[example-project]] [[router]]
```

### preference (writes to `Actions/`)

```markdown
---
type: preference
date: 2026-05-27
tags: [scraping, routing, cost-discipline]
ai-first: true
confidence: stated
---

# Scraping tier order — the user's default

**For future Claude:** For any web scraping task, the default tool order is by cost, not by capability flex. The user established this on 2026-05-27 and it overrides any default that says "use Chrome extension for bot detection".

[Full preference body — same shape as the feedback memory format in CLAUDE.md auto-memory: rule + why + how-to-apply]

**Links:** [[router]] [[web-scrape]] [[scraping-tier]]
```

### error

Must have all four: what / cause / fix / prevention. If any field is unknown, capture is rejected — go root-cause first.

```markdown
## 2026-05-27 — Snapshot tar exit code 255 despite success

**What:** PowerShell tar command for snapshot returned exit code 255 even though the archive completed (manifest verified).
**Cause:** PowerShell `Pop-Location` after the `try/finally` block emitted non-zero. Tar itself returned 0.
**Fix:** Check the log file's "Tar exit code:" line and archive file size, not the wrapper exit code.
**Prevention:** When wrapping CLI calls in PowerShell with cleanup, always log the inner command's exit code separately. Don't trust the wrapper's exit as source of truth.

**Confidence:** high (observed this session)
**Links:** [[example-project]] [[powershell-pitfalls]]
```

### token-note

```markdown
## 2026-05-27 — Skip gstack preamble when reading source skills

**Expensive:** Read an entire long `<skill>/SKILL.md` to extract one section — burned thousands of tokens of preamble.
**Cheaper:** Read with `offset`/`limit` to skip past the preamble.
**When to apply:** Any gstack-family skill (many open with a long bash preamble).
**Token saved per skill read:** ~20-30K.

**Confidence:** high
**Links:** [[example-project]] [[gstack-skills]]
```

### person

```markdown
---
type: person
date: 2026-05-27
tags: [people, vendor]
ai-first: true
confidence: stated
---

# <Name>

**For future Claude:** [one-paragraph: who they are, relationship to the user, why they matter, when last interacted]

## Context
[...]

## Open threads
[...]

**Links:** [[<project>]] [[<related person>]]
```

### idea

```markdown
---
type: idea
date: 2026-05-27
status: parked | exploring | promoted
tags: [<domain>]
ai-first: true
confidence: speculation
---

# <idea title>

**For future Claude:** [one-paragraph: what the idea is, what problem it solves, why the user isn't building it yet]

## The idea
[...]

## Why it might work
[...]

## Why it might not
[...]

## What would graduate it to a project
[...]

**Links:** [[<related ideas>]] [[<adjacent projects>]]
```

### status (overwrites — most recent only)

```markdown
---
type: status
project: <slug>
date: 2026-05-27
ai-first: true
---

# <project> — current status

**For future Claude:** [one-paragraph: what state the project is in right now, what just happened, what's next]

## Phase
[current phase / milestone]

## What's done
[...]

## In flight
[...]

## Blocked
[...]

## Open loops (link to open-loops.md)
[...]

**Links:** [[<project>-README]] [[<project>-decisions]]
```

### open-loop

```markdown
## YYYY-MM-DD — Waiting on the user's call on N ambiguous skill decisions

**Loop:** Audit found N skills where the right home is ambiguous; sent decision map asking for "agree all" or flips.
**Waiting on:** the user
**Unblocks:** Phase 3 mechanic consolidation (can't finalize SKILLS_INDEX without locked decisions)
**ETA:** This session

**Links:** [[example-project]] [[audit-v1]]
```

### mechanic-state

Writes or updates `_system/second-brain/Mechanics/<mechanic-name>/state.md` — the third vault axis. Read by future-Claude in skill-intake Step 0 (per AGENTS.md § Future-Claude skill intake) before proposing any new skill, to see what already exists in that mechanic.

Each mechanic's state file has these sections (created lazily, appended to over time):

```markdown
---
type: mechanic-state
mechanic: web-scrape
last-touched: 2026-05-28
importance: 5
ai-first: true
---

# web-scrape — current state

**For future Claude:** This file is the source of truth for what web-scrape already does. Read it BEFORE proposing a new scraping skill or wrench. If a need overlaps an existing wrench, expand the wrench instead of adding a new one.

## Current wrenches
- search — native web-search discovery
- scrape — one URL through the evidence-selected Scrapling fetcher
- map — sitemap/robots/internal-link URL discovery
- crawl — bounded URL manifest plus Scrapling loop
- interact — live browser session, also serves Cloak Browser tier-2 fallback
- agent — schema-first normalization with local validation

## Recent additions / changes
- 2026-07-17: Replaced provider-first commands with task-shaped free routes and local schema validation.

## Known gaps the user has surfaced
- (none yet — capture as they arise)

## Trigger phrase patterns owned
- "scrape this", "get the content from", "extract from URL", "crawl this site"
- "find pages about X on Y.com", "search the web for"
- "log into and pull", "structured data from", "all products from this catalog"

## Cross-mechanic dependencies
- printing-press-router fires first on integration intent
- router for downstream Codex/Gemini dispatch
- second-brain for scraping lessons in Actions/scraping.md
- build consumes outputs for ingest layers
- seo wrenches call into web-scrape for SERP / competitor work

## Open considerations
- task-shape scraping route selected from the current web-scrape table
- scrape-lesson-log.py helper script (Phase 5, Codex deliverable)
```

Updates happen via:
- `skill-forge` on scaffold (writes the initial file when a new mechanic or wrench is added)
- End-session ritual category 6 (new skills found + where they integrate)
- `skill-scout` on a trigger-phrase miss (notes the gap)
- The user manually when something material changes

**Append rules:** new sections at the top of their list; date-stamp every addition under "Recent additions / changes"; update `last-touched` in frontmatter on every write; keep or set `importance` so recall can rank the note.

---

## What capture does NOT do

- **Does not write trivia.** "the user ran ls" — no. "the user ran ls and discovered the old vault is big enough to archive" — yes (as a decision or status).
- **Does not write raw code.** Source code lives in source files, not the vault. The vault notes the DECISION ABOUT the code.
- **Does not write errors without fixes.** All four fields (what / cause / fix / prevention) are required. Half-fixed errors stay as open-loops until rooted.
- **Does not chase every passing thought.** the user says "save this" or it doesn't get captured. The exception is errors and corrections — those auto-capture (silently, not interrupting) on the principle of "no errors twice".
- **Does not announce captures to the user.** Just write and continue. They can read the vault whenever; interrupting flow with "I've saved that to <path>" is noise.

---

## Wikilink discipline

Every capture includes `[[wikilinks]]` to:
- The project (if project-axis)
- Related preferences / decisions / people
- Future-target notes that don't exist yet (marks them as worth writing)

Link liberally. A `[[name]]` that doesn't match anything yet is fine — it's a pointer for future-Claude to fill in.

---

## Vault access

Per the mechanic, use whichever access method is available:
- **MCP server `mcp-obsidian`** if `append_content` / `write_file` tools are available
- **Direct filesystem** (Write tool, or Edit for append) as fallback

Both produce the same vault content. Don't bother the user about which one.

---

## See also

- [../SKILL.md](../SKILL.md) — the second-brain mechanic
- [recall.md](recall.md) — the read side
- [end-session.md](end-session.md) — the full ritual that batches captures + everything else
- [learn.md](learn.md) — manage the body of captures after they've accumulated
