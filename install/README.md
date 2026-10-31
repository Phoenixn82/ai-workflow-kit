# install/: one manifest for every tool

`tools.json` lists every tool the kit uses: the third-party ones (downloaded from their official source at
install time, never stored in this repo) and the kit's own ones under `framework/` (installed from this clone).
Profiles (`profiles/<name>/profile.json`) pick tools from it by `id`. The installer and the doctor read it;
nothing else holds install commands.

```bash
python3 install/test_manifest.py              # check the manifest (PASS or every problem, exit 1)
python3 install/test_manifest.py --plan       # how each tool resolves on arch, debian and macos
python3 install/test_manifest.py --self-test  # broken copies of the manifest must each be caught
```

## install.sh and doctor.sh

```bash
install/doctor.sh --profile odin                     # one line per tool: ok | missing | wrong-version (writes nothing)
install/install.sh --profile odin,telegram --dry-run # the plan and what each step would do; changes nothing
install/install.sh --profile odin,telegram           # asks once, then installs what is missing
install/install.sh --profile odin --harness          # also merges hooks, settings and agent rules (backup first)
bash install/test_install.sh                         # the installer's own checks (safe anywhere; --containers: Debian + Arch)
```

| Flag | Meaning |
|---|---|
| `--profile a[,b]` | one or more of `odin loki heimdall thor raven telegram` (required) |
| `--dry-run` | print the plan and every step it would take; nothing is written |
| `--yes` | do not ask; installs the required tools, plus the optional ones `--optional` names |
| `--optional all\|none\|<id,...>` | which optional tools and services to add (without it the installer asks; `--yes` alone means none) |
| `--os arch\|debian\|macos` | plan for another OS; only with `--dry-run` (a real run uses the detected OS) |
| `--harness` | also run the steps that touch agent config: `claude-plugin` methods, `harness: true` steps and the profile `rules` |
| `--only <id,...>` | just these tools of the profile (and what they require) |
| `--ignore-hardware` | go on when `requires_hardware` does not match (it warns instead of stopping) |

Exit codes: `0` done (or a dry run), `1` a tool, rule, service or unit failed (the summary names it), `2` bad
arguments or a failed prerequisite: wrong OS for the profile, hardware that matches no `requires_hardware` branch,
no Homebrew on macOS, sudo refused, or no terminal to ask on without `--yes`. Nothing is installed after a `2`.
`doctor.sh` exits 1 when a required tool is missing or a required service fails its health check; without
`--profile` it lists every tool and only reports. Python 3.10+ is the one thing both need first; `install.sh`
offers to install it with the OS package manager when it is missing.

What a run does, in order:

1. **Prerequisites.** The profile's `os` list, then `requires_hardware`: RAM and the largest disk (10% slack for
   what a "16 GB" machine actually reports), and the `any_of` branches (`nvidia-smi` for VRAM, `arm64` macOS for
   Apple silicon). `always_on` becomes a note.
2. **The plan.** One line per tool: `present`, `install`, `manual`, `harness` (needs `--harness`) or `skip` (not for
   this OS), with the method it resolves to. Tools that need others come after them; a profile's `server-lane-*`
   tool runs first (it brings the packages, Docker, firewall and the stack folder).
