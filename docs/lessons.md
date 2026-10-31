# Lessons from a whole-workflow audit

In October 2026 the setup this kit comes from was audited end to end: 30 areas, a few hundred agents gathering evidence, each finding checked by a separate refuter, then rounds of review by a model from another lab. These are the lessons that generalize. Each one changed how the kit is built.

## 1. Prose rules fail; delivered machinery works

A rule written in an instruction file is a hope. The web-tools rule was written in 8 places and still missed 6 times; the day it became a real CLI registered as a skill, it got about 1,900 calls in 12 days and banned tools dropped to zero. A "load this skill first" rule missed nearly every time, even after its bug was fixed, and the maintainer's correction rate stayed flat across two rebuilds of the rules.

**Do this:** when a rule matters, ship it as a hook, a CLI, a lint or a test. Keep instruction files short enough to be read, and put the rest in tools that run whether or not the model remembers.

## 2. Every rebuild added a layer and removed nothing

Ten audits and 13 re-architectures in 139 days left the same rule in 11 places, 8 layers setting output style, 3 piles of memory, 2 writers of the same file and 5 overlapping watchdogs. More of the maintainer's prompts went to building the workflow than to the projects it was meant to serve, and project work only won in the weeks with the fewest harness changes.

**Do this:** remove before you add. Give every new service, daemon or automation a **kill date** (a "review by" day about 30 days out) and a question to answer then: still used, and still cheaper than not having it? No answer means it goes. List every automation with its kill switch in one registry, and let a lint fail when one is missing. → [safety](safety.md#automations-opt-in-registered-one-kill-switch-a-kill-date)

## 3. Nothing had a feedback loop, so everything decayed silently

The memory index was overwritten and 142 of 185 memories went dark for 22 days. The free-model router's container lost DNS and kept reporting healthy for days. A scheduled scraper failed 9 days out of 9. An uptime monitor was red for 10 days over a typo. Token cuts made in June had regrown 2.7 to 5 times by October.

**Do this:** a check that says "healthy" is not proof of work; probe with a real call (one real completion, one real query). Send one quiet alert per state change, not per check. Run a **weekly review** that reads the past week's facts and failures, and have a separate verifier judge its changes: the doer is not the judge. → [`framework/_tools/weekly-review/`](../framework/_tools/weekly-review/), [`framework/_tools/fleet-probes/`](../framework/_tools/fleet-probes/)

## 4. Measure context cost before and after

About 75k tokens were loaded before the first prompt of every session, and every subagent paid about 54k of it again: one month of subagent spawns cost 24.7M tokens of pure overhead. Most of it was in nobody's view: hundreds of MCP tool names, skill listings for skills that never fired, plugin bootstraps, a 26 KB instruction file.

**Do this:** read the first turn's input tokens in a fresh session, change one thing, read it again. Put budgets in a lint (instruction-file lines, boot-file characters, number of listed skills) so cuts can't silently regrow. → [mcp and plugins](mcp-and-plugins.md), [`framework/_tools/harness-lint/`](../framework/_tools/harness-lint/)

## 5. One source of truth for models

The main session's model flipped 15 times in 41 days, because an interactive model switch rewrote the global default. Subagents inherited whatever it was. The public-push gate pinned a model that had been retired, so every public push failed, and its override skipped the secret scan too.

**Do this:** keep one table (`lanes.json`) that says which model and effort each lane and each kind of job gets, have one script write it into every tool's config, and lint the live configs against the table. Pass the model explicitly when you dispatch. → [agents and lanes](agents-and-lanes.md)

## 6. Adversarial review by a model from another lab converges

Fresh, read-only review rounds by a strong model from a different lab, over the work that was actually executed, found 25 problems in round 1 (17 blocking or high), then 5, 3, 4 and 1 new blocking-or-high findings, with zero blocking from round 4 on; round 6 closed everything and found nothing new. An earlier migration plan went 24 → 13 → 0. The second model doesn't share the first one's blind spots.

**Do this:** start each round in a fresh session with only the diff and the last round's findings. Send every finding through a fix and a separate check before the next round. Cap the rounds, and if you hit the cap without an approval, say so instead of declaring victory.

## 7. Tests must never touch real data

A test that imported the memory pipeline before the fixture set its temporary paths wrote test notes into the real vault, twice in one day. They were reverted before any commit, but only because someone looked.

**Do this:** put the guard in the code, not in the test's good manners. The pipeline now refuses to import under a test runner until its state and vault paths are set (the test setup points them at temporary folders), so a misordered test fails loudly instead of writing. → [memory](memory.md#handoff-v2-automatic-session-distillation)

## Smaller ones

- **Fail closed where it matters.** A safety hook that crashes should deny, not wave the call through; an ordinary hook that crashes can be ignored.
- **Put always-on things on the always-on box.** Prompt-time search kept falling back to keyword-only because its embed model lived on a GPU box that sleeps. It moved to a small CPU container on the always-on server. → [fleet](fleet.md)
- **Archive, don't delete, and keep a manifest.** Every cleanup that can be restored byte for byte is a cleanup nobody has to be careful about.
- **A security tool is a guard until proven a boundary.** Write down what the secret firewall can't catch next to what it can. → [safety](safety.md#the-secret-firewall-and-what-it-cant-do)
