# lane-intel

Daily: watch Theo (t3.gg) on YouTube + X and read slopalytics.com, fold their model/effort advice into a per-lane stance,
and adjust `_system/lanes.json` the way they say (the user: "adjust the lane setup as they see fit"). Stdlib Python on odin;
whisper fallback on thor's GPU.

`run.sh` (timer, 05:40 Etc/UTC +0-20 min): HALT + preflight -> `theo_watch` -> `x_watch` -> `slopalytics` ->
`stance` -> `propose`. Each step isolated; one Telegram line per run (heimdall-alert), only when something is new or failed.

| file | does |
|---|---|
| theo_watch.py | RSS (channel UCbRP3c757lWg9M-U7TyEkXA), skips Shorts (portrait or < 180 s) and uploads before `--since`, transcript = auto-subs (60 s); only when yt-dlp reports the video has no captions: audio -> thor `whisper_thor.py` (faster-whisper distil-large-v3, cuda int8_float16, 20 min kill), max 6 videos/run; a video gets 3 tries (a try killed mid-run counts), then `gave-up`: skipped by the daily run, one notify line, retry by hand with `--backfill=<id>`. A wall (429/403, bot check, captcha, rate limit, caption failure yt-dlp does not explain, timeout; not an age gate) marks the video `deferred`, charges no try, ends the run and alerts once: no audio fallback around a block |
| summarize.py + PROMPT-summarize.md | ONE `claude -p --json-schema` call per video / per X batch (model+effort from route `headless-summary`) -> note |
| x_watch.py | one `web read https://x.com/theo`, new status ids, snowflake times, pinned dropped; a post is a lane candidate only if it names a model, an effort level, a harness/agent tool or a benchmark, and a product/marketing post (nightly, release notes, sponsor, "T3 Chat now supports") only with an effort level or a benchmark; candidates -> one summarize call |
| slopalytics.py | index -> /assets/index-*.js (object literal tokenized, never eval) -> /data/t3-usage.json, each through `web read --raw` (the DOM wrapper is undone; a value the HTML parse mangled fails closed); <= 3 requests, a failed read stops the run; no robots.txt (404); snapshot only on new fetchedAt |
| stance.py | folds every theo/x note: per role x provider, newest source wins by (date, source time in UTC epoch: post snowflake; video `published:` or else 00:00 UTC of its date); inside one source an effort-naming claim beats a vague one |
| propose.py | APPLY or PROPOSE (rules below), runs `apply_lanes.py` (the only writer of settings.json / codex config); each changes.md line ends `[routes: ...]`, the task classes that use the lane |
| route.py (`~/.local/bin/route`) | not part of the run: reads lanes.json `routes` per call; `route list`, `route <class>` (Agent alias, Workflow `agent()` {model, effort}, `cx` or `lane` command), `route --json`, `route --cx` (used by `cx`), `--self-test`. Table lookup: `$ROUTE_LANES`, else `$KIT_HOME/framework/_system/lanes.json` when it exists, else `~/AI_Projects/_system/lanes.json`; `route --lanes-path` prints the one in use |

Vault (written): `Reference/lane-intel/{theo/<date>-<id>.md, x/<date>.md, slopalytics/<fetchedAt-date>.md, stance.md, changes.md}`.
State: `~/.local/state/lane-intel/` (theo.json, x.json, alerts.json, stance.json, costs.jsonl, transcripts/, slopalytics/, backups/ of lanes.json).
Thor: `~/.local/lane-intel/venv` (yt-dlp, faster-whisper, nvidia-cublas/cudnn-cu12, av<16) + `tmp/` (audio deleted after use).

**Apply rule.** Lane map: build/anthropic->claude.main, subagent/anthropic->claude.subagent, review/anthropic->claude.judge,
headless/anthropic->claude.headless, plan/openai->codex.plan, review/openai->codex.check, build/openai->codex.build+codex.interactive.
Applied only when Theo's newest pick names a model in `allowed_models`, an effort that is not max, is unambiguous within its source,
and is dated after the lane's last history entry (any non-lane-intel entry counts for every lane, so a backfill never undoes a
deliberate hand change). History gets `{date, by: "lane-intel", change: "<lane>: old -> new", source}`; lanes.json backed up first.
Everything else (no effort named, model not allowed, avoid hits, slopalytics same-provider dominance) goes to changes.md as
PROPOSED, once. claude.design, claude.sweep and local.text are never touched.

**Every script:** `--self-test` (`./run.sh --self-test` runs all); writers take `--dry-run` (`theo_watch --dry-run` still makes the
Claude call, per spec); `--out DIR` redirects notes; `LANE_INTEL_STATE=DIR` redirects state.

**Cost:** a summary costs well under a dollar of subscription credit per video or per day of X posts, provided the run
uses `--strict-mcp-config` (without it the preamble loads every MCP server and costs several times more).

## Install / first run (orchestrator)
```
./install.sh --dry-run && ./install.sh          # thor venv (idempotent), link units (a different unit already there is kept as <unit>.bak-<time>), daemon-reload (does NOT enable the timer)
./theo_watch.py --backfill=vu8X3YroB-w,38_6C0dkKmU,D8PikZ1KhUo,8WbW_n95wc4,vRS2leJLAI0,ND_kRgU_WlY,nYA0yASgaZI
./theo_watch.py --seed     # optional: mark the other current RSS videos seen (else the first timer run summarizes up to 6)
./run.sh                    # first full pass: X, slopalytics, stance, propose
systemctl --user enable --now lane-intel.timer   # only after the first run looks right
```
Backfill: always the `--backfill=<id>,<id>` form (ids can start with `-`, e.g. `-XWSJM-Ue-o`, which argparse reads as a flag
without the `=`); an explicit backfill is not capped at 6 and also retries `gave-up` videos. Window: `--since` (default
2026-09-28, a fixed date: the user 2026-10-12, only the last two weeks matter) marks older uploads `too-old`, in a backfill too;
pass `--since=YYYY-MM-DD` to reach further back. A stale upstream dataset raises one "slopalytics stale" alert.

## Snippets for the orchestrator
REGISTRY.md row:
`| lane-intel (added 2026-10-12) | daily 05:40 Etc/UTC +0-20 min on **odin** (user `lane-intel.timer`, Persistent) | claude -p (route headless-summary), one call per new video; whisper on thor | Theo YouTube/X + slopalytics -> vault Reference/lane-intel/, auto-edits _system/lanes.json via apply_lanes.py | `systemctl --user disable --now lane-intel.timer` or HALT |`
Hooks: none. settings.json: none (apply_lanes.py writes model/effort when a lane changes). Spend: each Claude call goes to
`record_spend.py` (tokens) and `costs.jsonl` (USD). Kill: `_system/automations/HALT`, budget ceiling 0, or disable the timer.
Twitter lane: `x_watch.py` is it (logged-out profile read only).
