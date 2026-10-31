---
name: end-session
description: Optional deep pass inside the second-brain mechanic. The user invokes it through `/handoff` to review automatic handoff-v2 output, harvest Codex findings, review lessons, and write only approved durable corrections.
---

# end-session

handoff-v2 automatically seals and distills session segments into the project records, `RESUME.md`, and `_brief.md`. This wrench is the optional deep pass the user invokes through `/handoff`; do not create handoff files or session prompts.

## Contract

1. From `$HOME/AI_Projects/pipeline/handoff-v2`, run:

   ```bash
   python3 -m handoff.cli status
   ```

   If the queue is nonzero, run `python3 -m handoff.cli drain` and recheck status. Repeat until status reports zero queued segments. If status or drain fails, or the queue cannot reach zero, stop and report the failure.

2. Harvest Codex spawn findings:

   ```bash
   python3 $HOME/AI_Projects/_system/skills/second-brain/scripts/harvest_spawn_findings.py --project $HOME/AI_Projects
   ```

   `NO_PENDING` means there is nothing to include. If the harvester returns a digest, retain only missed durable facts or corrections from above `HARVEST_KEYS`; discard retired prompt material and other out-of-contract headings. Present retained items as a clearly labeled Codex group. On an error, report the failure and do not fabricate findings. After the user approves the group and its facts are written, use the harvester's `--mark-consumed --digest <path>` interface; leave rejected findings unconsumed.

3. Review candidates with:

   ```bash
   python3 -m handoff.cli lessons
   ```

   Promote only candidate lessons the user approves, and mark rejected candidates accordingly.

4. Show a short draft of durable facts or corrections handoff-v2 missed or got wrong. Write only approved content as dated, idempotent entries in its existing project target. `RESUME.md` and `_brief.md` remain pipeline-owned.

5. If warranted, add a concise note to `$HOME/AI_Projects/_system/second-brain/Blog/_QUEUE.md` after the user approves it.

6. For a user-approved workflow-kit dry run, use:

   ```bash
   AI_PROJECTS_ROOT=$HOME/AI_Projects python3 $HOME/AI_Projects/_system/automations/workdirs/ai-workflow-kit/tools/sync-kit.py
   ```

   Add `--apply` only after the user approves the sync. `--commit` or `--push` also require the user's explicit approval and the public-push guard gate.

7. Give the user a one-line confirmation.

## Discard

Do not save raw code, routine tool calls, trivia, or errors without a verified fix.
