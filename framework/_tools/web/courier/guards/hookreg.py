"""Add or remove the webcred guard's PreToolUse entry in a Claude Code settings.json or a Codex hooks.json.

    python3 hookreg.py add|remove <json file> <entry template> <hook script path> <backup stamp>

Idempotent: `add` does nothing when an entry already runs webcred-guard.sh, `remove` does nothing when none does.
A file it changes is backed up first (<file>.bak-webcred-<stamp>) and replaced atomically with its mode kept. A file
that isn't valid JSON is left alone (exit 1). Every other key and entry is kept as it was."""
import json
import os
import shutil
import sys
import tempfile

MARK = "webcred-guard.sh"


def ours(entry) -> bool:
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return isinstance(hooks, list) and any(isinstance(h, dict) and MARK in str(h.get("command", "")) for h in hooks)


def without_guard(data: dict) -> dict:
    """data without the guard's PreToolUse entries; a PreToolUse list (and hooks table) left empty goes too, as it
    was before the install added it."""
    pre = data.get("hooks", {}).get("PreToolUse", [])
    rest = [e for e in pre if not ours(e)]
    out = {**data, "hooks": {**data.get("hooks", {}), "PreToolUse": rest}}
    if not rest:
        del out["hooks"]["PreToolUse"]
        if not out["hooks"]:
            del out["hooks"]
    return out


def write(path: str, data: dict, stamp: str) -> None:
    mode = 0o600
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
        shutil.copy2(path, f"{path}.bak-webcred-{stamp}")
        print(f"backup:    {path}.bak-webcred-{stamp}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".hookreg-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def load(path: str) -> dict | None:
    """The file's JSON ({} if it isn't there), or None (with a WARNING) when it isn't a shape this can edit."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except ValueError:
        print(f"WARNING:   {path} is not valid JSON; not touching it", file=sys.stderr)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        print(f"WARNING:   {path} has an unexpected shape; not touching it", file=sys.stderr)
        return None
    if not isinstance(data.get("hooks", {}).get("PreToolUse", []), list):
        print(f"WARNING:   {path}: hooks.PreToolUse is not a list; not touching it", file=sys.stderr)
        return None
    return data


def main(op: str, path: str, template: str, hook: str, stamp: str) -> int:
    data = load(path)
    if data is None:
        return 1
    pre = data.get("hooks", {}).get("PreToolUse", [])
    present = any(ours(e) for e in pre)
    if op == "add":
        if present:
            print(f"unchanged: {path} (guard registered)")
            return 0
        with open(template) as f:
            entry = json.loads(f.read().replace("__HOOK__", hook))
        data.setdefault("hooks", {}).setdefault("PreToolUse", []).append(entry)
        write(path, data, stamp)
        print(f"registered: {path}")
        return 0
    if not present:
        print(f"unchanged: {path} (no guard entry)")
        return 0
    write(path, without_guard(data), stamp)
    print(f"removed:   guard entry from {path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] not in ("add", "remove"):
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
