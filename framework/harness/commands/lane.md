---
description: "Route admitted bounded text work through the lane CLI"
---

Use the `lane` CLI for bounded, checkable text work.

1. Check readiness: `lane doctor`
2. Check admitted classes: `lane admission`
3. Run an admitted class: `lane <class> --tier public|business|business-pii|private`

Classes from `_tools/lanes/config/classes.yaml`: `triage`, `label-text`, `sentinel`, `extract-json`, `classify`, `code-small`, `sql`, `regex`, `systemd-calendar`, `commit-msg`, `log-triage`, `summary`, `brief`, `digest`, `copy-public`, `translate-sms`, `lead-extract`.

Use a free lane only when `lane admission` admits the class. Otherwise route the work to Claude or Codex. Private or PII work uses `--tier private` or `--tier business-pii` and stays on `auto:local`. Never use a bare model ID or print provider keys.
