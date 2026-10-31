#!/usr/bin/env python3
"""Re-apply the local Ponytail plugin-cache patch (PATCHES.md): drop the SubagentStart hook that injects
ponytail mode into every subagent (Ponytail is for user-facing sessions only). Idempotent; only touches
the hooks file each cached ponytail version's .claude-plugin/plugin.json names. Takes effect next session.
usage: repatch.py [--dry-run] | --self-test     (chrome-devtools has its own: ~/.local/bin/chrome-devtools-repatch)
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path


def cache_root():
    return Path.home() / ".claude/plugins/cache/ponytail/ponytail"


def ours(group):
    return any("ponytail-subagent.js" in str(h.get("command", "")) for h in group.get("hooks", []))


def patch(path, dry):
    data = json.loads(path.read_text())
    hooks = data.get("hooks", {})
    groups = hooks.get("SubagentStart", [])
    keep = [g for g in groups if not ours(g)]
    if len(keep) == len(groups):
        return f"ok {path} (no ponytail SubagentStart hook)"
    if dry:
        return f"would patch {path} (remove ponytail SubagentStart hook)"
    backup = path.with_name(path.name + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    if keep:
        hooks["SubagentStart"] = keep
    else:
        del hooks["SubagentStart"]
    # ponytail: indent=2 + newline reproduces upstream's formatting byte-for-byte (checked on 4.10.0)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return f"patched {path} (removed ponytail SubagentStart hook)"


def main(dry):
    root = cache_root()
    if not root.is_dir():
        print(f"repatch: ponytail not installed ({root} missing), nothing to do")
        return 0
    rc = 0
    for version in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            manifest = json.loads((version / ".claude-plugin/plugin.json").read_text())
            rel = manifest.get("hooks", "./hooks/hooks.json")
            if not isinstance(rel, str):
                raise ValueError(f"inline hooks in plugin.json ({type(rel).__name__}); patch by hand")
            print(patch(version / rel, dry))
        except (OSError, ValueError) as e:  # the patch target moved: say so loudly, never guess
            rc = 1
            print(f"repatch: {version}: {e}", file=sys.stderr)
    return rc


UPSTREAM = {"hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "node \"${CLAUDE_PLUGIN_ROOT}/hooks/ponytail-activate.js\""}]}],
    "SubagentStart": [{"hooks": [{"type": "command", "command": "node \"${CLAUDE_PLUGIN_ROOT}/hooks/ponytail-subagent.js\"",
                                  "timeout": 5, "statusMessage": "Loading ponytail mode..."}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "node \"${CLAUDE_PLUGIN_ROOT}/hooks/ponytail-mode-tracker.js\""}]}]}}


def self_test():
    with tempfile.TemporaryDirectory() as h:
        v = Path(h, ".claude/plugins/cache/ponytail/ponytail/9.9.9")
        (v / ".claude-plugin").mkdir(parents=True)
        (v / "hooks").mkdir()
        (v / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "ponytail", "hooks": "./hooks/claude-codex-hooks.json"}))
        f = v / "hooks/claude-codex-hooks.json"
        original = json.dumps(UPSTREAM, indent=2) + "\n"
        f.write_text(original)
        run = lambda *a: subprocess.run([sys.executable, __file__, *a], capture_output=True, text=True, env={**os.environ, "HOME": h})
        r = run("--dry-run")
        assert r.returncode == 0 and "would patch" in r.stdout and f.read_text() == original, r
        r = run()
        data = json.loads(f.read_text())
        assert r.returncode == 0 and "patched" in r.stdout, r
        assert "SubagentStart" not in data["hooks"] and set(data["hooks"]) == {"SessionStart", "UserPromptSubmit"}, data
        expected = dict(UPSTREAM["hooks"])
        del expected["SubagentStart"]
        assert f.read_text() == json.dumps({"hooks": expected}, indent=2) + "\n", "formatting drifted"
        assert Path(str(f) + ".bak").read_text() == original
        before = f.stat().st_mtime_ns
        r = run()
        assert r.returncode == 0 and r.stdout.startswith("ok ") and f.stat().st_mtime_ns == before, "not idempotent"
        (v / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "ponytail", "hooks": "./hooks/renamed.json"}))
        r = run()
        assert r.returncode == 1 and "renamed.json" in r.stderr, "moved target must fail loudly"
        r = subprocess.run([sys.executable, __file__], capture_output=True, text=True, env={**os.environ, "HOME": h + "/none"})
        assert r.returncode == 0 and "not installed" in r.stdout
    up = Path.home() / ".claude/plugins/marketplaces/ponytail/hooks/claude-codex-hooks.json"
    live = cache_root() / "4.10.0/hooks/claude-codex-hooks.json"
    if up.is_file() and live.is_file():  # real upstream file, patched in a temp copy, must equal the live patched file
        with tempfile.TemporaryDirectory() as t:
            c = Path(t, "h.json")
            shutil.copy2(up, c)
            patch(c, dry=False)
            assert c.read_bytes() == live.read_bytes(), "patching upstream 4.10.0 does not reproduce the live cache file"
    print("repatch.py self-test: ok (dry-run unchanged, patch, byte format, .bak, idempotent, moved target fails, absent plugin ok)")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(main("--dry-run" in sys.argv))
