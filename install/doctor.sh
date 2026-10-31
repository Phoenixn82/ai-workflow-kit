#!/usr/bin/env bash
# doctor.sh [--profile <a[,b]>] [--os arch|debian|macos]
# One line per tool: ok | missing | wrong-version (skip = not for this OS), then the profile's services (ok | down)
# and units (on | off | not-installed) with their kill switches. Writes nothing.
# Exit 1 when a required tool is missing or a required service is down; without --profile it only reports.
set -uo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
if ! { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; }; then
  echo "missing       python3                   Python 3.10+ (the doctor and the installer need it first)"
  exit 1
fi
exec python3 "$here/lib.py" doctor "$@"
