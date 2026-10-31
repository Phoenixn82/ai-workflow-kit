"""Paths, constants and small filesystem primitives shared by the pipeline.

Everything under STATE is machine-local and deliberately outside the synced vault:
unreviewed transcript-derived text must never be git-pushed (PLAN.md F.4, F.5).
"""
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # pipeline/handoff-v2
# A test module that imports handoff before tests/test_gates.py sets the temp paths
# would write fixtures into the real vault and state.
import sys as _sys
if "unittest" in _sys.modules and not (os.environ.get("HANDOFF_STATE") and os.environ.get("HANDOFF_VAULT")):
    raise RuntimeError("handoff imported under unittest without HANDOFF_STATE/HANDOFF_VAULT: "
                       "import tests.test_gates first")
STATE = Path(os.environ.get("HANDOFF_STATE", str(ROOT / "state")))
QUEUE = STATE / "queue"
RESULTS = STATE / "results"
ARCHIVE = STATE / "archive"                            # unacked work, never expired
QUARANTINE = STATE / "quarantine"                      # admission overflow, re-admitted FIFO
CANDIDATES = STATE / "candidates"                      # quarantined lessons, machine-local
PENDING = STATE / "pending"                            # redacted prefilter text awaiting thor
LOGS = STATE / "logs"
LOCKS = STATE / "locks"
OUTBOX = STATE / "outbox"                              # durable sync intents

TRANSCRIPTS_STATE = STATE / "transcripts.json"
COSTS = STATE / "costs.jsonl"                          # one line per Claude distill call
PROJECT_MAP = ROOT / "project-map.json"

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
VAULT = Path(os.environ.get("HANDOFF_VAULT",
                            str(Path.home() / "AI_Projects" / "_system" / "second-brain")))
PROJECTS_DIR = VAULT / "Projects"
UNSORTED_SLUG = "_unsorted"
# Rules already on file: a lesson these cover is dropped before it becomes a candidate.
RULE_FILES = [Path.home() / ".claude" / "CLAUDE.md", VAULT / "harness-memory" / "MEMORY.md"]


def lesson_files() -> list:
    """Live lessons.md plus the rotated year archives (vault-lint rotate), so
    rotated [candidate] lessons stay reviewable."""
    return sorted([*PROJECTS_DIR.glob("*/lessons.md"), *PROJECTS_DIR.glob("*/_archive/lessons-*.md")])


def lesson_project(path: Path) -> str:
    return path.parent.parent.name if path.parent.name == "_archive" else path.parent.name

KIT_HOSTS = Path(os.environ.get("KIT_HOSTS_ENV") or Path.home() / ".config" / "ai-workflow-kit" / "hosts.env")


