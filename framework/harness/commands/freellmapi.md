---
description: "FreeLLMAPI router on thor: free-tier model dispatch through the lane CLI, port 3001"
---

The router runs on thor (Docker, `127.0.0.1:3001`, shared to the tailnet with `tailscale serve --tcp 3001`); loki may run its own key-gated instance for Hermes. Endpoints:
- Dashboard: `http://thor:3001/` (`http://127.0.0.1:3001/` on thor itself)
- OpenAI-compatible base URL: `http://thor:3001/v1`
- Chat endpoint: `http://thor:3001/v1/chat/completions`

Do not treat this as Claude Code's primary brain. Claude Code's model gateway expects Anthropic-compatible `/v1/messages`; FreeLLMAPI exposes OpenAI-compatible `/v1/chat/completions`. Use it for side calls. The routing rules (when to fire, data tiers, admission) live in the `router` mechanic's `freellmapi` wrench: `_system/skills/router/wrenches/freellmapi.md`.

## Use it through `lane`

```bash
lane doctor                                   # CLI and router reachable
lane admission                                # which task classes may use free models
lane <class> --tier public|business|business-pii|private
```

Never a bare model id (any provider could serve it). Private, vault, handoff and client-PII work uses `--tier private` or `--tier business-pii`, which stays on `auto:local` (thor's local models, never cloud).

Model names for a direct call: `auto`, `auto:fast`, `auto:smart`, `auto:local`.

```bash
curl -s http://thor:3001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $FREELLMAPI_KEY" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Summarize this idea in 5 bullets."}]}'
```

`$FREELLMAPI_KEY` is the unified key from the dashboard's header: never paste it into chat or a shell literal.

## Health check

`lane doctor`, or `curl -s http://thor:3001/api/ping` (expect `{"status":"ok"}`; `/v1/models` needs the key and answers 401 without it). If it does not answer, run `thor-up` first (thor sleeps when idle), then `docker compose ps` in `~/freellmapi-docker` on thor.

## Provider setup

Add free-tier and keyless providers on the dashboard's Keys page (or with `freellm-keydrop`, see the wrench). Skip any provider that needs a payment method. A Google key also serves the Gemini CLI lane (`_system/skills/router/wrenches/gemini.md`). OpenRouter: $0 free models only.

Keep every instance single-user and loopback-only (the tailnet share and loki's listener are key-gated). Do not paste provider keys or the unified key into chat.
