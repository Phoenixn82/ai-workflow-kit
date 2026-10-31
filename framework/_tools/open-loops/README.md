# open-loops — find, reconcile, plan, build, check and close open loops

The reusable Claude+Codex workflow the user asked for on 2026-10-02. One phase per invocation; the main
loop reads each result before launching the next phase. There is no default model: every `agent()` takes
model and effort from its route class in `_system/lanes.json` "routes" (`route list`, `route <class>`), mirrored
in the script's `ROUTES` block (Workflow scripts cannot read files; re-sync it when lanes.json changes):
extractors, closers and the Astra runner = `bulk-sweep` (Haiku medium); reconcilers, synthesis and revise =
`research-synthesis` (Opus high); builders = `feature-build` (Opus high); the wave refuter = `verify-judge`
(Opus xhigh). Plans tag every item with a route class (schema tier enums = route classes + `you` + `local`).
Sonnet is not routed; Terra is retired.

Files
- `prep.py` — zero-token helpers: `condense` (handoff-v2 prefilter over Claude transcripts + Codex rollouts),
  `batches` (extract-stage manifest), `merge` (flatten + dedupe extractor output into `loops-raw.json`).
- `close.py` — zero-token vault closer: writes one dated, marker-idempotent sweep entry per project NOTES.md
  and the canonical sweep record under `Projects/workflow/`.
- `$HOME/AI_Projects/.claude/workflows/open-loops.js` — the Workflow script (phases below).

Run (from any shell; `ROOT` is a scratch dir, e.g. the session scratchpad)
```bash
export OPEN_LOOPS_DIR=$ROOT
python3 _tools/open-loops/prep.py condense --days 14 --out $ROOT
python3 _tools/open-loops/prep.py batches --out $ROOT            # prints batch ids
```
Then from Claude Code (ultracode on), one Workflow call per phase, each capped at 12 agents: (merge the JSON from `route --workflow-args` into
every phase's args as `routes` and `codex`: the live model picks, which change when Claude runs ahead of its weekly pace):
```
Workflow({name:'open-loops', args:{phase:'discover',  root:ROOT, ids:[...]}})        # bulk-sweep extractors
python3 _tools/open-loops/prep.py merge <task output json> --out $ROOT               # -> loops-raw.json
Workflow({name:'open-loops', args:{phase:'reconcile', root:ROOT, clusters:{name:[slugs]}}})
python3 _tools/open-loops/prep.py reconciled <task output json> --out $ROOT          # returned objects are authoritative -> reconciled/*.json
Workflow({name:'open-loops', args:{phase:'plan',      root:ROOT, intent:'<verbatim ask>'}})  # research-synthesis -> Astra -> research-synthesis
Workflow({name:'open-loops', args:{phase:'build',     root:ROOT, wave:1, items:[{id, loop_ids, risk, route?, prompt, writes, git, shared}]}})  # feature-build only; model/effort are rejected
python3 _tools/open-loops/prep.py results <task output json> --out $ROOT             # every record incl. failed fallbacks -> results/<id>.json
#   Codex build items: main loop runs `cx long-unattended-build workspace-write ...` per item, captures the diff to results/<id>.diff, writes results/<id>.json
#   Review: main loop runs `cx code-review read-only ...` over every diff_ref; P1 -> one build fix round + one review re-check
Workflow({name:'open-loops', args:{phase:'check',     root:ROOT, wave:1, items:[{id, risk}]}})  # one verify-judge refuter reads results/*.json
#   verified := refuter pass && (no diff || code-review has no P1)  ->  write $ROOT/checks.json {"verified":[ids]}
python3 _tools/open-loops/close.py --root $ROOT --run ol-YYYY-MM-DD   # Done only for verified+done; writes Projects/workflow/open-loops-<YYYY-MM-DD>.md
```
Item ids, cluster names and group names must match `^[a-z0-9][a-z0-9_-]{0,80}$` (they become file names).
Codex is reached only by `cx`: the plan phase's `plan-review` (Astra) from its bulk-sweep runner; `long-unattended-build` builds,
`code-review` diff reviews and the closing `plan-review` once-over from the main loop; never as an `agent()` model. Flags and the stdin/filesystem-boundary rules: `_system/skills/router/wrenches/codex.md`.

Pace
- `route --workflow-args` lists in `codex` the classes that run on Codex right now. A phase that needs one throws; run that
  stage from the main loop instead (`cx feature-build workspace-write ...` per build item, like the Codex build items above;
  `cx research-synthesis read-only ...` / `cx verify-judge read-only ...` with the stage's prompt), or rerun later.

Gates
- Read `~/.local/state/omarchy/agents/usage/claude.json` before every phase; no fan-out past 40 % of the 5-hour window.
  Budget about two phases per 5-hour window (extractors run on Haiku via bulk-sweep).
- Builders never send, post, push, delete, restart services, or edit another host; live mutations get their own
  wave with precondition, backup and rollback. Deletions and anything outward-facing are the user asks.
