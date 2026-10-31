---
description: Optional deep pass over automatic handoff-v2 distillation
argument-hint: "focus or emphasis for this handoff (optional)"
---

`/handoff` is the user's optional deep pass. handoff-v2 already seals and distills the session automatically; do not create handoff files or session prompts.

## Procedure

1. Check the pipeline from `$HOME/AI_Projects/pipeline/handoff-v2`:

   ```bash
   python3 -m handoff.cli status
   ```

   If the queue is nonzero, run `python3 -m handoff.cli drain` and recheck status. Repeat until status reports zero queued segments. If status or drain fails, or the queue cannot reach zero, stop and report the failure.

2. Harvest Codex spawn findings before preparing the draft:

   ```bash
   python3 $HOME/AI_Projects/_system/skills/second-brain/scripts/harvest_spawn_findings.py --project $HOME/AI_Projects
   ```

   `NO_PENDING` needs no action. For a digest, keep only missed durable facts or corrections from above its `HARVEST_KEYS` marker; discard retired prompt material and other out-of-contract headings. Present the retained items as Codex spawn findings. Surface errors; do not invent findings. After the user approves and the related facts are written, retire approved findings with the harvester's `--mark-consumed --digest <path>` interface. Leave rejected findings unconsumed.

3. Review lesson candidates:

   ```bash
   python3 -m handoff.cli lessons
   ```

   Promote only the candidates the user approves; mark rejected candidates accordingly.

4. Show the user a short draft of only missed durable facts or corrections. Write only the approved items, using the existing project targets and dated, idempotent entries.

5. Add a brief note to `$HOME/AI_Projects/_system/second-brain/Blog/_QUEUE.md` if the session produced a warranted future post idea and the user approves it.

6. For a user-approved workflow-kit dry run, use:

   ```bash
   AI_PROJECTS_ROOT=$HOME/AI_Projects python3 $HOME/AI_Projects/_system/automations/workdirs/ai-workflow-kit/tools/sync-kit.py
   ```

   Add `--apply` only after the user approves the sync. `--commit` or `--push` also require the user's explicit approval and the public-push guard gate.

7. Confirm the completed deep pass in one line.

`$ARGUMENTS`, if provided, steers the review.
