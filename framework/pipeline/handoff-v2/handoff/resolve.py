"""Project resolution from what a session actually touched (PLAN.md J, rev 2026-09-28).

Two failures shaped this module:
  * ai-memory filed most observations into one bucket called "vault".
  * v1 routed by the session's START folder. The user starts almost every session
    at ~/AI_Projects, which was mapped to workflow, so most jobs
    (example-project-c, fleet, side projects...) landed there.

So routing scores EVIDENCE per project: files edited, paths in commands, ssh
targets, the vault project folder touched, and the user's own words. The catalog
(project-map.json) is explicit and allowlisted; nothing is inferred from a
directory name alone. When nothing scores, the answer is _unsorted (or, in the
distiller, the model picks from the catalog under a grammar-constrained enum).
"""
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import config as C

_doc_cache: dict | None = None

# Edits are the work; reads and ssh targets only say where the work happened.
# A session started INSIDE a project folder is the user's own choice of project,
# so that cwd is strong evidence (the bare ~/AI_Projects root maps to nothing).
W_EDIT, W_VAULT_EDIT, W_KEYWORD, W_BASH, W_CWD, W_HOST, W_READ = 5, 5, 3, 2, 10, 1, 1
CAP_BASH, CAP_HOST, CAP_READ = 10, 3, 5
MIN_EVIDENCE = 2            # one path in one command is real evidence; less is noise
CANDIDATE_SHARE = 0.2       # a secondary project needs 20% of the top score

VAULT_PROJECTS = "_system/second-brain/Projects/"


def _expand(p: str) -> str:
    return os.path.expanduser(p).rstrip("/") if not p.endswith("*") else os.path.expanduser(p)


def _doc() -> dict:
    global _doc_cache
    if _doc_cache is None:
        _doc_cache = C.read_json(C.PROJECT_MAP, default={}) or {}
    return _doc_cache


def catalog() -> dict:
    return _doc().get("projects", {})


