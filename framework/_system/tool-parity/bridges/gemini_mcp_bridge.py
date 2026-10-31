#!/usr/bin/env python3
"""Launch Claude's configured Gemini MCP for Codex without duplicating secrets."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    config_path = Path.home() / ".claude" / "mcp.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        config = data["mcpServers"]["gemini"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"gemini bridge: could not read Claude MCP config: {exc}", file=sys.stderr)
        return 2

    command = [config.get("command", "npx"), *config.get("args", [])]
    command[0] = shutil.which(command[0]) or command[0]
    env = os.environ.copy()
    env.update({key: str(value) for key, value in config.get("env", {}).items()})

    return subprocess.call(command, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
