#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" != --self-test ]] && ! command -v hermes >/dev/null; then
  echo 'Install Hermes first: curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --non-interactive --skip-browser --skip-computer-use'
  exit 3
fi
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if python3 -c 'import yaml' 2>/dev/null; then
  exec python3 "$here/install.py" "$@"
fi
exec uv run --with pyyaml python "$here/install.py" "$@"
