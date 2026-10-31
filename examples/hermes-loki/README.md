# Quarantined-box Hermes: example files

Templates for a second [Hermes](../../docs/hermes.md#a-second-hermes-on-a-quarantined-box) that lives on the
one-way **loki** box and borrows the router through a reverse tunnel from your cockpit. The install steps, checks
and undo are in that doc section; this folder holds the files it copies.

| File | Goes to (on the box) | What it is |
|---|---|---|
| `SOUL.md` | `~/.hermes/SOUL.md` | the brief: permissive answering, the lock rules, no cameras, a short "this machine" section to fill in |
| `config-block.yaml` | appended to `~/.hermes/config.yaml` | router at `127.0.0.1:3002`, the public-tier pools, fallbacks, aliases, side jobs, the camera hook |

The tools themselves are in [`framework/_tools/hermes-loki/`](../../framework/_tools/hermes-loki/):
`loki-llm-gate` and its user unit run on the cockpit; `desk-shot` and `desk-gui` run on the box if it has a desktop
VM. The launcher is a shell function, shown in the doc.

Check the gate before you start:

```sh
framework/_tools/hermes-loki/loki-llm-gate --self-test     # loki-llm-gate self-test: ok
```

Edit the "This machine" and "What you're for" parts of `SOUL.md` to match your box, and keep the rest. The pool
names in `config-block.yaml` must exist on your router: `lane chains sync --apply` builds them from
`framework/_tools/lanes/config/chains.yaml`.
