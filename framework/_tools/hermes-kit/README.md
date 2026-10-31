# Hermes fleet harness

One coding session, many models: Hermes keeps the conversation while automatic fallbacks or `/model <alias>` change who answers. This adds configuration to [Hermes Agent](https://github.com/NousResearch/hermes-agent), rather than another router.

## What you need

- Hermes on PATH, Python 3 and PyYAML (or uv, which supplies PyYAML).
- A free-model router with the lanes `-biz` profiles. Review `lanes/config/providers.yaml`, then run `lane chains sync --apply` to build the pools from `lanes/config/chains.yaml`. Only acceptable provider audits enter trusted pools. Never put bare router model ids in the main config.
- The kit's jailed `web` CLI for outside public-web workers.
- Optional: Ollama on a GPU box, a model with at least 65536 tokens of context, and Herdr.

## Install

Set `FREELLMAPI_URL` to the router's `/v1` URL in `~/.config/ai-workflow-kit/hosts.env`. Optionally set `THOR_OLLAMA_URL` (Ollama's root URL). `KIT_HOSTS_ENV` selects another hosts file; environment values take precedence.

Create `~/.config/ai-workflow-kit/freellmapi.key` with mode 0600 using your secret manager or the installer's hidden-input command. No key is printed. The config's `key_cmd` reads that file on demand.

Create `~/.config/ai-workflow-kit/helper-denylist.json` with your guard denylist, or `{"denylist": []}` if you have none. Missing means fail closed; an empty list still enables generic email, key, IP and path scrubbing.

From this directory:

```sh
./install.sh --dry-run
./install.sh
./install.sh --no-local       # omit Ollama and the local alias / vision pin
./install.sh --wake-proxy     # also requires THOR_HOST and a working wake command
./install.sh --self-test      # temporary HOME and stub Hermes, no real config writes
aihermes
```

`HERMES_LOCAL_MODEL` overrides the default `qwen3.6-gpu:35b`. Set its Ollama context to at least 65536 too. Without an Ollama URL, local entries are omitted. With no local provider, do not submit private images: no local vision pin is installed.

The optional proxy listens on `127.0.0.1:3002` and `:11435`, forwarding to the router's standard port 3001 and Ollama's port 11434 on `THOR_HOST`. Its default wake command is `fleet-wake wake thor`; on a cockpit using `thor-up`, set a user-unit override with `systemctl --user edit thor-wake-proxy`:

```ini
[Service]
Environment=WAKE_CMD=thor-up
```

Then restart it: `systemctl --user restart thor-wake-proxy`. `WAKE_CMD` and `WAKE_WAIT` are optional environment overrides. The installer links the proxy and its user unit and enables it; it does not configure Wake-on-LAN for you.

The installer backs up config and SOUL, refuses top-level key conflicts outside its markers, validates YAML, sets the default model, disables Hermes web/browser, links the helper and launcher, and installs the Herdr integration if present. Existing external skill directories are kept when `~/.claude/skills` is added. A repeat run keeps the same content and makes new backups. Keep this directory in place: commands are symlinks into it. The launcher starts in `$AI_PROJECTS_ROOT` (default `~/AI_Projects`) and uses `agents.slice` when available.

## No camera access

The kit's `harness/hooks/camera-guard.py` blocks every AI from opening a camera. It denies shell commands that touch `/dev/video*`, v4l2 tools or the webcam driver, plus browser `getUserMedia` calls. Claude Code gets it through the dispatcher, and Codex through `harness/codex/hooks.json` (record its `trusted_hash`, or trust it once in `codex /hooks`). Hermes's installer does not wire hooks, so add this to `config.yaml` yourself, and add a matching approval to `~/.hermes/shell-hooks-allowlist.json` for gateway runs:

```yaml
hooks:
  pre_tool_call:
    - matcher: ".*"
      command: "/path/to/camera-guard.py --exit2"
      timeout: 10
      fail_closed: true
```

Gemini CLI: a `BeforeTool` entry in `~/.gemini/settings.json` with matcher `.*` running the same command with `--exit2`. On machines where nobody uses a camera, also block the driver with a file in `/etc/modprobe.d/` containing `install uvcvideo /bin/false`. Every AI runs as your user, so the hook is a policy fence, not a sandbox.

## The privacy split

The main model, fallbacks, aliases and conversation-reading side jobs use only audited `-biz` profiles. Optional local vision stays on your hardware. The provider's hosting and retention policy decides trust, not where a model was invented.

Outside models see only scrubbed, generic task text via `ask_outside_helper` or `outside_web_task`. They get no conversation history, files or terminal. The web worker has only jailed public reads and keyless search; robots rules, step and time budgets apply. Public jobs of two or more pages go there; logged-in work stays with the main agent using the kit's `web` skill. Both Hermes tool timeouts are 1800 seconds because the default 420 seconds cuts off web jobs.

## Choose or re-probe outside models

`HELPER_MODELS` is a comma-separated ordered list; set it in the helper-wall MCP `env` mapping. `HELPER_DENYLIST`, `HELPER_API` and `HELPER_KEY_FILE` also override defaults. Without `HELPER_API`, the helper reads `FREELLMAPI_URL` from the environment or hosts.env, then uses the loopback wake-proxy default. The shipped config sets these paths explicitly for kit users.

Probe each candidate against your router. This is an API probe, not a page scrape. It reads the key from disk without displaying it; turn shell tracing off first. Set `FREELLMAPI_URL` from hosts.env and change only the public model id in the body:

```sh
set +x
curl -sS -D - "${FREELLMAPI_URL%/}/chat/completions" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $(cat ~/.config/ai-workflow-kit/freellmapi.key)" \
  --data '{"model":"glm-5.3","messages":[{"role":"user","content":"Call web_read for https://example.com"}],"tools":[{"type":"function","function":{"name":"web_read","description":"Read a public page","parameters":{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}}}]}'
```

Check for a valid `tool_calls` reply and the `X-Routed-Via` response header. An outside id can move to a trusted provider and spend that pool's quota. Re-probe when the catalog changes; omit models that fail tools or route somewhere unsuitable. Leave the main `-biz` profiles intact.

## Undo and limits

Close Hermes. Restore the pre-install `config.yaml.bak-<timestamp>` and `SOUL.md.bak-<timestamp>` beside the originals, or remove the marked blocks and restore the scalar defaults and enabled toolsets yourself. Remove the `aihermes` and `helper-wall` symlinks. For the proxy, first run `systemctl --user disable --now thor-wake-proxy`, remove its symlinks and any override you added, then `systemctl --user daemon-reload`. Restore any replaced commands from their `.bak-<timestamp>` files. Remove the optional Herdr integration with `herdr integration uninstall hermes`.

Free outside models can be slow, rate-limited or wrong. Scrubbing is defense in depth: the wall relies on the main model writing generic questions and cannot recognize every private fact. Outside output is untrusted. Hermes `delegate_task` children inherit all tools, so they are not a privacy barrier. Provider audits and retention switches need periodic review. Fallbacks depend on the Hermes version; verify them after upgrades. A model may repeat the previous model's identity from history; logs identify the actual responder.
