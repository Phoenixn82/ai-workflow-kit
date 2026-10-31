#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")/.." || exit 0
# PreCompact: seal + enqueue ONLY. No worker spawn.
exec python3 -m handoff.cli seal pre-compact
