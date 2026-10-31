"""Hook wiring with a migration assertion.

v1 must not be enabled while ai-memory's handoff/injection or the standalone
vault-sync handlers are still live: two systems would inject at SessionStart and
two would race at SessionEnd (PLAN.md N).

Default is a dry run. `--apply` writes, after backing up settings.json.
"""
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

from . import config as C

SETTINGS = Path.home() / ".claude" / "settings.json"
BIN = C.ROOT / "bin"

DESIRED = {
    "SessionStart": BIN / "handoff-start.sh",
    "SessionEnd": BIN / "handoff-end.sh",
    "PreCompact": BIN / "handoff-precompact.sh",
}

CONFLICTS = [
    ("ai-memory session-start", "ai-memory", "session-start"),
    ("ai-memory session-end", "ai-memory", "session-end"),
    ("ai-memory pre-compact", "ai-memory", "pre-compact"),
    ("standalone vault-sync pull", "vault-sync", "pull"),
    ("standalone vault-sync push", "vault-sync", "push"),
]


def _commands(settings: dict) -> list[tuple[str, str]]:
    out = []
    for event, matchers in (settings.get("hooks") or {}).items():
        for m in matchers:
            for h in m.get("hooks", []):
                out.append((event, h.get("command", "")))
    return out


def audit() -> dict:
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    cmds = _commands(settings)
    blocking = [name for name, a, b in CONFLICTS
                if any(a in c and b in c for _e, c in cmds)]
    installed = [e for e, path in DESIRED.items()
                 if any(str(path) in c for ev, c in cmds if ev == e)]
    return {"settings": settings, "commands": cmds,
            "blocking": blocking, "installed": installed}


def report() -> int:
    a = audit()
    print(f"settings: {SETTINGS}")
    print(f"handoff-v2 hooks installed: {a['installed'] or 'none'}")
    if a["blocking"]:
        print("\nMIGRATION BLOCKED - these must be removed first:")
        for b in a["blocking"]:
            print(f"  - {b}")
        print("\n  ai-memory's handoff/injection would compete with the brief at "
              "SessionStart,\n  and standalone vault-sync would race the worker at "
              "SessionEnd.")
    else:
        print("\nmigration assertion: PASS (no competing handlers)")
    print("\nwould wire:")
    for event, path in DESIRED.items():
        print(f"  {event:14s} -> {path}")
    return 1 if a["blocking"] else 0


def apply() -> int:
    a = audit()
    if a["blocking"]:
        print("refusing to install while competing handlers are live:")
        for b in a["blocking"]:
            print(f"  - {b}")
        return 1
    settings = a["settings"]
    backup = SETTINGS.with_suffix(f".json.bak-handoff-{dt.datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(SETTINGS, backup)
    hooks = settings.setdefault("hooks", {})
    for event, path in DESIRED.items():
        entries = hooks.setdefault(event, [])
        cmd = f'bash "{path}"'
        if any(str(path) in h.get("command", "")
               for m in entries for h in m.get("hooks", [])):
            continue
        entries.append({"matcher": "", "hooks": [
            {"type": "command", "command": cmd, "timeout": 15}]})
    SETTINGS.write_text(json.dumps(settings, indent=2))
    print(f"installed. backup: {backup}")
    return 0


def main(argv: list[str]) -> int:
    return apply() if "--apply" in argv else report()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
