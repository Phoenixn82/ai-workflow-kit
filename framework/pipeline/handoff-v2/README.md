# handoff-v2

Automatic session distillation into the vault.

Every session segment is filed under the project(s) it actually touched and
distilled by Claude Haiku (lane `claude.distill`; thor's biggest local Qwen,
then `qwen3:8b`, as fallback - see Backends below) into
per-project `history.md`, `decisions.md`, `errors.md`, `lessons.md` and a merged
`RESUME.md`; a bounded `_brief.md` is pre-built for the next session to inject
at SessionStart. The user runs no commands.

## Why it is cheap

Claude Code already writes every session to `~/.claude/projects/<cwd>/<id>.jsonl`.
Prose is **0.4%** of that file (measured: 23.6 KB of 5.92 MB), so a pure-Python
pre-filter does ~99.6% of the reduction before any model is involved, and the model
sees a small prompt (~15K tokens).

## Flow

```
SessionEnd   -> seal segment, enqueue, detach a worker, return in ms
PreCompact   -> seal segment, enqueue only
SessionStart -> reconcile (local), spawn detached worker, inject pre-built brief

worker (detached): vault-sync pull -> prefilter (timeline + routing signals)
                   -> resolve (evidence -> candidate projects)
                   -> map each chunk -> synthesis -> deterministic gates
                   -> commit result -> render history/decisions/errors/lessons/
                      RESUME/journal per project -> briefs -> vault-sync push
                   -> unload thor's model when the queue is empty (if thor was used)
```

No hook does network work or model work. The **queue**, not SessionEnd, is the
durability guarantee: SessionEnd cannot run on SIGKILL, so `reconcile` recovers a
killed session from byte offsets (manual today; SessionStart does not call it yet).

Durability (audit 2026-10-12, F1): every write of `transcripts.json` holds one
global cursor lock; a seal copies its byte range to `state/spool/<job>.jsonl` and
the drain reads that copy (a rewritten or pruned transcript cannot tear or lose
it); a drain claims each job, and the next drain returns a dead drain's claim to
the queue. Outages (thor or Claude unreachable, usage limit, network, model
timeout, 5xx) defer without spending attempts; bad output and broken jobs count,
and the 6th counted failure moves the job to `state/quarantine/failed/`.

## v2 (2026-09-28): sorting, quality, category homes

- **Routing by evidence** (`resolve.py`, `project-map.json`): edits, command
  paths, ssh hosts, vault project folders and the user's own words score each
  project. The start folder no longer decides - mapping `~/AI_Projects -> workflow`
  misfiled most jobs. Multi-project sessions split per item.
- **Whole-session distillation** (`distill.py`): map over chronological chunks,
  then a synthesis pass. Items must cite ids the prefilter minted; quotes of
  The user must be verbatim (pasted blocks excluded); everything is re-redacted.
- **Eval** (`eval/`): sessions with Claude-written HANDOFFs, gold fact
  checklists and a blind judge.
- **Lessons** go to `Projects/<slug>/lessons.md` as `[candidate]` entries, never
  into the brief. `python3 -m handoff.cli lessons` lists them for review.
- **Idle thor holds nothing**: keep_alive 10m during a drain, unloaded when the
  queue empties. `state/PAUSED` holds the queue.

## Backends (2026-10-12)

The user, 2026-10-12: "maybe it would make more sense to use haiku for that now".
A local 27B is slower per job and holds GPU memory other jobs need.

- `HANDOFF_DISTILLER=auto` (default): Claude first, model and effort from lane
  `claude.distill` in `_system/lanes.json` (`claude-haiku-5-5`, medium), sent
  through `claude -p` on the subscription with the same prompts and schemas as
  thor and the same gates after. Any Claude failure (exit code, `is_error`,
  usage or rate limit, no network, timeout, bad JSON twice) reruns the job on
  thor; a limit or outage keeps the rest of that drain on thor. If thor is
  unreachable too, the job defers exactly as before. Never thor -> Claude.