def allowlist() -> set[str]:
    slugs = set(catalog())
    if C.PROJECTS_DIR.is_dir():
        slugs |= {p.name for p in C.PROJECTS_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")}
    return slugs | {C.UNSORTED_SLUG}


def _is_noise(path: str) -> bool:
    for n in _doc().get("noise", []):
        n = os.path.expanduser(n)
        if path == n.rstrip("/") or path.startswith(n if n.endswith("/") else n.rstrip("/") + "/") \
                or (not n.endswith("/") and path.startswith(n) and path[len(n):len(n) + 1] in ("", "/", ".")):
            return True
    return False


def _under(path: str, prefix: str) -> bool:
    if prefix.endswith("*"):
        return path.startswith(prefix[:-1])
    return path == prefix or path.startswith(prefix + "/")


def _match(path: str) -> str | None:
    """Slug owning a path, by the longest catalog prefix. Noise and unknown
    project directories return None: an unmapped subproject must never be filed
    under an ancestor (example-project-c once resolved to workflow that way)."""
    if not path:
        return None
    path = os.path.expanduser(str(path)).rstrip("/")
    if _is_noise(path):
        return None
    i = path.find(VAULT_PROJECTS)
    if i >= 0:
        slug = path[i + len(VAULT_PROJECTS):].split("/")[0]
        return slug if slug in allowlist() and not slug.startswith("_") else None
    best, best_len = None, -1
    for slug, proj in catalog().items():
        for pref in proj.get("paths", []):
            pref = _expand(pref)
            if _under(path, pref) and len(pref) > best_len:
                best, best_len = slug, len(pref)
    return best


def _host_slug(host: str) -> str | None:
    for slug, proj in catalog().items():
        if host.lower() in (h.lower() for h in proj.get("hosts", [])):
            return slug
    return None


_kw_cache: list | None = None


def _keywords() -> list[tuple[re.Pattern, str, str]]:
    global _kw_cache
    if _kw_cache is None:
        _kw_cache = [(re.compile(r"(?<![\w'])" + re.escape(k.lower()) + r"(?![\w])"), k, slug)
                     for slug, proj in catalog().items() for k in proj.get("keywords", [])]
    return _kw_cache


def rank(signals: dict) -> list[tuple[str, float, list[str]]]:
    """[(slug, score, evidence)] best first; empty when nothing scores."""
    score: Counter = Counter()
    why: dict[str, list[str]] = defaultdict(list)
    bash_pts: Counter = Counter()
    host_pts: Counter = Counter()
    read_pts: Counter = Counter()

    for path in signals.get("edits", {}):
        slug = _match(path)
        if slug:
            score[slug] += W_VAULT_EDIT if VAULT_PROJECTS in path else W_EDIT
            why[slug].append(f"edit {Path(path).name}")
    for path, n in signals.get("bash_paths", {}).items():
        slug = _match(path)
        if slug:
            bash_pts[slug] += W_BASH * n
    for path, n in signals.get("reads", {}).items():
        slug = _match(path)
        if slug:
            read_pts[slug] += W_READ * n
    for host, n in signals.get("hosts", {}).items():
        slug = _host_slug(host)
        if slug:
            host_pts[slug] += W_HOST * n
    for slug, pts in bash_pts.items():
        score[slug] += min(CAP_BASH, pts)
        why[slug].append("commands")
    for slug, pts in read_pts.items():
        score[slug] += min(CAP_READ, pts)
    for slug, pts in host_pts.items():
        score[slug] += min(CAP_HOST, pts)
        why[slug].append("ssh")
    cwds = signals.get("cwds") or {}
    if cwds:
        main_cwd = max(cwds, key=cwds.get)
        slug = _match(main_cwd)
        if slug:
            score[slug] += W_CWD
            why[slug].append("cwd")
    text = (signals.get("human_text") or "").lower()
    if text:
        seen = set()
        for rx, kw, slug in _keywords():
            if (kw, slug) not in seen and rx.search(text):
                seen.add((kw, slug))
                score[slug] += W_KEYWORD
                why[slug].append(f'said "{kw}"')

    allowed = allowlist()
    out = [(s, float(v), why[s]) for s, v in score.items() if v > 0 and s in allowed]
    out.sort(key=lambda x: (-x[1], x[0]))
    if out and out[0][1] < MIN_EVIDENCE:
        return []
    return out


def candidates(signals: dict, k: int = 4) -> list[str]:
    """Projects that plausibly own part of the session, primary first. The
    distiller may attribute each item only to one of these."""
    ranked = rank(signals)
    if not ranked:
        return []
    floor = max(MIN_EVIDENCE, ranked[0][1] * CANDIDATE_SHARE)
    return [s for s, v, _ in ranked if v >= floor][:k]


def in_scope(cwd: str | None) -> bool:
    """Backfill guard: a transcript outside AI_Projects with no catalog match is
    scratch or home-dir noise and is not worth thor time."""
    if not cwd:
        return False
    cwd = cwd.rstrip("/")
    root = os.path.expanduser("~/AI_Projects")
    return cwd == root or cwd.startswith(root + "/") or _match(cwd) is not None


def cwd_from_records(records: list[dict]) -> str | None:
    """Authoritative cwd, read from the transcript itself.

    The transcript directory name is a lossy encoding - both '/' and '_' become
    '-', so `AI_Projects` and `AI-Projects` are indistinguishable and decoding
    it is unsound. The records carry `cwd` verbatim; use that.
    """
    for rec in records:
        cwd = rec.get("cwd")
        if cwd:
            return cwd
    return None


def resolve(cwd: str | None, edited_paths: list[str] | None = None,
            signals: dict | None = None) -> tuple[str, str]:
    """Return (primary slug, reason). Callers without full signals (seal's slug
    hint, the SessionStart brief lookup) pass only cwd and edits."""
    if signals is None:
        signals = {"edits": Counter(edited_paths or []),
                   "cwds": Counter({cwd: 1}) if cwd else Counter()}
    ranked = rank(signals)
    if not ranked:
        return C.UNSORTED_SLUG, "no project evidence"
    slug, pts, why = ranked[0]
    others = [s for s, _, _ in ranked[1:4]]
    return slug, f"score {pts:g} ({', '.join(why[:4])})" + (f"; also {others}" if others else "")
