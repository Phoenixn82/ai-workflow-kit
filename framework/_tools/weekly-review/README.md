# weekly-review

Weekly check of the whole agent workflow (the user: "checks on the last week of workflow, checks how everything
works, updates processes and does research on better improvements"). systemd --user timer on odin, Sundays 03:17
Etc/UTC, Persistent. A deterministic shell around two short `claude -p` runs; the doer is not the judge.

| stage | file | tokens | what |
|---|---|---|---|
| 2 review | `PROMPT.md` | route `headless-summary` (Opus 5.5 high today), 80 turns, $25 cap | reads facts + lanes + registry, researches upstream (CHANGELOG, Codex releases, Anthropic engineering, lane-intel), writes `second-brain/Projects/workflow/weekly/<date>.md` + phone HTML `~/Review Queue Inbox/weekly-review-<date>.html`; applies safe class only |
| 3 verify | `VERIFY.md` | route `verify-judge` (Opus 5.5 xhigh today), 30 turns, $12 cap | prompt = VERIFY.md + the report + `diff --stat` since a pre-run snapshot (`git stash create`) + the full diff of OUT paths only, cut at 30 KB; reads with `git -C <vault or _system> status/diff/log/show` (no `cd`), reverts safe-class files to BASE (`git checkout <BASE> --` only for the weekly dir and REGISTRY.md, `cp` from `pre/` for SKILLS_INDEX/DECISION_MAP), lists OUT paths, appends `## Verifier` |

Then `run.sh`, before any commit, sends a FAILED line and exits 1 unless the verifier's verdict is `pass` and its own
fence holds; only then it commits the report (vault) and `automations/REGISTRY.md` (_system), logs
cost + modelUsage + permission denials per stage to `costs.jsonl`, and sends one silent Telegram line via heimdall.
Automation contract: every HALT check also runs `_system/automations/preflight.py` (HALT or a `budget.json` ceiling of 0
stops the run), and each stage books its real token total (sum of `modelUsage` across models) with `record_spend.py`.
Quiet week (no Claude or Codex sessions, 0 failures, no harness-lint FAIL, restic age and every host's facts read) skips
stages 2-3.

Theo: the report's "Theo this week" section lists each claim and tip with its link and maps it to a workflow
adjustment or a "no change, because"; adjustments become `Theo:` proposals (lane changes tagged `review`) for the user's
interactive session. A week with new Theo claims or tips is never a quiet week.

Safe class: regenerate SKILLS_INDEX.md (only the drift collected.md lists; `collect.py --skills-index` regenerates it); append REGISTRY rows for unregistered timers; fix moved paths in
DECISION_MAP.md; flip `relayed: true` on stub Codex manifests; list stale memories. Everything else is a proposal
tagged safe | review | you.

Fences, outermost first: `--permission-mode dontAsk` with Edit allowed only on those paths (absolute `Edit(//...)`
rules; settings' installer and `cd * && ...` allows plus every hard exclude are in `--disallowedTools`); no MCP
(`--strict-mcp-config`); `--max-turns`, `--max-budget-usd`, `timeout -k`; the verifier; `run.sh` re-diffing after the
verifier and before any commit (exit 1, nothing committed, "FENCE" in the FAILED line if anything outside the allowed set
changed; a concurrent Claude or Codex session excuses only paths its transcript shows it wrote after the snapshot (a
successful Claude Edit/Write/MultiEdit/NotebookEdit, or a Codex FileChange event with status "completed" for exactly that path, which Codex logs only for an applied patch) and no weekly-review
session wrote; a mention or a read excuses nothing; excused paths are listed, never reverted, and counted in the Telegram
line; anything unattributed, Bash-made changes included, is held for the user).
A vault or _system commit failure (hook, lock, disk) is a FAILED run; only "nothing staged" counts as nothing to commit.

## Use
    ./run.sh --self-test   # offline checks, also runs collect.py --self-test
    ./run.sh --dry-run     # prints both claude commands, commits and the Telegram call; runs nothing
    ./run.sh --smoke       # stage 2 only, 10 turns, report into ~/.local/state/weekly-review/smoke/, applies nothing
    ./run.sh --verify-only 2026-10-12   # rerun stage 3 + commit + Telegram on that date's existing review (needs pre/ + review.json)
    ./collect.py [--dry-run | --out DIR]   # stage 1 alone (--out: test copy, leaves the state dir alone)
    ./collect.py --skills-index   # rewrite ~/AI_Projects/SKILLS_INDEX.md from the filesystem
    ./install.sh [--enable|--dry-run|--self-test]   # copies units/ into ~/.config/systemd/user; enables the timer only with --enable
HALT: every entry point exits 0 with "HALT present" when `_system/automations/HALT` exists (re-checked before each
claude stage). Kill: `systemctl --user disable --now weekly-review.timer`.

## For the orchestrator
REGISTRY.md row:

    | weekly-review (added 2026-10-12, the user: weekly cron that checks the last week of workflow and researches improvements) | Sundays 03:17 Etc/UTC on **odin** (`systemd --user` `weekly-review.timer`, Persistent; holds a sleep inhibitor while running) | collect: zero tokens; review: one `claude -p` on route headless-summary (<=80 turns, $25 cap); verify: one `claude -p` on route verify-judge (<=30 turns, $12 cap) | 7-day health facts from odin/heimdall/thor -> report `Projects/workflow/weekly/<date>.md` + `~/Review Queue Inbox/weekly-review-<date>.html`; applies only the safe class (SKILLS_INDEX regen, REGISTRY rows for unregistered timers, DECISION_MAP moved paths, stub manifests relayed); commits report + REGISTRY.md; one silent Telegram line. Source `_tools/weekly-review/` | `systemctl --user disable --now weekly-review.timer` or create `HALT` |

Optional, if odin auto-suspends overnight: a heimdall `weekly-review-wake.timer` at
`Sun *-*-* 03:07:00 Etc/UTC` running `fleet-wake wake odin`; without it Persistent=true runs the review at
odin's next wake. No hooks, settings or lanes.json changes are needed. Runs wake thor via `thor-up` (restic, journal).
