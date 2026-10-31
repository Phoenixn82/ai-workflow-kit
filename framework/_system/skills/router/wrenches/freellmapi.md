---
name: freellmapi
description: Wrench inside the `router` mechanic. Calls the user's FreeLLMAPI router on thor (OpenAI-compatible, http://thor:3001/v1 over the tailnet) for bounded grunt work through the `lane` CLI, enforcing data tiers and `lane admission`. Fronts free-tier providers with health-aware fallback. Auto-fires on admitted grunt signals and falls through to Codex on junk.
---

# freellmapi — local FreeLLMAPI wrench

The **grunt-work lane.** Bounded, repetitive, low-stakes per-item work that should never burn Claude or Codex tokens.

The user runs a local OpenAI-compatible router, [`tashfeenahmed/freellmapi`](https://github.com/tashfeenahmed/freellmapi) (MIT), one instance per machine. Cost per call: **$0** — every provider is free-tier or the user's own hardware.

---

## Data tiers and lanes (2026-09-29, binding)

Every FreeLLMAPI call has a data tier. Plan: `_system/second-brain/Projects/lanes/PLAN.md`; tool: `_tools/lanes` (`lane <class> --tier …`).
- **Public** (public pages, synthetic tests, catalogs): `lane <class> --tier public` selects its profile; relays allowed; never AI Horde.
- **Business**: `lane <class> --tier business` selects audited Groq / Ollama Cloud / OVH profiles only. Never a bare model id: a bare id can be served by any provider.
- **Business-PII** (customer names, phones, emails, leads, SMS) and **Private** (vault, handoffs, personal docs): `auto:local` (thor `qwen3.8-text:27b`) only. Never Codex, never cloud.
- Cloudflare chat models are disabled (their neurons are for images). `fast` is a reserved profile name; the interactive lane is `quick`.
- A task class runs on free models only once it is admitted (`lane admission`); otherwise it goes to Codex (Business/Public) or waits.

---

## Where it runs (Linux)

| Host | Bind | Notes |
|---|---|---|
| **odin** | none (retired) | Odin clients use thor's router over the tailnet. |
| **loki** | `127.0.0.1:3001` **and** loki's Tailscale IP (`loki:3001`), never `0.0.0.0` | Serves Hermes (T3). The Tailscale-facing listener requires FreeLLMAPI's unified API key on every request (the router already gates all `/v1` calls behind that key — this satisfies "never unauthenticated on a non-loopback bind" without extra work). |
| **thor** | container on `127.0.0.1:3001`; tailnet `thor:3001` / `thor.<tailnet>.ts.net:3001` via `tailscale serve --tcp 3001` | **The router.** Upstream FreeLLMAPI in Docker at `~/freellmapi-docker` (installer `freellmapi.co/install.sh`, read before running). Local changes live only in `docker-compose.override.yml` (host-gateway for Ollama, port, `PROVIDER_TIMEOUT_CUSTOM=300000`); the installer never rewrites it. Update: `cd ~/freellmapi-docker && docker compose pull && docker compose up -d`. Keep the dashboard login in a 0600 file. Add free-tier and keyless providers only: skip any that needs a payment method, and read each provider's terms (some trial keys are non-commercial). A custom provider can point at thor's own Ollama (`host.docker.internal:11434`). **Adding a key:** copy it in the browser, then run `freellm-keydrop <platform> [--label L] [--base URL] [--account CF_ID]` on odin: it pipes the clipboard to thor's `~/freellmapi-docker/add-key.py` (never prints the key), tests a real call, clears the clipboard and removes that exact key from the clipboard history. Model names: `auto`, `auto:fast`, `auto:smart`, **`auto:local`** (thor's local models only, never cloud). **OpenRouter: $0 free models only, never paid:** never enable a paid model, never use `openrouter/auto`, never follow a 404's "use this paid slug" hint. |

**Never `HOST_BIND=0.0.0.0`** on any host, ever — the app's own README calls this "single-user, guarded only by the unified API key," which is not enough for an open bind.

---

## Endpoints

- Dashboard: `http://thor:3001/` (tailnet; `http://127.0.0.1:3001/` on thor itself)
- OpenAI-compatible base URL: `http://thor:3001/v1`
- Chat endpoint: `http://thor:3001/v1/chat/completions`
- Copy the unified key from the dashboard header.

**Do not** treat this as Claude Code's primary brain. Claude Code's model gateway expects Anthropic-compatible `/v1/messages`; FreeLLMAPI exposes OpenAI-compatible `/v1/chat/completions`. Use this router for side calls and provider-specific work.

---

## When to fire (auto)

