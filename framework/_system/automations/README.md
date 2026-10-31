# Automation Safeguard

Replaces the old "nothing runs on its own" blanket ban (formerly AGENTS.md rule #1; now the Standing-automations line in `AI_Projects/AGENTS.md`). The user can stand up
**unlimited** automations; the only brakes live here.

## Files

- `budget.json` — `{ "daily_token_ceiling": N }`. **The per-day token cap is DISABLED as of 2026-07-17** (the user removed the old 10M/day breaker — they run on flat subscriptions, so plan limits are the real ceiling). The ceiling is parked at a huge finite integer so it never trips; `0` still = halt all (kept as a kill-switch). Edit the number to re-enable a real cap.
- `HALT` — if this file exists, every automation's preflight aborts. The kill-switch. Absent by default.
- `spend-ledger-<YYYY-MM-DD>.jsonl` — one line per automation run: `{ts, automation, tokens, status}`.
- `preflight.py` — the preflight gate (HALT + ceiling≤0 kill-switches; the daily cap is parked off). Run FIRST in every automation.
- `record_spend.py` — append a run's token usage. Run AFTER each automation's work.
- `REGISTRY.md` — visibility ledger of standing automations.

## Contract every automation follows

1. `python _system/automations/preflight.py` → if exit code ≠ 0, **STOP** (budget reached or HALT). Do no work.
2. Do the work.
3. `python _system/automations/record_spend.py <base> <automation-name> <tokens> <status>` to log spend.

## Worked example (daily cap currently DISABLED)

How the cap worked when enabled (e.g. ceiling = 10,000,000): a manager run uses 1.2M tokens →
ledger sums to 1.2M → under ceiling, next run allowed; by the 9th run the day's sum hits ~10.8M →
preflight returns non-zero → all automations halt until the next UTC day or the user resets. **As of
2026-07-17 the ceiling is parked unreachably high, so this cap never trips** — only the `HALT` file
and a manually-set `0` ceiling stop automations now.

**Subscription credits only — never metered API credits.**