3. **One question**, then each missing tool: its method (a `sudo` line says why first, a piped script is
   downloaded, checked against its pinned sha256 and only then run, see [Pins](#pins-exact-versions-and-how-to-bump-them);
   Debian's package lists are refreshed once before the first `apt-get install`), its install steps,
   then its `verify` again. A tool whose verify still fails is `failed`, `manual` or `needs --harness`, never
   "installed"; a tool that depends on it is `blocked`.
4. **Rules** (only with `--harness`): `merge-json` keeps every value you have, adds missing keys and list items,
   adds a hook only when its command is not wired yet and every script under your home it names is installed, turns
   on only the plugins that are installed, writes your home path out (in a hook `command` every `$HOME`, `${HOME}`
   and leading `~` becomes your home quoted for where it stands, so a home with spaces, quotes, `$` or backslashes
   stays one word and the command around it, a launch guard included, is unchanged; in `env` and other values it
   is written plainly) and takes host values from `hosts.env` (`OLLAMA_HOST` from `THOR_OLLAMA_URL`, else left out).
   Unattended mode (`permissions.defaultMode: bypassPermissions`, `skipDangerousModePermissionPrompt`) is added only
   when the safety dispatcher's PreToolUse hook is wired in the result; otherwise both wait (listed as left out).
   That hook fails closed: if the dispatcher cannot start, every matched tool call is blocked. It runs
   `/usr/bin/python3`, which on a Mac is only a stub until the Xcode Command Line Tools are installed, so on macOS
   check `/usr/bin/python3 -I -S -c ''` succeeds (or run `xcode-select --install`) before `--harness`.
   `merge-markdown` appends the kit's `## ` sections your file lacks and never edits one you have. A file that would
   not change is not touched; one that would is backed up first. A file that is a link (a dotfiles manager's) stays
   a link: the merge, and its backup, go to the file the link points at.
5. **Services.** A service starts only when its tool and everything that tool `requires` are in place in this
   run: uptime-kuma, ntfy and Pi-hole require `server-lane-heimdall` (its firewall), so `--only uptime-kuma` brings
   the server setup along, and a failed setup leaves them stopped even when their image is already there.
   `compose`: `docker compose [--env-file] [--profile] up -d <service>` in the service's
   `project_dir`, never on the template in the clone. A missing project gets the template folder's files once
   (`env_example` as `.env`, mode 600); a `.env` made in this run waits for you to fill it in (the next run starts
   it), and an `env_file` missing a `${VAR:?}` the template needs stops with "set VAR". Then the health check, with
   `hosts.env` loaded, retried for up to 60 s. ollama-embed also gets its model after `up` (`POST_UP` in `lib.py`).
   `systemd`: only the health check (its tool installed the unit).
6. **Units.** Each automation's `files` are copied into `~/.config/systemd/user` (or `/etc/systemd/system` with
   sudo), `daemon-reload`, then `enable --now` only when `enable` is true, and then lingering for user units. Every
   unit also gets a row in `~/AI_Projects/_system/automations/REGISTRY.md` (made if missing) with its kill switch,
   unless its unit file name (`retain.timer`) is already there. Units are Linux only; on macOS they are listed as skipped.
7. **Summary**: installed, present, manual, needs `--harness`, needs you, blocked, failed; every backup made;
   every file of yours that was kept; what was left out of the merged config; the steps left to you. A dry run
   ends with how many files it would back up first; a `server-lane-*` step's own writes are its setup script's, so
   the dry run names the script's preview instead (`servers/linux/setup.sh <lane> --dry-run`).

What it never does: replace one of your files outside `--harness` (yours is kept and listed), replace anything
without a `<file>.bak-<timestamp>` beside it first (an entry of a `skills/` folder goes to
`skills.bak-<timestamp>/<name>`, so the copy does not load as a second skill), edit a compose project or `.env`
that exists, enable a unit the profile leaves off, or install anything before you say yes. Running it twice
changes nothing the second time.

Commands run in bash from the clone's root with [`shell-env.sh`](shell-env.sh) as `BASH_ENV`: as root without a
`sudo` binary (a fresh container) `sudo` just runs the command, and right after Docker is installed `docker` runs
through `sg docker` until you log in again. `~/.local/bin` is put first on `PATH` (uv, claude and `npm -g` install
there; `npm -g` goes to `~/.local` when npm's own folder belongs to root).

For tests: `KIT_FAKE_HW='{"ram_gb": 16, "disk_gb": 500, "nvidia_vram_gb": 8, "apple_silicon": false,
"unified_memory_gb": 0}'` replaces the hardware probe, and `KIT_HEALTH_WAIT=<seconds>` shortens the health wait.
`python3 install/lib.py plan --profile <name>` prints the plan as JSON.

## Conventions

- Commands and `verify` run in bash with the **clone's root as the working directory**, so `framework/...` and
  `servers/...` paths are relative to the clone. `~` is your home directory.
- OS names: `arch`, `debian` (Debian and Ubuntu), `macos`. Profile names: `odin`, `loki`, `heimdall`, `thor`,
  `raven`, `telegram`.
- Remote code comes only from exact versions (see [Pins](#pins-exact-versions-and-how-to-bump-them)). A piped
  script has exactly the shape `curl <flags> <versioned https URL> | [VAR=value ...] sh|bash [args]` and its sha256
  in the tool's `script_sha256`; the installer never pipes it: it downloads the file into a private folder, checks
  the hash and runs that file, or stops with "upstream changed ... install manually from <url>". Commands that need
  root start with `sudo` on purpose, so the installer can say so.
- Host addresses and secrets are never in the manifest. Tools read them from
  `~/.config/ai-workflow-kit/hosts.env` (or the environment) and fail with "set X" when one is missing. The
  `hosts-env` tool copies the empty template there once (mode 600) and never touches a file that exists.
- A `verify` that looks for a system binary also checks `/usr/sbin`: Debian keeps `smbd`, `smartctl`, `unbound`
  and friends there, outside a normal user's `PATH`. `test_manifest.py` enforces it for the usual suspects.

## A tool entry

| Field | Type | Meaning |
|---|---|---|
| `id` | string | lowercase, digits, dashes; what profiles and `requires` refer to |
| `name`, `what` | string | display name and one line on what it does for you |
| `profiles` | list | the profiles that include it (must match the profile files) |
| `homepage`, `license` | string | where it comes from (bundled: its path in this repo) and its license (SPDX where one exists) |
| `bundled` | bool | `true` = ships in this repo under `framework/` or `servers/` |
| `optional` | bool | `true` = the profile works without it; the installer asks before adding it |
| `verify` | string | shell command, exit 0 = already installed |
| `os` | list, optional | the OSes it supports (default all three); elsewhere it is skipped with a message |
| `methods` | object | install commands, keys below; `manual` is always present |
| `requires` | list, optional | tool ids that must be installed first (no cycles; a required tool never depends on an optional one); a service of the tool starts only when these are in place |
| `script_sha256` | object, optional | a piped script's URL → its sha256 (64 hex digits); every piped URL needs one, and every entry must be used |
| `install` | list | bundled tools: ordered install steps (below). Third-party tools may add follow-up steps, such as registering an MCP server |
| `entrypoints` | object | bundled tools: command name → its file in the kit |
| `config` | list | bundled tools: what it reads, each `{"path", "keys"?, "required"?, "note"?}` |

### Picking a method (third-party tools)

For OS *x* the installer uses the first key that exists: `x` (that OS's package manager or official installer),
then `mise`, `uv`, `npm`, `docker`, `claude-plugin`, and finally `manual`, which it prints as
"install manually: ..." and moves on. It never reports a manual tool as installed until its `verify` passes.
A tool whose chosen method is `uv`, `npm`, `docker`, `claude-plugin` or `mise` lists that runtime in `requires`.

`claude-plugin` installs into `~/.claude` (Claude Code's plugin folder and `enabledPlugins` in `settings.json`), so
the installer treats it like a `harness: true` step: it runs only with `--harness`, after backing up
`settings.json`; without `--harness` the tool is listed as skipped. No other method may run `claude plugin
install`, `claude mcp add` or `git config --global` (`test_manifest.py` fails it); those belong in the
`claude-plugin` method or a `harness: true` step.

### Pins: exact versions and how to bump them

Every download command in the manifest names one exact version: `test_manifest.py` fails a piped script without
a versioned URL and a sha256, `curl`/`wget` in any other shape, an `npm install -g`, `uv tool install`, `pip install`, `mise use` or
`uv-venv` package without one exact version, and a `latest` or bare-major image tag (`:2`, `:v3`) in `tools.json` or
any compose template, and it checks that a docker-method tool pulls the image its service's compose file runs.
Signed OS packages (`pacman`, `apt-get`, Homebrew) are preferred where the OS has one; they follow the OS's own
updates. Unversioned vendor installers (get.docker.com, claude.ai/install.sh, chatgpt.com/codex/install.sh,
herdr.dev/install.sh, mise.run) are not used.

Still decided upstream at install time (know them before you rely on the pins): Claude Code plugins install from
their marketplace's current state; `scrapling install` fetches its browsers; `==` pins in `uv tool install`, `pip
install` and `uv-venv` fix the named packages, not their dependencies; the pinned Ollama script downloads that
version's binary from ollama.com over HTTPS without a separate checksum; FreeLLMAPI's `better-sqlite3` may fetch
a prebuilt binary for your Node version from its GitHub release. Image tags are versions, not digests.

| What | Pinned to | Where |
|---|---|---|
| uv (Debian) | `uv-installer.sh` of release 0.12.5 + sha256 | `tools.json` uv |
| mise (Debian) | `install.sh` of release v2026.8.14 + sha256 (it checks its tarball's own checksum) | `tools.json` mise |
| Tailscale (Debian) | `scripts/installer.sh` at tag v1.102.3 + sha256, `TAILSCALE_VERSION=1.102.3` from Tailscale's signed apt repo | `tools.json` tailscale |
| Ollama (Debian) | `scripts/install.sh` at tag v0.33.3 + sha256, `OLLAMA_VERSION=0.33.3` | `tools.json` ollama |
| Docker (Debian) | the distro's `docker.io` + `docker-compose-v2` (Ubuntu) or `docker-compose` (Debian 13+; Debian 12 ships Compose v1 only, so `docker` stays failed there until a server lane installs Docker's own signed packages) | `tools.json` docker |
| Claude Code, Codex, Gemini CLI, agent-browser, computer-use-linux | npm `@2.1.296`, `@0.162.1`, `@0.63.0`, `@0.38.1`, `@0.7.1` (Codex on Arch: the `openai-codex` package) | `tools.json` |
| yt-dlp, faster-whisper venv, Scrapling, tg-admin venv | exact `==` versions | `tools.json` |
| herdr (Linux), Betterleaks | manual: an exact release file and its sha256 in the manual line | `tools.json` |
| Images | uptime-kuma 2.5.6, ntfy v2.30.0, Pi-hole 2026.09.0, Ollama 0.33.3, gluetun v3.41.3 (media: as tagged) | `servers/compose/*/compose.yaml` and `tools.json` |
| FreeLLMAPI | commit `5d7b9b6` (v0.13.3), installed with `npm ci` from its lockfile | `servers/tools/freellmapi-bootstrap.sh` |

To bump a pin: pick a release that is at least two weeks old, read what changed, then

- piped script: download the new versioned URL into an empty folder, `sha256sum` it, read it, and put the URL in
  the method and the hash under the same URL in `script_sha256` (the old entry goes: an unused one fails the check);
- npm / uv / pip / mise: change the `@x.y.z` or `==x.y.z`; an image: change the tag in `tools.json` (method and
  `verify`) and in the compose template together; FreeLLMAPI: `pinned_commit` and its comment in the bootstrap;
- run `python3 install/test_manifest.py` and `python3 install/uninstall_doc.py --write` (the undo lines name images).

### Install steps (bundled tools)

Every step has `do`; any step may add `os` (only on these OSes), `harness: true` (only with `--harness`),
`sudo: true` with `why` (runs as root, says why first).

| `do` | Fields | What happens |
|---|---|---|
| `mkdir` | `path` | create the directory |
| `link` | `src`, `dest` | symlink `dest` → the file or folder in the clone |
| `copy` | `src`, `dest`, `mode`? | copy a file or folder (a `sudo` copy may target an absolute system path) |
| `uv-tool` | `dir` | `uv tool install --editable <dir>` |
| `uv-sync` | `dir`, `python`? | `uv sync --frozen` in `dir`, which makes `<dir>/.venv` |
| `uv-venv` | `venv`, `packages`, `python`? | make that venv with `uv venv`, then `uv pip install` the pinned packages |
| `npm-ci` | `dir` | `npm ci --prefix <dir>`, which makes `<dir>/node_modules` |
| `run` | `cmd`, `why` | run a command or the tool's own installer (from the clone root) |
| `note` | `text` | print it; a step only you can do |

Rules the installer follows for `link` and `copy`:

- Missing parent directories of `dest` are created.
- A destination that already exists and is not already this exact link or copy is **kept** and reported as
  "already present, skipped". Your own files are never replaced on a normal run.
- Steps marked `harness: true` touch agent configuration (`~/.claude`, `~/.codex`, `~/.claude.json`, global git
  settings). They run only with `--harness`; then each existing destination is first backed up to
  `<file>.bak-<timestamp>` beside it. `test_manifest.py` fails any step that writes there without the flag.
- Agent instruction files and `settings.json` are not install steps: profiles list them under `rules`, and the
  installer merges them (backup first), never replaces them. That includes the hook wiring: no step runs a
  tool's own hook installer (handoff-v2's `handoff.install --apply` resolves its path into this clone and would
  wire every handoff hook a second time next to the rule's `~/AI_Projects` ones).
- A `run` step that changes a shared setting checks it first and keeps a value someone else set (the guard's
  `core.hooksPath` step stops with a message instead of replacing yours).

### The workspace layout

The kit's hooks and timers name paths under `~/AI_Projects` (for example
`%h/AI_Projects/_tools/vault-rag/reindex.sh`). The `workspace` tool creates that tree, and each bundled tool
links its folder there (`~/AI_Projects/_tools/<tool>` → `framework/_tools/<tool>` in this clone), so the units
work without editing. If you already have an `~/AI_Projects` with your own copies, they stay as they are.

## Adding a tool

1. Add the entry to `tools.json` (third-party: a method per OS where one exists, plus `manual` with a URL).
2. Add its `id` to the `tools` list of each profile that should get it, and the profile name to its `profiles`.
3. Run `python3 install/test_manifest.py` and `python3 profiles/test_profiles.py`.
4. Run `python3 install/uninstall_doc.py --write` to regenerate the undo lines in `uninstall.md` (CI fails with "generated block is stale" until you do), then `python3 install/uninstall_doc.py` to check it.
