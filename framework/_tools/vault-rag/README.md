# vault-rag: retrieve vault notes by query at prompt time

Hybrid search over the second-brain vault: sqlite-vec cosine KNN (nomic-embed-text on heimdall's always-on CPU Ollama, `heimdall/`) plus FTS5/BM25, fused with RRF.
A UserPromptSubmit hook gives Claude Code and Codex up to 3 relevant chunks, so agents stop reading title indexes. Eval: `EVAL-2026-10-12.md`.

**Indexed:** every `.md` under `_system/second-brain/` (harness-memory/ included, once) except `log-archive/`, `Blog/`, root `log.md`, the raw `_archive/log.md`, and the `_archive/` dirs outside the archive tier (`Actions/_archive`, `harness-memory/_archive`: superseded rules). Archived notes (`_archive/`, `Projects/<p>/_archive/`) stay indexed, per the vault AGENTS.md schema
and any hidden path (`.git .obsidian .sync`, `.claude-codex-log.md`, `.codex-session-changes/`). Rule files: `~/.claude/CLAUDE.md`,
`~/AI_Projects/CLAUDE.md`, `~/AI_Projects/AGENTS.md`. DB: `~/.local/state/vault-rag/vault_index.db` (WAL; never in the vault).

| File | What it does |
|---|---|
| `vault_rag.py` | `index [--rebuild] [--dry-run]`, `query`, `stats`, `--self-test` (offline: temp vault, fake embeddings) |
| `vault-recall` | `vault-recall "<q>" [-k N] [--json] [--fts-only]`; linked into `~/.local/bin`. If a 300 ms TCP probe of the embed host's :11434 fails, or the embed takes over 3 s, it answers FTS-only. Host: `VAULT_RAG_OLLAMA_HOST`, else `EMBED_URL` from the environment or `~/.config/ai-workflow-kit/hosts.env` (`KIT_HOSTS_ENV` overrides the path; the user's points at heimdall's embed server). Empty means unset; with no host at all queries answer FTS-only (the CLI says "set EMBED_URL") and `index` aborts with the index unchanged. A value with no scheme gets Ollama's `http://` and `:11434`, e.g. `thor`; deliberately not `OLLAMA_HOST`, which points at thor for the big models |
| `hook-user-prompt.sh` | the prompt hook (`--self-test`). Skips prompts under 6 words, starting with `/` or a harness tag (`<task-notification>` ...), or over 2,000 chars. Injects strong, not-yet-seen hits from the top 3, minus raw layers (`history.md`, `journal.ndjson`, `sessions/`, `_archive/`; the CLI still returns them), snippets cut to 120 chars, whole JSON output ≤1,500 chars; any error means no output and exit 0 |
| `eval_hook.py` | live hook eval: 16 on-topic queries (recall gate `--min-recall 13`), 15 off-topic; `--replay N` pipes the last N real prompts through the hook and prints aggregates only. `--hook` compares another copy |
| `reindex.sh` | timer entry point: HALT check, `preflight.py` (ceiling 0 = kill-switch), prune `seen-*` older than 7 days, dry-run, `thor-up` only if chunks need embedding and the embed host is thor, incremental index (`--dry-run`, `--self-test`). Embedded batches go to `embed-cache.jsonl` beside the DB first, so a run killed at the 2 h cap resumes next time. The embedding fingerprint (weights blob digest + model, dim, prefixes) is stored in the index and on the cache's first line: a changed model makes the incremental run abort until `vault_rag.py index --rebuild`, and never reuses vectors from another model. Each embedded batch is sealed in the cache by a fresh fingerprint check, and a resume reuses only sealed vectors (the unsealed tail of a killed run is cut off); the commit re-reads the stored fingerprint, so a concurrent `--rebuild` with another model makes the run abort |
| `systemd/vault-rag-reindex.{service,timer}` | daily at 02:40 Etc/UTC, Persistent, Nice=15, idle IO |
| `heimdall/compose.yaml`, `heimdall/nomic-embed-text.Modelfile` | the embed server: `ollama/ollama:0.33.3` container `ollama-embed` on heimdall (`~/services/ollama-embed/`), host network bound to the tailnet IP only, 3 CPUs, model resident (`KEEP_ALIVE -1`). Use the same weights blob as any other embed host so vectors match the stored index. Its bind address is `${HEIMDALL_HOST}` (Compose refuses to start without it), so apply it with `docker compose --env-file ~/.config/ai-workflow-kit/hosts.env -f ~/services/ollama-embed/compose.yaml up -d`. Kill: `docker stop ollama-embed` on heimdall |
| `install.sh` | idempotent: makes `.venv` beside `vault_rag.py` when it is missing (`uv venv` + `sqlite-vec==0.1.9`), links the CLI and the units, daemon-reload; enables the timer only with `--enable-timer` (`--dry-run`, `--self-test`). It does **not** register hooks |

A hit is "strong" if its cosine sim is ≥ 0.70 (hybrid), or, in FTS-only mode, if it contains at least 60 % of the query terms and at least 3 of them.
The hook injects only strong hits. Dedupe: `~/.local/state/vault-rag/seen-<session_id>`.
Runtime: `.venv` (uv, sqlite-vec 0.1.9) is bound to system Python 3.14. After an Arch Python minor upgrade, rebuild it (`rm -rf .venv && ./install.sh`). `vault-recall` and `reindex.sh` pick their Python as `$VAULT_RAG_PY`, else that `.venv`, else `uv run --with sqlite-vec==0.1.9` (slower first call). The reindex unit reads `EnvironmentFile=-%h/.config/ai-workflow-kit/hosts.env` (EMBED_URL, THOR_HOST).

## Orchestrator: wire-up snippets (not applied)
`~/.claude/settings.json`: append to `hooks.UserPromptSubmit[0].hooks` (matcher `""`):
```json
{"type": "command", "command": "bash $HOME/AI_Projects/_tools/vault-rag/hook-user-prompt.sh", "timeout": 8}
```
`~/.codex/hooks.json`: add a `UserPromptSubmit` key (Codex's trust state records it as `user_prompt_submit`). Codex asks you to trust the new hook (`/hooks`):
```json
"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "bash $HOME/AI_Projects/_tools/vault-rag/hook-user-prompt.sh", "timeout": 8}]}]
```
REGISTRY.md row: `| vault-rag-reindex: incremental vault index for prompt-time retrieval (added 2026-10-12) | 02:40 daily on **odin** (user timer, Persistent) | local Ollama embeddings on heimdall, zero AI tokens | changed vault .md → ~/.local/state/vault-rag/vault_index.db | systemctl --user disable --now vault-rag-reindex.timer |`
`_system/tool-parity/registry.json` entry `vault-rag`: it lists an MCP that does not exist. Set `"commands": ["vault-recall"], "mcp_names": []`
and the hint "run vault-recall '<query>'". `DECISION_MAP.md` line 16 can now say `vault-recall "<query>"` (hook does it automatically).

## Caveats
- **Embed host:** run it on the always-on box (heimdall), not a GPU box that suspends when idle (queries fall back to FTS
  while it sleeps) or queues embeds behind big chat models. A small CPU-only embed model answers a hybrid query in well
  under a second; a whole-vault re-embed on a small CPU takes a couple of hours. Reindex batches are 2 chunks with `OLLAMA_NUM_PARALLEL 4`, so a hook query during a
  reindex still finds a free slot and stays hybrid; large batches push such queries to the 3 s FTS fallback.
- Chunking is unchanged: headings, then 1,500-char windows. Markdown-aware chunking, a reranker and an index manifest are deferred.

## qmd (tobi/qmd), future candidate, not installed
Revisit when all of these hold: the exact-scan slowness at ~1k files / ~8k chunks is fixed; the daemon use-after-free is fixed; it kills its
children on timeout (no orphans); it runs with remote Ollama embeddings or CPU-only without loading the workstation; it gives JSON output with a score usable for gating;
and it beats the current hybrid search on your own eval queries.