The Grunt Rule (from the router mechanic): if the task is a per-item loop of N > 5 bounded, low-stakes operations, map it to a class, check `lane admission`, then run `lane <class> --tier ...` only where admitted. Claude reads the output.

**Fires automatically on:**
- "classify these N items" / "tag each" / "bulk process" / "for each of these"
- N-item simple transforms (regex / format / translation per item)
- Batch translation requests
- Per-item scoring or ranking on bounded criteria
- Pre-classification passes before Claude makes the final judgment call on borderline cases

**Explicit phrases:** "classify these", "tag each", "bulk process", "for each of these N", "grunt work", "low-stakes batch", "use freellm", "use the local router".

Each phrase is only a routing signal. It does not authorize a free model until the class is admitted and the data tier is set.

---

## When NOT to fire

- Single-task important code generation → `codex` wrench
- Architecture decisions, planning, judgment → Claude
- Long-context reads or multimodal → `gemini` wrench
- Anything the user will commit to a code repo on the first try
- Anything where a wrong answer has real consequences (auth, payments, migrations, prod config)
- Private, vault, handoff, or client-PII inputs → `lane <class> --tier private|business-pii` on `auto:local` only

The free-tier providers are good for bulk, not for one-shot critical output. The auto-fire rule is **"per-item AND bounded AND low-stakes"**, not "this is text-shaped, ship it cheap".

---

## Invocation

```bash
lane doctor
lane admission
lane <class> --tier public|business|business-pii|private
```

Choose `<class>` from `_tools/lanes/config/classes.yaml`. Use a free lane only when `lane admission` admits it. Private, vault, handoff, and client-PII work uses `--tier private` or `--tier business-pii` and remains on `auto:local`; never use a bare model ID or print keys.

## Auto-fire + Codex fallback chain

On a grunt signal, identify the class and tier first. The router runs `lane <class> --tier ...` only when `lane admission` admits that class. Invalid Public/Business output falls through to Codex; Private and Business-PII remain `auto:local` and never fall through to cloud.

### What counts as "junk" worth retrying on Codex

- Empty content / whitespace only
- Content that clearly doesn't follow the requested format (asked for JSON, got prose; asked for one-line summary, got 20 paragraphs)
- A refusal or "I cannot" response on a benign request
- Provider-level error in the response wrapper

### What DOES NOT trigger fallback

Do not silently fall through on a Codex/Claude-grade task that was **misrouted** to freellmapi in the first place. If the prompt was wrong-tier, fix the routing — don't paper over with retries. Look at the symptom: if the prompt was inherently above grunt-grade, the routing decision was wrong, not the provider.

---

## Provider setup

Open the dashboard at `http://127.0.0.1:3001/`, add provider keys on the Keys page, then use the fallback chain already configured. Priority order for adding keys:

1. OpenRouter
2. Cloudflare Workers AI
3. Google Gemini (also unblocks the `gemini` wrench's CLI auth — see that wrench)
4. Groq or Cerebras
5. Mistral
6. Z.ai
7. Ollama Cloud, HuggingFace, or NVIDIA only for explicit experiments

**Keep every instance single-user; loopback-only except loki's Tailscale-facing listener (key-gated).** Do not paste provider keys or the unified key into chat, a shell command literal, a commit, or any AI-context-visible text. The declarative `FREEAPI_CONFIG_PATH`/`FREEAPI_CONFIG_JSON` startup-config mechanism exists for repeatable installs (see the app's own `docs/install.md`) if provider keys ever need to be re-applied on a fresh machine.

---

## Health check

Before any lane dispatch, verify the CLI and router are ready:

```bash
lane doctor
```

If it fails, the router stays on Codex/Claude. **Never silently degrade** — a downed local router should fail loud, not silently fall back to a Claude loop that drains the quota.

---

## Example tasks

**Classify 200 public catalog items:**
```bash
lane classify --tier public
```

**Extract structured fields from public documents:**
```bash
lane extract-json --tier public
```

**Normalize public strings with a regex:**
```bash
lane regex --tier public
```

**Tag 1,000 vault notes:**
```bash
lane label-text --tier private
```

**Extract customer leads:**
```bash
lane lead-extract --tier business-pii
```

Run `lane admission` first for every example. The vault and lead examples stay on `auto:local`; judgment tasks such as clickability scoring stay in Claude or Codex.

---

## See also

- [../SKILL.md](../SKILL.md) — the router mechanic (Grunt Rule lives there)
- [codex.md](codex.md) — fallback lane when freellmapi returns junk
- [gemini.md](gemini.md) — the Gemini CLI lane, whose auth fix reuses this router's Google provider key
- Upstream: https://github.com/tashfeenahmed/freellmapi (MIT)
- loki/thor bootstrap: `servers/tools/freellmapi-bootstrap.sh`
