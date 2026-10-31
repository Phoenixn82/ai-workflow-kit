# Hermes: one session, many models

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is a coding harness you run in your projects folder, optionally in Herdr. It keeps one conversation while you switch models by hand or a failing model hands over automatically. Optionally, a [second Hermes](#a-second-hermes-on-a-quarantined-box) lives on a quarantined box and borrows the router through a tunnel only while you use it.

## What you get

```text
you -> Hermes -> trusted pool -> fallback pools -> local (optional)
         |
         +-> helper-wall -> outside models / public web
```

The router and a local model are separate services; Hermes manages session history and fallback. Local models run on your GPU box, not your laptop.

## Routing and context

The main pool is `auto:code-biz`. Fallbacks try `auto:checker-biz`, then `auto:ops-biz`, then optional local Ollama. Hermes can fall back on 429, 5xx, 401, 404 or empty/unusable replies, carrying the conversation across.

Every declared model has `context_length: 65536`. Compression then keeps the session small enough for any model in the chain to take over. Your local server must support the same context size.

In the same session, type `/model code`, `/model check`, `/model write`, `/model ops`, `/model quick` or `/model local` (when configured). These aliases select the corresponding `-biz` pool, or your local model. They do not start a new conversation.

### Picking the main model

The installer sets `model.default` to `auto:code-biz`. Another trusted option is `auto:uncensored-biz`, built from the `uncensored` chain in `lanes/config/chains.yaml`: hosted US-made models in the order that chain lists them (Gemma 4 31B first, then Llama 3.3 70B, GPT-OSS 120B and Nemotron). All of them still refuse some gray-area prompts. The `-biz` tier keeps only the providers your audit in `providers.yaml` accepts, so this is still a vetted pool. Run your own probe before you rely on the order.

To make it the default, edit the `model:` section of `~/.hermes/config.yaml`:

```yaml
model:
  default: auto:uncensored-biz
  provider: custom:freellm
```

The kit's config block does not declare this pool or give it an alias. Add these lines inside the marked block, to the maps that are already there (not as new top-level keys):

```yaml
providers:
  freellm:
    models:
      auto:uncensored-biz: {context_length: 65536}   # beside the other -biz entries
model_aliases:
  uncensored: {provider: "custom:freellm", model: "auto:uncensored-biz"}
```

`/model uncensored` then hops to it mid-session. If it is your default, also point `auxiliary.compression` and `auxiliary.background_review` at it, so a summary job does not refuse a conversation the main model accepted. A rerun of `install.sh` rewrites the marked block and sets `model.default` back to `auto:code-biz`: add these lines again afterwards.

### Running without a local GPU

`install.sh --no-local` (or an empty `THOR_OLLAMA_URL`) leaves out the Ollama provider, the local fallback, `/model local` and the local vision pin. The `auto:checker-biz` and `auto:ops-biz` fallbacks stay.

To go further and keep Hermes on the pool you picked, set `fallback_providers: []` inside the marked block. The router still falls through the models inside that pool; when the whole pool is rate-limited you get an error instead of a hop to a pool with different behavior. Add a SOUL line such as "If every pool is rate-limited, say so and stop."

Without a vision pin, Hermes's default `auto` side job could send images anywhere. Either keep private images out, or pin hosted vision inside the marked block (declare `auto:vision-biz: {context_length: 65536}` under `providers.freellm.models` too):

```yaml
auxiliary:
  vision: {provider: freellm, model: "auto:vision-biz", timeout: 120}
```

The `vision` chain holds Mistral's Ministral models; its `-biz` pool exists only if your audit accepts Mistral. Screenshots then leave your machines for that provider. Both edits are inside the block, so re-add them after a rerun of `install.sh`.

## Where private work goes

Trust follows the **provider hosting the model**, not its origin. A Chinese-made model served by a vetted provider can be trusted; a familiar model name through an unknown relay may not be.

The main model, fallbacks, router aliases and six side jobs (compression, title_generation, web_extract, session_search, curator and background_review) use only `-biz` profiles. `lane chains sync --apply` builds these from `lanes/config/chains.yaml`, filtered by acceptable audit status in `lanes/config/providers.yaml`. Review audits and training/retention settings for your accounts. Never add a bare model id to the trusted config: the router could choose any provider.

Outside providers (including Chinese-hosted services, training-enabled free tiers and relays) are reachable only through helper-wall. `ask_outside_helper` sends question text only; `outside_web_task` sends a generic task to a public-web worker with just jailed `web read` and keyless search. Both scrub task text first. The worker cannot log in or use the terminal or your files.

Jobs of two or more public pages go to `outside_web_task`. Logged-in work stays with the main agent through the kit's [web skill](web.md). Hermes's built-in `web` and `browser` toolsets are disabled. MCP and both tool-call timeouts are 1800 seconds; the default 420 seconds kills longer web jobs.

## Setup

From the kit root:

1. Install Hermes using the optional manifest entry's official command; check `hermes --version`. The installer flags skip browser and computer-use downloads. Hermes is MIT licensed; its own tools are not copied into the kit.
2. Set up the [free router and lane CLI](agents-and-lanes.md#free-and-local-models-the-lane-cli). Review provider audits, run `lane chains sync --apply`, and make sure the `-biz` pools exist before sending private work.
3. Set `FREELLMAPI_URL` in `~/.config/ai-workflow-kit/hosts.env` to the router's `/v1` URL. Optionally set `THOR_OLLAMA_URL` and `HERMES_LOCAL_MODEL` for a local 64k model. The [hosts template](../examples/hosts.env.example) has the address fields.
4. Store the router key in `~/.config/ai-workflow-kit/freellmapi.key` (0600). The installer prints a hidden-input command if it is missing. Create `helper-denylist.json` beside it using your guard rules, or `{"denylist": []}` if you have none. Missing denylist means no requests leave the wall.
5. Run the harness installer:

```sh
framework/_tools/hermes-kit/install.sh --dry-run
framework/_tools/hermes-kit/install.sh
aihermes
```

Use `--no-local` to omit Ollama. Use `--wake-proxy` only if the router/GPU host suspends: set `THOR_HOST`, install a wake command, and follow the [proxy notes](../framework/_tools/hermes-kit/README.md#install). This uses loopback ports 3002 and 11435. Herdr integration is installed automatically when `herdr` is on PATH. No gateway is enabled; Hermes on heimdall is optional for an always-on Telegram gateway you configure separately.

The profile installer runs this step only when you select `hermes-kit` with `--harness`. The installer backs up config and SOUL, replaces only its marked blocks, and refuses conflicting top-level keys outside them (exit 4, no writes). Reconcile those keys yourself before retrying. It sets model defaults and external skills through `hermes config set`; it never adds a new shell function. The `aihermes` executable enters `$AI_PROJECTS_ROOT` or `~/AI_Projects` and uses `agents.slice` when available. The SOUL block it adds ([SOUL-addendum.md](../framework/_tools/hermes-kit/SOUL-addendum.md)) carries the privacy split, the [which-machine rule](#which-machine) and the camera rule. It wires no hooks: add those yourself ([Guard hooks](#guard-hooks)).

## Guard hooks

Hermes runs the shell hooks listed under `hooks:` in `config.yaml` before each tool call. `hooks` is not one of the installer's block keys, so put it outside the markers; it survives reruns. Two hooks ship with the kit:

- [`agent-hooks/route-guard.py`](../framework/_tools/hermes-kit/agent-hooks/route-guard.py) keeps Hermes on the kit's [routing](agents-and-lanes.md). On the `terminal` tool it blocks `claude -p` (or `--print`) with no `--model`, any Sonnet model (not routed in `lanes.json`), and a bare `codex exec` or `codex e`. The block message tells the model to run `route list`, then exactly the command `route <class>` prints. It splits chained commands on `;`, `&`, `|` and newlines, so a separator quoted inside a prompt can split wrongly. It is useful only if you use `route` and `cx`.
- [`camera-guard.py`](../framework/harness/hooks/camera-guard.py) with `--exit2` blocks every tool call that would open a camera (see the [harness README](../framework/_tools/hermes-kit/README.md#no-camera-access)).

Link both into `~/.hermes/agent-hooks/` from the kit root, then add the entries to `~/.hermes/config.yaml`:

```sh
mkdir -p ~/.hermes/agent-hooks
ln -s "$PWD/framework/_tools/hermes-kit/agent-hooks/route-guard.py" ~/.hermes/agent-hooks/route-guard.py
ln -s "$PWD/framework/harness/hooks/camera-guard.py" ~/.hermes/agent-hooks/camera-guard.py
```

```yaml
hooks:
  pre_tool_call:
    - matcher: "terminal"
      command: "~/.hermes/agent-hooks/route-guard.py"
      timeout: 10
      fail_closed: true
    - matcher: ".*"
      command: "~/.hermes/agent-hooks/camera-guard.py --exit2"
      timeout: 10
      fail_closed: true
```

A hook blocks with exit code 2. An allowed call prints nothing and exits 0. With `fail_closed: true`, a hook that crashes, times out or prints something other than JSON blocks the call instead of letting it through.

Hermes asks once, at the terminal, before it registers a new hook, and records your yes in `~/.hermes/shell-hooks-allowlist.json`. Runs with no terminal (a gateway, or `hermes -z` over plain ssh) skip a hook that is not on that list, with only a log warning. So start one interactive session and approve each hook first, or start those runs once with `--accept-hooks` (or `HERMES_ACCEPT_HOOKS=1`), which records the approval. An entry looks like this; the command must match `config.yaml` character for character:

```json
{"approvals": [{"event": "pre_tool_call", "command": "~/.hermes/agent-hooks/route-guard.py",
  "approved_at": "<set by Hermes>", "script_mtime_at_approval": "<set by Hermes>"}]}
```

Check, from the kit root:

```sh
framework/_tools/hermes-kit/agent-hooks/route-guard.py --self-test    # route-guard self-test passed
framework/harness/hooks/camera-guard.py --self-test                   # camera-guard self-test passed
echo '{"tool_input":{"command":"claude -p hi"}}' | framework/_tools/hermes-kit/agent-hooks/route-guard.py; echo "exit $?"
hermes hooks list      # both hooks, approved
hermes hooks doctor    # exec bit, allowlist, script changes since approval
```

The `echo` line prints the block JSON and `exit 2`. Undo: remove the `hooks:` entries, run `hermes hooks revoke "<command>"` for each, and delete the two links. Every AI runs as your user, so hooks are a policy fence, not a sandbox.

## Which machine

Local MCPs, browsers and `web act` act on the machine Hermes runs on, whatever machine the user names. A Hermes asked about another box can drive its own desktop and report the results as the other box's. The SOUL addendum's "Which machine" paragraph covers the rule (check `hostname`, work on another machine only over its ssh alias). It does not know your fleet, so add a short machine map to `~/.hermes/SOUL.md`, outside the markers so reruns keep it:

```text
Machines (ssh aliases exist for each):
- odin: this laptop. The local MCPs, browsers and logins live here and act only here.
- thor: GPU and router box. It sleeps when idle: run `thor-up` first, then `ssh thor`.
- heimdall: always-on scheduler. `ssh heimdall`.
- loki: quarantined box, one-way. `ssh loki`; its desktop VM is `ssh loki-desk`, driven from here with
  `loki-shot` and `loki-gui`. Never do credentialed work there.
When the user names a machine, the work happens on that machine. Never report local results as another machine's.
```

## How do I know it works?

Offline checks (from the kit root):

```sh
framework/_tools/hermes-kit/install.sh --self-test
framework/_tools/helper-wall/helper-wall --self-test
framework/_tools/thor-wake-proxy/thor-wake-proxy --self-test
```

Session-hop recall: run `aihermes`, then enter each line in the same session. Each answer should recall `cedar-orbit-47`:

```text
Remember the public test codeword cedar-orbit-47. Reply with it.
/model check
What was the codeword?
/model quick
What was the codeword?
/model local
What was the codeword?
```

Skip `local` if you omitted Ollama. Check Hermes logs for the provider actually used; a model's self-description can repeat earlier history.

Forced-404 fallback: test in an isolated Hermes home, so your installed config stays unchanged. Choose a deliberately missing **profile**, never a bare model id. Run:

```sh
test_home=$(mktemp -d)
cp ~/.hermes/config.yaml "$test_home/config.yaml"
cp ~/.hermes/SOUL.md "$test_home/SOUL.md"
HERMES_HOME="$test_home" hermes --provider custom:freellm --model auto:missing-test-biz
```

In that session ask `Reply with the public word maple.` Verify the missing profile produces a 404 and a configured fallback answers. Inspect `$test_home/logs/` for the attempted and fallback models. Exit Hermes, then remove the test directory with `rm -r -- "$test_home"`. If your router substitutes unknown profiles instead of returning 404, this does not test fallback: use a test router that returns 404. Do not change production routing just to force the error.

Helper-wall scrub check: the self-test uses synthetic names, an email, an IP, a path and a fake key, and asserts the captured outgoing question contains none of them. For an online check, in Hermes enter:

```text
Call ask_outside_helper with question: Explain recursion for test-person@example.com. Do not add context.
```

The response should say `EMAILx1` was redacted. Use only synthetic test data.

Public-web worker check, in Hermes:

```text
Call outside_web_task with task: Read https://example.com and https://www.iana.org/domains/reserved and return a two-row table with page title and source URL. No private context.
```

Verify the returned sources and the helper log's web_read calls. Slow/rate-limited models can take several minutes.

Provider check: run the [tool-call curl probe](../framework/_tools/hermes-kit/README.md#choose-or-re-probe-outside-models). Inspect `X-Routed-Via` and valid `tool_calls`. Repeat with `"model":"auto:code-biz"`; that header must name a vetted provider. Re-probe outside candidates before changing `HELPER_MODELS`; a bare outside id can start consuming a trusted pool if its hosting changes.

## Limits and undo

Free outside models are slower and rate-limited, and their output is untrusted. The wall relies on the main model writing generic questions; scrubbing cannot catch every private fact. `delegate_task` children inherit all tools and are not a privacy barrier. Review provider audits periodically and retest fallback after Hermes updates. Without local Ollama, no local vision pin is installed; keep private images out of that setup unless you [pin hosted vision](#running-without-a-local-gpu). Lines you add inside the marked block (the uncensored pool, an empty fallback list, hosted vision) are replaced on every rerun of `install.sh`.

Restore the pre-install `config.yaml.bak-<timestamp>` and `SOUL.md.bak-<timestamp>`, remove the launcher/helper links, and disable/remove the optional proxy unit. Remove the guard hooks as in [Guard hooks](#guard-hooks). Exact undo and replaced-command backup instructions are in the [harness README](../framework/_tools/hermes-kit/README.md#undo-and-limits) and [uninstall guide](../install/uninstall.md).

## A second Hermes on a quarantined box

A second Hermes can live on the one-way [loki](../profiles/loki/README.md) box and work only there. The box holds nothing private, so this Hermes may use the public-tier pools (any free provider, including models your trusted pools leave out) and a permissive SOUL. It still cannot reach your other machines.

```text
cockpit (odin)                                                 box (loki)
aihermes-loki ── ssh -t -R 127.0.0.1:3002:127.0.0.1:3006 ──▶  hermes, in ~/hermes-work
loki-llm-gate 127.0.0.1:3006  ◀──── the tunnel ────────────── model calls to 127.0.0.1:3002
  │  only /v1/* passes; the router's admin API and dashboard get 403
  ▼
thor-wake-proxy 127.0.0.1:3002 ──▶ the router on thor (:3001), profile auto:loki
```

### How the one-way rule holds

- The box still cannot open a connection to your other machines: its host firewall and `tag:quarantine` stay as the [loki profile](../profiles/loki/README.md#why-it-looks-like-this) sets them. Nothing on the box changes.
- Model calls ride an `ssh -R` reverse tunnel that you open from the cockpit. It exists only while that session is open; close it and the box has no route to the router.
- At the cockpit end, [`loki-llm-gate`](../framework/_tools/hermes-loki/loki-llm-gate) passes `GET` and `POST` to the router's `/v1/*` (chat, models) and nothing else. The router's admin API (`/api/...`), its dashboard and any path with `..`, an encoded dot or slash, or a backslash get 403; other methods get 501. It forwards to `thor-wake-proxy`, which wakes thor if it sleeps, and streams replies back unbuffered.
- The box holds one secret, a router key. If your router can issue more than one key, give the box its own so you can revoke it alone. While a session is open, any process on the box can use the tunnel with that key.
- Public-tier providers may log or train on what they receive, and this Hermes sends them what it reads on the box. Keep private files off it, as the quarantine already asks.

The main pool is `auto:loki`, from the `loki` chain in [chains.yaml](../framework/_tools/lanes/config/chains.yaml): first free models that tend to refuse less under the example SOUL's permissive brief, then Chinese-made and other free models, then stricter ones. The system prompt often matters more than the model. Fallbacks are `auto:public` and `auto:code`, vision is `auto:vision`, and `/model uncensored` reaches the public `uncensored` pool. No pool holds local GPU models, and lanes policy refuses paid OpenRouter ids (`openrouter-paid`) in every tier.

### Install on the cockpit

You need `thor-wake-proxy` listening on `127.0.0.1:3002` (`install.sh --wake-proxy` from [Setup](#setup), or the manifest's `thor-wake-proxy` entry). From the kit root:

```sh
mkdir -p ~/.local/bin ~/.config/systemd/user
ln -s "$PWD/framework/_tools/hermes-loki/loki-llm-gate" ~/.local/bin/loki-llm-gate
ln -s "$PWD/framework/_tools/hermes-loki/loki-llm-gate.service" ~/.config/systemd/user/loki-llm-gate.service
systemctl --user daemon-reload
systemctl --user enable --now loki-llm-gate
```

Or let the kit installer make the two links (`install/install.sh --profile odin --only hermes-loki-gate --optional hermes-loki-gate`), then run the two `systemctl` lines. The unit runs `loki-llm-gate 3006 3002` (listen port, upstream port, both on `127.0.0.1`). Add the launcher to `~/.bashrc`:

```bash
aihermes-loki() {
  local args=""
  [ $# -gt 0 ] && args=$(printf '%q ' "$@")
  ssh -t -R 127.0.0.1:3002:127.0.0.1:3006 loki "cd ~/hermes-work && exec ~/.local/bin/hermes $args"
}
```

Arguments pass through quoted, so `aihermes-loki -c` continues the last session. It runs in Herdr like any other terminal command.

### Install on the box

The box reaches the internet, so Hermes installs there the usual way. Every step below runs from the cockpit's kit root.

1. Install Hermes with the official command from the manifest's `hermes` entry, inside `ssh -t loki`. Check with `ssh loki '~/.local/bin/hermes --version'`, and create the working folder: `ssh loki 'mkdir -p ~/hermes-work'`.
2. Set the model, back up the config, then append the block. `hermes config set` may rewrite the file, so append last:

   ```sh
   ssh loki '~/.local/bin/hermes config set model.default auto:loki && ~/.local/bin/hermes config set model.provider custom:freellm'
   ssh loki 'cp ~/.hermes/config.yaml ~/.hermes/config.yaml.bak-$(date +%Y%m%d)'
   ssh loki 'cat >> ~/.hermes/config.yaml' < examples/hermes-loki/config-block.yaml
   ```

   A fresh config has none of the block's top-level keys (`providers`, `fallback_providers`, `model_aliases`, `auxiliary`, `hooks`). If yours has any, merge them by hand instead of appending.
3. Edit the "This machine" and "What you're for" parts of [`examples/hermes-loki/SOUL.md`](../examples/hermes-loki/SOUL.md), then copy it and the camera hook. These are copies, not links, because the box has no kit clone; copy them again after a kit update.

   ```sh
   ssh loki 'mkdir -p ~/.hermes/agent-hooks ~/.local/bin; [ ! -f ~/.hermes/SOUL.md ] || cp ~/.hermes/SOUL.md ~/.hermes/SOUL.md.bak-$(date +%Y%m%d)'
   scp examples/hermes-loki/SOUL.md loki:.hermes/SOUL.md
   scp framework/harness/hooks/camera-guard.py loki:.hermes/agent-hooks/camera-guard.py
   ```

4. Store the router key at mode 600 without printing it. Open `ssh -t loki` and run:

   ```sh
   mkdir -p ~/.config/hermes-loki
   (umask 077; read -rs -p "Router key: " key; printf "%s" "$key" > ~/.config/hermes-loki/router.key; unset key)
   ```

5. Only if the box runs a desktop VM: copy the desk helpers.

   ```sh
   scp framework/_tools/hermes-loki/desk-shot framework/_tools/hermes-loki/desk-gui loki:.local/bin/
   ```

   They call `ssh loki-desk`, so the box needs a `Host loki-desk` entry for the VM with the box's key authorized there, and ImageMagick (`magick`). The VM needs an X display `:0`, `xdotool` and `xwd` (Debian package `x11-apps`). `desk-shot` saves under `~/hermes-work/shots/` and prints the path. Check from the box with `ssh loki ssh loki-desk true`. The kit's one-way firewall rejects new connections to private address ranges, and a libvirt VM's default network is one; if the check fails, the VM bridge (`virbr0`) needs the same accept rule the box's own containers get. From the cockpit, [`loki-shot` and `loki-gui`](../framework/harness/bin/) do the same jobs through a `loki-desk` alias that jumps through loki.
6. Run `aihermes-loki` once and approve the camera hook at the prompt. Runs without a terminal skip an unapproved hook ([Guard hooks](#guard-hooks)).

### Check the quarantined Hermes

From the cockpit's kit root:

```sh
framework/_tools/hermes-loki/loki-llm-gate --self-test     # loki-llm-gate self-test: ok
systemctl --user is-active loki-llm-gate                  # active
ssh -R 127.0.0.1:3002:127.0.0.1:3006 loki 'curl -s -o /dev/null -w "%{http_code}\n" 127.0.0.1:3002/api/keys'   # 403
ssh -R 127.0.0.1:3002:127.0.0.1:3006 loki 'cd ~/hermes-work && ~/.local/bin/hermes -z "run hostname"'          # loki
```

The self-test runs the gate against a stub router: `/v1/` passes, `/api/keys`, `/` and traversal tricks get 403, `PUT` gets 501. The 403 through the tunnel proves the router's admin API stays closed to the box. The one-shot proves model calls flow and that commands run on the box, not on the cockpit. The one-way checks in the [loki profile](../profiles/loki/README.md#verify) must still fail.

### Two sessions share one tunnel

A second `aihermes-loki` while one is open cannot bind the box's port 3002. ssh warns `remote port forwarding failed for listen port 3002`, and the second Hermes rides the first session's tunnel. When the first session closes, the second loses its model calls until you start it again. Keep to one session, or add `-o ExitOnForwardFailure=yes` to the `ssh` line so a second launch refuses to start.

### Undo the quarantined Hermes

On the cockpit:

```sh
systemctl --user disable --now loki-llm-gate
rm ~/.local/bin/loki-llm-gate ~/.config/systemd/user/loki-llm-gate.service
systemctl --user daemon-reload
```

Then delete `aihermes-loki` from `~/.bashrc`. On the box, restore `~/.hermes/config.yaml.bak-<date>` and `~/.hermes/SOUL.md.bak-<date>` (or delete the lines between the two `quarantined-box Hermes` comments), run `hermes hooks revoke "~/.hermes/agent-hooks/camera-guard.py --exit2"`, and remove `~/.hermes/agent-hooks/camera-guard.py`, `~/.local/bin/desk-shot`, `~/.local/bin/desk-gui` and `~/.config/hermes-loki/router.key`. Revoke that key in the router too. To remove Hermes itself, follow its own uninstall instructions.
