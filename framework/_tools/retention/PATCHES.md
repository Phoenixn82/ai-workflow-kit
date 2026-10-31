# Plugin-cache patches (audit 2026-10-12 H4/H5)

Claude Code plugins live in `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`. A plugin update
unpacks a fresh version dir, so every local edit below is silently lost on the next update. This file is
the record; the scripts re-apply them idempotently.

## 1. Ponytail 4.10.0: SubagentStart hook removed (was undocumented)

- File: `~/.claude/plugins/cache/ponytail/ponytail/4.10.0/hooks/claude-codex-hooks.json`
  (the file `.claude-plugin/plugin.json` names in `"hooks"`)
- When: 2026-09-26, right after the 4.10.0 install. No vault note existed.
- Why: CLAUDE.md "Audience boundary": Ponytail is for user-facing sessions only; upstream injects
  `ponytail-subagent.js` into every subagent.
- Exact diff (upstream marketplace clone at e3ba2aa6 vs live cache):

```diff
--- marketplaces/ponytail/hooks/claude-codex-hooks.json
+++ cache/ponytail/ponytail/4.10.0/hooks/claude-codex-hooks.json
@@ -13,18 +13,6 @@
         ]
       }
     ],
-    "SubagentStart": [
-      {
-        "hooks": [
-          {
-            "type": "command",
-            "command": "node \"${CLAUDE_PLUGIN_ROOT}/hooks/ponytail-subagent.js\"",
-            "timeout": 5,
-            "statusMessage": "Loading ponytail mode..."
-          }
-        ]
-      }
-    ],
     "UserPromptSubmit": [
       {
         "hooks": [
```

- Re-apply: `python3 $HOME/AI_Projects/_tools/retention/repatch.py [--dry-run]` (every cached ponytail
  version; keeps a one-time `.bak`; prints `ok …` when already patched; exits 1 and names the file if the
  plugin moved its hooks file, so a new layout is never guessed at).
- Check: `diff -u ~/.claude/plugins/marketplaces/ponytail/hooks/claude-codex-hooks.json ~/.claude/plugins/cache/ponytail/ponytail/*/hooks/claude-codex-hooks.json`
  shows only the hunk above. `repatch.py --self-test` also patches a copy of the upstream file and asserts it
  is byte-identical to the live one.

## 2. chrome-devtools-mcp 1.9.0: `--executablePath=/usr/bin/chromium`

- Files: `…/claude-plugins-official/chrome-devtools-mcp/1.9.0/.claude-plugin/plugin.json` (2026-09-14) and
  `…/1.9.0/mcp.json` (2026-09-15). odin has Chromium, no Google Chrome.
- Documented already: harness memory `chrome-devtools-chromium-path`, `Projects/workflow/errors.md`.
- Exact diff:

```diff
--- .claude-plugin/plugin.json.bak
+++ .claude-plugin/plugin.json
@@ -6,7 +6,8 @@
     "chrome-devtools": {
       "command": "npx",
       "args": [
-        "chrome-devtools-mcp@1.9.0"
+        "chrome-devtools-mcp@1.9.0",
+        "--executablePath=/usr/bin/chromium"
       ]
     }
   }
--- mcp.json.bak
+++ mcp.json
@@ -7,7 +7,8 @@
       "args": [
         "--prefix",
         "${PLUGIN_DATA}",
-        "chrome-devtools-mcp@1.9.0"
+        "chrome-devtools-mcp@1.9.0",
+        "--executablePath=/usr/bin/chromium"
       ]
     }
   }
```

- Re-apply: `~/.local/bin/chrome-devtools-repatch` (idempotent; it has no dry-run and ignores argv, so
  `--help` applies too). Then `/mcp` reconnect or a new session.
- The two `.bak` files are themselves swept into the archive by `retain.py` after 14 days; this diff is the record.

## How the patches survive an update

1. **Weekly**: `retain.timer` (Sundays 05:30; not installed or enabled as of 2026-10-12, see README) runs `repatch.py` and
   `chrome-devtools-repatch` after the archive pass, so drift lasts at most a week.
2. **Every session start (proposed, not applied)**: `settings.json.proposed` adds a `SessionStart`
   (`startup`) hook running both, stdout silenced (SessionStart stdout would land in context), ~60 ms.
   A plugin update happens at session start, so the repaired file takes effect from the next session.
   Apply (backup first; `~/.claude` is also git):

```bash
cp -p ~/.claude/settings.json ~/.claude/settings.json.bak-20261012-H4
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".claude/settings.json"
d = json.loads(p.read_text())
frag = json.loads(pathlib.Path("$HOME/AI_Projects/_tools/retention/settings.json.proposed").read_text())
ss = d.setdefault("hooks", {}).setdefault("SessionStart", [])
ss += [g for g in frag["hooks"]["SessionStart"] if g not in ss]
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
PY
```

3. **By hand** after any `claude plugin update` / marketplace refresh: run both, then start a new session.