- `HANDOFF_DISTILLER=claude`: Claude only; a failure defers the job.
- `HANDOFF_DISTILLER=ollama`: **switch back** to thor only (the pre-10-12 path).
  Drains are spawned by both Claude's and Codex's hooks, and Codex's never see
  `~/.claude/settings.json` -> `env`, so the switch that covers every drain is the
  default in `handoff/config.py`: `DISTILLER = os.environ.get("HANDOFF_DISTILLER",
  "ollama")...`. A missing `claude.distill` lane also means thor.
- Redaction is unchanged and still runs first: every prompt is built from text
  `prefilter.build` already redacted (`tests/test_claude_distill.py` checks that
  no secret reaches the `claude` process).
- Size: at most 150K chars of prefiltered transcript per call (~45K tokens,
  under Haiku's 100K-token price step); longer segments are chunked, not cut.
  Recent segments were 0.1-45 KB, so one map call plus one synthesis.
- Cost: each call's `total_cost_usd` and `modelUsage` tokens go into the job's
  `stats` (`backend`, `wall_s`, `cost_usd`) and one line of `state/costs.jsonl`.
- thor can sleep more: a drain Claude handled never contacts thor, not even to
  unload. It is woken only for a fallback.
- Compare the two on your own jobs with `eval/compare.py` (outputs in
  `state/eval/compare/`): a blind Opus judge with the eval rubric scored Haiku
  well ahead of a local 27B, with about half the fabrications, at a few cents of
  list price per job.
- `python3 eval/compare.py` reruns the backend comparison (see its docstring).

## Commands

```bash
python3 -m handoff.cli status      # queue depth, redactor, thor reachability
python3 -m handoff.cli queue status  # queued/in-progress/deferred/quarantined, with ages
python3 -m handoff.cli drain       # process queued segments (normally detached)
python3 -m handoff.cli reconcile   # enqueue anything the hooks missed
python3 -m handoff.cli lessons     # candidate lessons across projects
python3 -m handoff.cli refile      # dry-run re-resolve of archived jobs (--apply rebuilds)
python3 eval/run.py --variant v2 --model <tag>   # then eval/score.py stage|score
python3 -m handoff.install         # dry-run migration audit
python3 -m handoff.install --apply # wire hooks (refuses while ai-memory is live)
python3 -m unittest discover -s tests
```

## Layout

| Path | Role |
|---|---|
| `handoff/prefilter.py` | deterministic reduction, candidate ids, redaction |
| `handoff/redact.py` | pre-transmission redactor (the tripwire cannot redact) |
| `handoff/seal.py` | locked segment sealing + repo fingerprint at seal time |
| `handoff/store.py` | locks, cursors, FIFO queue, admission, retention |
| `handoff/distill.py` | map/synthesis passes on Claude or thor, fallback, deterministic gates |
| `handoff/render.py` | idempotent per-project renderers (history/decisions/errors/lessons/RESUME) |
| `handoff/brief.py` | pre-computed brief, freshness fingerprint, withholding |
| `handoff/resolve.py` | evidence-scored project routing over the catalog in `project-map.json` |
| `state/` | machine-local; never synced, never in the vault |

## Measured behaviour

| | |
|---|---|
| 5.92 MB transcript -> prefiltered | 40.6 KB |
| Pass A + Pass B | ~16K tokens in |
| Claude per job (Haiku) | 2 calls, a few cents at list price (subscription) |
| Release gates | 29 passing |

## Things that bit, kept as comments in the code

- `type:"user"` is not the user: **174 tool_result blocks to 4 text blocks**.
- `is_error` is unreliable - `error: unknown option '-S'` arrived with it false.
- Uncapped `num_predict` let a short prompt run for many minutes.
- Unconstrained output loops: the model repeated four `done` items until the cap.
  A grammar-constrained schema cut it to 140 tokens.
- Bytes-per-token varies widely across sessions, so the ratio is calibrated
  from measurements, not assumed. A fixed 3.0 discarded almost every action.
- Inspection is not work: `cat`/`sed -n`/`ls` were being distilled as achievements.
