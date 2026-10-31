---
name: recall
description: Wrench inside the `second-brain` mechanic. Read side — find, search, restore context, list. Scoped auto-pull fires automatically when the user names a project not yet loaded this session. Fires on "what do I know about X", "where was I", "show me the notes on Y", "find anything about Z", "resume", "restore context", "pick up where I left off", "remind me what I decided about W".
---

# recall — vault read side

Read into the vault when the user needs context, asks about prior work, or names a project not yet loaded. Replaces the old `context-restore` skill plus search functionality. Three modes:

1. **Scoped auto-pull** (automatic when a project is named)
2. **Find / search** (explicit query)
3. **Restore context** (resume a prior session)

---

## Mode 1: Scoped auto-pull

Fires automatically — no the user prompt needed — when the user names a project that's not in the current session's loaded set.

**Detection:** when the user's message mentions a project name (matches a `Projects/<slug>/` folder) AND second-brain hasn't loaded that project this session yet.

**Action:** read these files BEFORE responding to the user's message (any that are absent — file sets diverge per project, e.g. agentic-os-2 has no `_README.md` — just skip; `status.md` is the reliable anchor):
1. `Projects/<slug>/_README.md` (if it exists) — what the project is, north star
2. `Projects/<slug>/RESUME.md` if it exists — merged done/next/blockers/noted (the `_brief.md` pointer block was already injected at SessionStart; do not re-read it). Only if there is no `RESUME.md`: `status.md` — legacy current state
3. Only if there is no `RESUME.md`: most recent `next-prompt.md` — legacy context from last session

**Why before responding:** the user's question may need context from these files. Loading after answering risks answering with incomplete picture.

**Token budget:** ~2-5 KB total. Stays bounded. Don't read decisions.md / errors.md / tokens.md unless explicitly asked.

**Silent operation.** Don't announce the load to the user. Just do it, then answer with context.

---

## Mode 2: Find / search

Fires on: "what do I know about X", "find anything about Y", "search for Z", "show me the notes on W".

**Two paths:**

### Path A: `vault-recall` (hybrid BM25 + vector over every vault page; `_tools/vault-rag`, 2026-10-12)

```bash
vault-recall "<question in plain words>" -k 5      # path#heading, score, snippet; add --fts-only when thor is asleep
# then Read the top hit(s)
```
The UserPromptSubmit hook already injects the top 3 hits for most prompts; call it by hand for a deeper or rephrased search.

### Path B: exact strings (fallback)

```bash
rg -i "<exact phrase>" $HOME/AI_Projects/_system/second-brain
```

**Candidate ranking:** When multiple notes match, surface the active project's `open-loops.md` and `next-prompt.md` first. Then rank remaining candidates by frontmatter `last-touched` recency plus `importance` (1-10, default 5). Grep/keyword search over the index and notes is the current baseline; adding embeddings / semantic recall is welcome and encouraged if it improves retrieval quality.

**Result format** — present results as a tight summary, NOT a file dump:

```
Found 3 relevant notes:

1. **Projects/example-project/decisions.md** (2026-05-27) — "Use Cloak Browser as fallback, not Chrome extension"
2. **Actions/scraping-tier.md** — measured web lanes (2026-09-30 bake-off): `web read` / `web act` / `web handoff`, all via the `web` skill
3. **Projects/example-project/errors.md** (2026-05-27) — snapshot tar exit code 255 false alarm

Want the full text of any of these?
```

If a single result is clearly The Answer, present it inline. If multiple, offer to expand.

---

## Mode 3: Restore context

Fires on: "resume", "restore context", "where was I", "pick up where I left off", "continue from yesterday".

### Step 1: Identify which session to restore

If the user specified a project, scope to that. Otherwise look for the most recent handoff across all projects:

