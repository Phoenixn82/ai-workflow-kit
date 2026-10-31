---
type: reference
---
# Hardware / fleet

| Name (MagicDNS) | Role | Machine | OS | Reach |
|---|---|---|---|---|
| workstation | daily driver, runs the agents; no local models | (your laptop) | | local |
| workhorse | GPU box: local models, free-model router, backups target | | | `ssh workhorse` |
| heimdall | always-on: timers, scrapes, watchdog, DNS | old laptop | Debian | `ssh heimdall` |
| mediabox | Jellyfin + requests, one-way (tag:quarantine) | old laptop | Debian | `ssh mediabox` |
