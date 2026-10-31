#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")/.." || exit 0
# SubagentStart: inject the parent project's pre-built brief so the subagent
# does not sweep the vault to orient. Inject only - no seal, no worker.
exec python3 -m handoff.cli subagent-start