```bash
# Find most recent handoff. Handoffs live in TWO shapes/locations:
#   Projects/<proj>/sessions/HANDOFF_<date>*.md   (most projects — uppercase, nested)
#   Projects/<proj>/handoff-<date>*.md            (a few — lowercase, top-level)
# Use a recursive, case-covering glob (the old `Projects/*/handoff-*.md` missed ~23 of 25).
# NOTE: root `path` at the Projects/ dir and DROP the leading `Projects/` from the pattern —
# the Glob tool returns nothing when `path` is a parent dir AND the pattern repeats a literal
# leading segment (verified 2026-06-13). Keep the pattern relative to `path`.
Glob(pattern="**/HANDOFF_*.md", path="$HOME/AI_Projects/_system/second-brain/Projects")
Glob(pattern="**/handoff-*.md", path="$HOME/AI_Projects/_system/second-brain/Projects")
# Pick the newest by the YYYY-MM-DD embedded in the filename (filename sort ≠ recency when names differ).
```

### Step 2: Read the handoff file

The handoff file (emitted by the end-session ritual) is the canonical restore entry. It contains:
- "For future Claude" preamble (the situation)
- State at end of session (what was done, what's in flight, what's blocked)
- **Exact next prompt** (the actual prompt for resuming)
- Open loops

### Step 3: Also read

- `Projects/<slug>/_README.md` (project front page)
- `Projects/<slug>/status.md` (current state)
- `Projects/<slug>/open-loops.md` (waiting threads)

### Step 4: Present the restore briefing to the user

Tight format, not a paste-the-file dump:

```
Resumed: [[example-project]]

**Where we were:** Phase 3 mechanic consolidation. router done. second-brain in progress (mechanic SKILL.md written, wrenches next).

**Next step from handoff:** Build remaining wrenches for second-brain (capture, recall, end-session, learn). Then move to guard mechanic.

**Open loops (2):**
- guard mechanic not yet built
- 10 on-demand mechanics to build after always-core trio

**Suggested first action:** Continue Phase 3 by writing the four second-brain wrenches in parallel. Want me to proceed?
```

If the handoff file's next-prompt is precise enough, use it verbatim as the suggested first action. Don't add ceremony.

---

## Cross-Conductor restore

For sessions started across Conductor workspaces (different OS, different project root), the handoff file's paths are absolute and portable. The restore works the same — just `glob` the vault for the latest handoff matching the project slug.

If `claude_code_session_id` differs and the handoff file is from a different machine, surface that: *"Resuming handoff from session <id> (started in <workspace>). Vault paths are portable but local file paths in the handoff may need adjustment."*

---

## What recall does NOT do

- **Does not load the whole vault.** Scoped is the rule. If the user wants a full vault dump, that's `learn export`, not recall.
- **Does not write to the vault.** Read-only. Captures and updates go through `capture` or `end-session`.
- **Does not invent context.** If the vault has no entry for the query, say so plainly. Don't fabricate "based on what I remember".
- **Does not surface stale claims unverified.** A memory file naming an old plan price may predate a plan change. Recency markers on entries (the `(as of YYYY-MM-DD, source)` in the AI-first rule) make verification visible. When a recalled fact has a date older than 30 days AND the user is about to act on it, flag the staleness.

---

## Special triggers

| Phrase | Behaviour |
|---|---|
| "what do I know about <thing>" | Mode 2 search, vault-wide |
| "what did I decide about <thing>" | Search scoped to `Projects/*/decisions.md` and `Actions/*.md` |
| "show me the errors on <project>" | Read `Projects/<slug>/errors.md` directly |
| "remind me how <preference>" | Read the matching `Actions/<slug>.md` |
| "what's in flight on <project>" | Read `Projects/<slug>/status.md` |
| "who is <name>" | Read `People/<name>.md` if exists; otherwise say no record |
| "resume" / "where was I" | Mode 3 restore |
| "pick up <project>" | Mode 3 restore scoped to project |

---

## See also

- [../SKILL.md](../SKILL.md) — the second-brain mechanic (scoped auto-pull defined there)
- [capture.md](capture.md) — write side
- [end-session.md](end-session.md) — emits the handoff files this wrench reads
- [learn.md](learn.md) — manage the body of stored knowledge
