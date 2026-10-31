---
name: cliproxyapi
description: Wrench inside the `router` mechanic. CLIProxyAPI on thor — the user's Claude (and optionally Codex/Gemini/Grok) subscriptions exposed as an OpenAI-compatible endpoint at thor:8317. Use when a tool needs Claude via a base_url + api_key instead of the Claude Code CLI.
---

# cliproxyapi — subscription CLIs as an OpenAI-compatible API

**Live since 2026-09-10 on thor.** `router-for-me/CLIProxyAPI`, MIT. `systemd --user` unit `cliproxyapi.service`, linger enabled, survives reboot.

- **Base URL:** `http://thor:8317/v1` (tailnet-bound; not `0.0.0.0`)
- **Key:** `~/.cliproxy-client.key` on thor. Read it inside a remote shell; never print it.
- **Models:** 16 Claude ids including `claude-sonnet-4-6`, `claude-opus-5`, `claude-haiku-4-5-20251001`.
- **Config:** `~/cliproxyapi/config.yaml` (0600). Credentials: `~/.cli-proxy-api/`.

## When to use
A tool that wants `base_url` + `api_key` and should run on the user's subscription rather than a metered key. Sits alongside [[freellmapi]] (free tiers, `127.0.0.1:3001` on odin and `thor:3001` on thor). Prefer this for quality work; prefer freellmapi for bulk/low-stakes.

## Adding another subscription
```
cd ~/cliproxyapi
./cli-proxy-api -config config.yaml -codex-device-login -no-browser   # headless, prints a device code
./cli-proxy-api -config config.yaml -claude-login -no-browser         # needs the tunnel below
```
Claude's OAuth callback lands on **port 54545**, so from a machine with a browser:
```
ssh -f -N -L 54545:127.0.0.1:54545 you@thor
```
then open the printed URL there.


## Caveat
Proxying subscription tokens outside the official client is a grey area under OpenAI's and Anthropic's terms. Own machine, own data, own subscription, no resale — the user's call, made knowingly 2026-09-10.
