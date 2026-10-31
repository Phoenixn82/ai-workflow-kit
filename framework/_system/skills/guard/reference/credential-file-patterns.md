# Credential File Patterns

Guard has no runtime shell-read hook today. This file is the shared pattern set for future `careful` / `freeze` shell-read parity work and for manual synthetic filename checks.

## Read-deny pattern set

- `**/.credentials.json`
- `~/.claude/.credentials.json`
- `$HOME/.claude/.credentials.json`
- `**/token.json`
- `**/tokens.json`
- `**/*apikey*.json`
- `**/service-account*.json`

## Current runtime status

Claude's harness-level `settings.json` can deny `Read(...)` calls for these paths. Guard currently has only destructive-command and edit-boundary hooks, so shell lanes such as `Get-Content`, `rg`, `type`, or `cat` are not blocked by guard at runtime.
