#!/usr/bin/env python3
"""apply_lanes.py: push _system/lanes.json (the one source of truth for model + effort per lane) into the
places that read it: ~/.claude/settings.json (main), ~/.codex/config.toml (interactive).
Subagents get no env default (env CLAUDE_CODE_SUBAGENT_MODEL is removed, the user 2026-10-13): every Agent / Workflow
agent() call names its model from `route`, and ~/.claude/hooks/route-guard.py denies one that does not.
`cx` reads lanes.json itself. Prints every change; --check only reports drift (exit 1 when drifted).
/model flips and hand edits can put different models in several files; this makes them one."""
import json, os, re, sys

from route import lanes_path  # route.py sits beside this file
LANES = lanes_path()  # $ROUTE_LANES, then $KIT_HOME/framework/_system/lanes.json, then ~/AI_Projects/_system/lanes.json
SETTINGS = os.path.expanduser("~/.claude/settings.json")
CODEX = os.path.expanduser("~/.codex/config.toml")


def load():
    if not os.path.isfile(LANES):
        sys.exit(f"apply_lanes: no lanes.json at {LANES} (set ROUTE_LANES to one, or KIT_HOME to the kit clone)")
    return json.load(open(LANES))["lanes"]


def settings_changes(lanes):
    d = json.load(open(SETTINGS))
    main = lanes["claude.main"]
    want = {("model",): main["model"], ("effortLevel",): main["effort"],
            ("modelSettings", main["model"], "effortLevel"): main["effort"]}
    changes = []
    if "CLAUDE_CODE_SUBAGENT_MODEL" in d.get("env", {}):
        changes.append(("env.CLAUDE_CODE_SUBAGENT_MODEL", d["env"].pop("CLAUDE_CODE_SUBAGENT_MODEL"), None))
    for path, val in want.items():
        node = d
        for k in path[:-1]:
            node = node.setdefault(k, {})
        if node.get(path[-1]) != val:
            changes.append((".".join(path), node.get(path[-1]), val))
            node[path[-1]] = val
    return d, changes


def codex_changes(lanes):
    s = open(CODEX).read()
    lane = lanes["codex.interactive"]
    changes = []
    for key, val in (("model", lane["model"]), ("model_reasoning_effort", lane["effort"])):
        m = re.search(rf'^{key}\s*=\s*"([^"]*)"', s, re.M)
        if m and m.group(1) != val:
            changes.append((f"codex.{key}", m.group(1), val))
            s = s[:m.start(1)] + val + s[m.end(1):]
        elif not m:
            changes.append((f"codex.{key}", None, val))
            s = f'{key} = "{val}"\n' + s
    return s, changes


def main(argv):
    if argv == ["--self-test"]:
        lanes = load()
        assert lanes["claude.main"]["model"].startswith("claude-") and lanes["codex.build"]["effort"] != "max"
        print("self-test passed"); return 0
    check = "--check" in argv
    lanes = load()
    d, sc = settings_changes(lanes)
    s, cc = codex_changes(lanes)
    for name, old, new in sc + cc:
        print(f"{'DRIFT' if check else 'SET'} {name}: {old!r} -> {new!r}")
    if check:
        return 1 if sc or cc else 0
    if sc:
        json.dump(d, open(SETTINGS, "w"), indent=2); open(SETTINGS, "a").write("\n")
    if cc:
        open(CODEX, "w").write(s)
    print("applied" if sc or cc else "no change")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