def hosts_env(key: str) -> str:
    """KEY from the environment, else from the kit's hosts.env (last KEY=value wins); "" when missing."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        vals = [v.strip().strip("'\"") for k, _, v in (ln.partition("=") for ln in KIT_HOSTS.read_text().splitlines())
                if k.strip() == key]
    except OSError:
        return ""
    return vals[-1] if vals else ""


# thor's GPU Ollama: HANDOFF_OLLAMA_URL, else THOR_OLLAMA_URL (environment or hosts.env). "" = not configured: the
# Ollama path then reports ThorUnavailable(OLLAMA_UNSET) and jobs defer, as when thor is down.
OLLAMA_URL = (os.environ.get("HANDOFF_OLLAMA_URL") or hosts_env("THOR_OLLAMA_URL")).rstrip("/")
OLLAMA_UNSET = f"no Ollama host: set THOR_OLLAMA_URL in {KIT_HOSTS} (or HANDOFF_OLLAMA_URL)"
# heimdall's vault-embedding Ollama (vault-rag's EMBED_URL, the private-data lane): RESUME merges use it to
# spot reworded duplicates. "off" or no host = lexical de-dupe only. A bare host gets Ollama's http + :11434.
EMBED_URL = (os.environ.get("HANDOFF_EMBED_URL") or hosts_env("EMBED_URL")).rstrip("/")
if EMBED_URL and EMBED_URL != "off":
    EMBED_URL = ("" if "://" in EMBED_URL else "http://") + EMBED_URL
    EMBED_URL += "" if EMBED_URL.rsplit(":", 1)[-1].isdigit() else ":11434"
# qwen3.8:27b without its vision projector: pick the largest Qwen that fits
# your GPUs.
MODEL = os.environ.get("HANDOFF_MODEL", "qwen3.8-text:27b")
# Tried when MODEL times out, errors, or returns unparseable JSON. Never removed
# from thor: it fits one GPU and is the floor the pipeline can always reach.
FALLBACK_MODEL = os.environ.get("HANDOFF_FALLBACK_MODEL", "qwen3:8b")
MODEL_TIMEOUT = int(os.environ.get("HANDOFF_MODEL_TIMEOUT", "900"))
FALLBACK_TIMEOUT = 300
# Layers to force onto the GPUs for MODEL. Ollama keeps some headroom per card and
# can park part of a 27B on the CPU; forcing all layers can fit it entirely. If a load
# fails (another job holds a card) the request errors and falls back to FALLBACK_MODEL.
# 0 = let Ollama fit.
PRIMARY_NUM_GPU = int(os.environ.get("HANDOFF_NUM_GPU", "99"))
# Warm across one drain batch, then released: the drain sends keep_alive 0 when
# the queue empties, so thor holds no model memory while idle (the user,
# 2026-09-28: "not hold large amounts of RAM when it could be used").
KEEP_ALIVE = "10m"

# Which backend distils a session (the user 2026-10-12: "maybe it would make more
# sense to use haiku for that now"). a local 27B is slower per job and ties up
# GPU memory.
#   auto   (default) Claude first, via the claude.distill lane in lanes.json;
#          thor's Ollama path on any Claude failure (exit code, is_error, usage or
#          rate limit, no network, timeout, bad output after one retry)
#   claude Claude only; a Claude failure defers the job like an unreachable thor
#   ollama thor only: the behaviour before 2026-10-12
# To switch back for good, change the default below: drains are also spawned by
# Codex's hooks (~/.codex/hooks.json), which never see ~/.claude/settings.json env.
DISTILLER = os.environ.get("HANDOFF_DISTILLER", "auto").strip().lower()
LANES = Path(os.environ.get("HANDOFF_LANES",
                            str(Path.home() / "AI_Projects" / "_system" / "lanes.json")))
CLAUDE_LANE = "claude.distill"
CLAUDE_BIN = os.environ.get("HANDOFF_CLAUDE_BIN", "claude")
CLAUDE_TIMEOUT = int(os.environ.get("HANDOFF_CLAUDE_TIMEOUT", "300"))
# Prefiltered transcript per Claude call. 150K chars is ~45K tokens at the
# calibrated ~3.3 bytes/token (75K even at a dense 2): under the 100K-token line
# where Haiku's long-context price starts. A longer segment is cut into several calls (the
# chunking v2 already does for thor), so nothing is dropped.
CLAUDE_CHUNK_BYTES = 150_000

# Budgets. Token caps are enforced by measurement (PLAN.md D), these are the
# conservative byte ceilings that bound work before any model is contacted.
MAX_PREFILTER_BYTES = 60_000
MAX_MODEL_CTX = int(os.environ.get("HANDOFF_CTX", "20480"))
MODEL_OUTPUT_HEADROOM = 1_024
MAP_OUTPUT = 3_600                     # tokens per chunk's notes; the 27B writes 2-3K and hit 3,000 on dense chunks
SYNTH_OUTPUT = 2_500                   # tokens for the session synthesis
BRIEF_MAX_BYTES = 3_600                # ~1200 tokens at a conservative 3 bytes/token
QUEUE_ADMISSION_CEILING = 200
ACKED_RETENTION_DAYS = 14              # applies to ACKED work only (PLAN.md I)

for _d in (QUEUE, RESULTS, ARCHIVE, QUARANTINE, CANDIDATES, PENDING, LOGS, LOCKS, OUTBOX):
    _d.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, data: str) -> None:
    """Write via temp file + rename on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True))
