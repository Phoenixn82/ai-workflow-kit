---
description: Prepare the Claude-driven open-loops workflow.
---

Before each phase, Claude's main loop must read `~/.local/state/omarchy/agents/usage/claude.json`
and stop if the five-hour window is past 40%.

Set `ROOT` to the session scratchpad, then run:
```bash
export OPEN_LOOPS_DIR=$ROOT
python3 $HOME/AI_Projects/_tools/open-loops/prep.py condense --days 14 --out $ROOT
python3 $HOME/AI_Projects/_tools/open-loops/prep.py batches --out $ROOT
```

Tell the main loop to run the next single Workflow phase described in
`$HOME/AI_Projects/_tools/open-loops/README.md`.
