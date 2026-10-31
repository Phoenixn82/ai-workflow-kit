# Philosophy

The whole kit rests on a few habits. Adopt these even if you take no code.

## 1. Few skills, all of them used

A skill costs context in every session (its name and description load up front), so one that never fires is pure cost. The kit keeps a short list, and everything on it is in daily use:

| Kind | What it is | Where | Examples |
|---|---|---|---|
| **Skill** | The agent fires it by name when a job matches | [`framework/harness/skills/`](../framework/harness/skills/), each tool's `skill/` folder | `codex-build`, `codex-review`, `grill-me-codex`, `web`, `telegram`, `motion`, `jev-browse` |
| **Reference mechanic** | A doc the agent reads for a domain; not fired | [`framework/_system/skills/`](../framework/_system/skills/) | `guard` (the public-push gate), `second-brain` (memory), `router` |
| **Plugin** | Installed from its own marketplace | [`install/tools.json`](../install/tools.json) | superpowers (brainstorm, then plan), Ponytail, frontend-design, Impeccable |
| **Workflow** | A scripted fan-out of agents | [`framework/.claude/workflows/`](../framework/.claude/workflows/) | `open-loops` |

**Check the list before improvising.** Before building a tool for a job, the agent checks its skill list and fires a match. When nothing fits a job you do often, it offers to draft a skill instead of improvising the same thing every time.

**Before a skill exists it must:**
1. solve a real, recurring need (no "might be handy");
2. not duplicate a skill, plugin or tool you already have;
3. stay readable (split reference material out when it grows);
4. be wired in (listed in `SKILLS_INDEX.md`, with trigger phrases).

The first version of this system had over a hundred skills, most of which never fired; a later audit archived more that had not fired in six weeks. What is left: [`framework/SKILLS_INDEX.md`](../framework/SKILLS_INDEX.md), with routing in [`framework/DECISION_MAP.md`](../framework/DECISION_MAP.md).

## 2. The laziest thing that works

Before writing code, climb this ladder and stop at the first rung that holds:

1. Does it need to exist at all? (YAGNI)
2. Is it already in this codebase?
3. Does the standard library do it?
4. Does a native platform feature cover it?
5. Does an installed dependency solve it?
6. **Does a public repo or package already do it? Search GitHub first** and report repo, stars, license and fit. If something covers ~80%, adopt or adapt it.
7. Can it be one line?
8. Only then: the minimum code that works.

Never simplify away validation, error handling, security, accessibility, or anything explicitly asked for. New non-trivial logic leaves one small runnable check behind.

This is enforced by the [Ponytail](https://github.com/DietrichGebert/ponytail) plugin plus the rules in [`framework/harness/CLAUDE.global.md`](../framework/harness/CLAUDE.global.md).

## 3. Working habits

- **No errors twice.** Every error is recorded as *what / cause / fix / prevention* in the vault.
- **No asking twice.** A preference you state twice becomes a stored default.
- **Search before building**, root-cause before fixing: no fix without a root cause.
- **Blast radius first.** Before an edit, list everything that reads, calls or imports what you touch.
- **Surgical changes.** Touch only what the task needs; match the surrounding style.
- **Autonomy is opt-in.** Nothing schedules itself. You may set up any number of automations, each listed in a registry, all stopped by one `HALT` file. → [safety](safety.md)
- **Apps that work every time.** Own your runtime, pin dependencies, use an idempotent launcher, fail closed. → [`RELIABILITY_STANDARD.md`](../framework/RELIABILITY_STANDARD.md)
- **No auth walls on your own work-in-progress.** Add login gates only when real users arrive.
- **GitHub-first.** Work on a branch, show it, merge on a yes.
- **Every finished artifact lands in a review inbox** so you see it without asking. → [memory](memory.md#review-queue)

## 4. Pick the model by the job

There is no default model. Every dispatch names a kind of job (a route), and one table, `lanes.json`, says which model and effort that job gets. For big work the routes chain: plan in your own session, an adversarial plan review by a model from another lab, build, a cross-lab review of the diff, then a verify stage before you call it done. Details: [agents & lanes](agents-and-lanes.md#big-work-stage-by-stage).
