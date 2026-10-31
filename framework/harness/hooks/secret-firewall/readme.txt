SECRET FIREWALL  (installed by Claude, 2026-05-30)
==================================================

Purpose: stop any AI running in Claude Code from reading your API keys /
secrets, and stop secrets from leaking into commits.

Files in this folder:
  block-secrets.py   PreToolUse hook. Blocks Read/Edit/Write/Grep/Glob/Bash
                     calls that try to read a secret file, dump environment
                     variables, or decrypt the .secrets store. This is the
                     real guarantee (it runs BEFORE the tool).
  detect-secrets.py  PostToolUse tripwire. If a secret value slips into tool
                     output, it warns the model not to propagate it. (It
                     cannot un-show it -- Claude Code hooks can't rewrite tool
                     output -- so this is defense-in-depth only.)

What counts as "protected":
  - any .env / *.env file (except .env.example/.sample/.template)
  - provider-keys*.json  (e.g. the FreeLLMAPI vault)
  - ~/.secrets/**  (the DPAPI store)
  - ~/.codex/auth.json, ~/.aws/credentials, ~/.netrc, ~/.npmrc
  - SSH private keys, *.pem / *.pfx / *.p12
  - environment-variable dumps (Get-ChildItem Env:, printenv, $env:*KEY*, ...)
  - Telegram account logins: *.session (+ -journal/-wal/-shm) under a telegram path; everything in Telegram Desktop's tdata (bare names after cd too); paths passed to sqlite3.connect() in inline code; SQLite file: URIs (file:PATH?query, file:///, file://localhost/, %XX, %00) count as the local path for sqlite3/interpreter operands and connect(); only a literal other host is remote

Registered in: ~/.claude/settings.json  (permissions.deny + hooks).

To TEMPORARILY disable (e.g. you want Claude to help edit an .env):
  comment out the PreToolUse entry in ~/.claude/settings.json, or rename this
  folder. Re-enable when done.

Fail-safe: block-secrets.py fails CLOSED: bad input, an internal exception or
even a SyntaxError exits non-zero and the call is denied (see the corpus README
in _tools/secret-firewall-corpus). The dispatcher also denies when a safety hook
crashes, times out or is missing.

Editing: every save of block-secrets.py is live for every session at once. Edit a copy,
run the gates on it (--self-test, test-block-secrets.py, the corpus run.py --hook COPY),
then mv it over the live file. An in-place edit that calls a function before defining it
denies every tool call in every session (NameError).
Tests set HOOK_EVENT_LOG (devnull or a temp file) so they never land in the live log.
