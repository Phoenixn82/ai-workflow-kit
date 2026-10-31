# `vault-sync` hook draft — T2-WO1

This file is a draft only. T7 owns the three live configuration files; do not
copy these snippets automatically. Commands use the installed worker and obey
the requested 10-second pull and 90-second push budgets.

## Claude Code — merge into `~/.claude/settings.json`

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "startup|resume|clear",
      "hooks": [{"type": "command", "command": "$HOME/.local/bin/vault-sync pull", "timeout": 10}]
    }],
    "SessionEnd": [{
      "hooks": [{"type": "command", "command": "$HOME/.local/bin/vault-sync push", "timeout": 90}]
    }]
  }
}
```

## Codex CLI — merge into `~/.codex/hooks.json`

Codex documents both `SessionStart` and `SessionEnd`, but its `SessionEnd`
timeout is capped at three seconds. The documented `Stop` event is the only
end/stop lifecycle event that can carry the requested 90-second push budget;
it runs when a turn stops, rather than only when the main thread exits.

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "startup|resume|clear",
      "hooks": [{"type": "command", "command": "$HOME/.local/bin/vault-sync pull", "timeout": 10}]
    }],
    "Stop": [{
      "hooks": [{"type": "command", "command": "$HOME/.local/bin/vault-sync push", "timeout": 90}]
    }]
  }
}
```

Verified against OpenAI's [Codex Hooks documentation](https://developers.openai.com/codex/hooks), which lists `SessionStart`, `SessionEnd`, and `Stop`, documents the nested `hooks` JSON shape, and states that `SessionEnd` supports at most three seconds.

## Gemini CLI — draft for `~/.gemini/hooks.json`

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "startup|resume|clear",
      "hooks": [{"type": "command", "command": "$HOME/.local/bin/vault-sync pull", "timeout": 10000}]
    }],
    "SessionEnd": [{
      "matcher": "exit|clear",
      "hooks": [{"type": "command", "command": "$HOME/.local/bin/vault-sync push", "timeout": 90000}]
    }]
  }
}
```

Gemini's current official CLI documentation calls these lifecycle events
`SessionStart` and `SessionEnd`, with the nested `hooks` object shown above and
timeouts in milliseconds. It documents user configuration in
`~/.gemini/settings.json`; if the installed Gemini version does not load
`~/.gemini/hooks.json`, merge this exact `hooks` object into that documented
settings file instead. Source: [Gemini CLI hooks](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/index.md) and its [hooks reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md).
