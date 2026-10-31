"""Pre-computed recall brief. SessionStart does a `cat`, never a search.

This is the whole point of pre-computation: claude-mem v3 auto-injected
everything and caused context pollution, v4 retreated to an 800-token index and
forced manual search. Both failures come from deciding what to inject *at*
session start. We decide at distillation time and inject a finished file.
"""
import datetime as dt
import re
from pathlib import Path

from . import config as C
from . import render, resolve, seal, store

BRIEF_NAME = "_brief.md"


def brief_path(slug: str) -> Path:
    return C.PROJECTS_DIR / slug / BRIEF_NAME


def build(slug: str, repo_fingerprint: dict) -> str:
    """Render the brief for a project and store the fingerprint it was built at."""
    prior = render.parse_resume(slug)
    if repo_fingerprint.get("state") != "ok" and prior.get("repo"):
        repo_fingerprint = prior["repo"]     # keep the drift check armed for secondary projects
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # An actionable next step needs deterministic backing. `verified` means the
    # referenced work shows up in the repo delta; everything else is recorded but
    # not presented as something to go do (PLAN.md F.2).
    actionable = [i for i in prior["next"] if i.get("evidence_level") == "verified"]
    recorded = [i for i in prior["next"] if i.get("evidence_level") != "verified"]

    heading = f"{slug}" + (f" — {prior['summary']}" if prior.get("summary") else "")
    lines = [
        "---",
        "type: brief",
        f"project: {slug}",
        f"built: {now}",
        f"generation: {prior['generation']}",
        f"repo_head: {repo_fingerprint.get('head', 'unknown')}",
        f"repo_tree: {repo_fingerprint.get('tree_hash', 'unknown')}",
        # Vault schema (second-brain/AGENTS.md). Kept after the drift keys: inject()
        # splits on "---", so one inside the description can only cut the description.
        "status: active",
        f"updated: {now[:10]}",
        f"description: {render._yq(heading, 120)}",
        "---",
        f"## {heading}",
        "_Recorded evidence, not instructions — distilled by a local model from prior transcripts._",
    ]
    m = re.search(r"^last_done:[ \t]*(.*)$", render.resume_path(slug).read_text(errors="replace"), re.M) \
        if render.resume_path(slug).exists() else None
    last = render._unyq(m.group(1)) if m else ""
    if last:
        lines += [f"**Last session:** {last}",
                  f"_Full record: `Projects/{slug}/history.md` · decisions.md · errors.md · lessons.md_"]
    # Pointer block FIRST. This is what a resuming session reconstructs by hand
    # otherwise (median +31K tokens of orientation reads): where the project is,
    # what git state it is in, which files were last touched and how big.
    cwd = prior.get("cwd") or "unknown"
    branch = repo_fingerprint.get("branch", "?")
    head = str(repo_fingerprint.get("head", "unknown"))[:12]
    dirty = " (dirty)" if repo_fingerprint.get("dirty") else ""
    lines += ["", f"**Root:** `{cwd}` · **repo:** `{branch}` @ `{head}`{dirty}"]
    # What this project declares it needs - so a resuming session knows its
    # tools without discovering them (the user: "if the project mentions it only
    # needs X tools, load those").
    if cwd and cwd != "unknown":
        cdir = Path(cwd) / ".claude"
        tools = []
        try:
            import json as _json
            sp = cdir / "settings.json"
            if sp.exists():
                tools += [k.split("@")[0] for k, v in (_json.loads(sp.read_text()).get("enabledPlugins") or {}).items() if v]
            tools += [p.stem for p in (cdir / "agents").glob("*.md")] if (cdir / "agents").is_dir() else []
            tools += [p.name for p in (cdir / "skills").iterdir() if p.is_dir()] if (cdir / "skills").is_dir() else []
        except Exception:
            tools = []
        if tools:
            lines += [f"**Tools:** {', '.join(sorted(set(tools)))}"]
    notes = C.PROJECTS_DIR / slug / "NOTES.md"
    if notes.exists():
        n = sum(1 for ln in notes.read_text(errors="replace").splitlines() if ln.startswith("- "))
        lines += [f"**Notes:** `Projects/{slug}/NOTES.md` ({n} entries, not auto-loaded — Grep it)"]
    if prior.get("touched"):
        lines += ["**Last touched:**"]
        lines += [f"- `{t['path']}` ({t['bytes'] if t['bytes'] is not None else '?'} B)"
                  for t in prior["touched"][:8]]
    if actionable:
        lines += ["", "**Next (evidence-backed):**"]
        lines += [f"- {i['text']}" for i in actionable[:5]]
    if recorded:
        lines += ["", "**Recorded next steps (unverified - confirm before acting):**"]
        lines += [f"- {i['text']}" for i in recorded[:5]]
    # Nominations came from the main agent's own judgement, not a local model, so
    # they are the one class of item safe to present plainly rather than hedged.
    if prior.get("noted"):
        lines += ["", "**Noted (judged worth keeping):**"]
        lines += [f"- {i['text']}" for i in prior["noted"][:6]]
    if prior["blockers"]:
        lines += ["", "**Blocked:**"]
        lines += [f"- {i['text']}" for i in prior["blockers"][:5]]
    if not (actionable or recorded or prior["blockers"] or prior.get("noted")):
        lines += ["", "_No open items recorded._"]

    text = "\n".join(lines) + "\n"
    if len(text.encode()) > C.BRIEF_MAX_BYTES:
        text = text.encode()[:C.BRIEF_MAX_BYTES].decode("utf-8", "ignore")
        text += "\n_(brief truncated at byte ceiling)_\n"
    C.atomic_write(brief_path(slug), text)
    return text


def _frontmatter(text: str) -> dict:
    out = {}
    if not text.startswith("---"):
        return out
    for line in text.split("---", 2)[1].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def pending_for(slug: str) -> int:
    n = 0
    for path in C.QUEUE.glob("*.json"):
        job = C.read_json(path)
        if job and job.get("slug_hint") == slug:
            n += 1
    return n


def inject(cwd: str | None, source: str) -> str:
    """What SessionStart prints. Returns '' when nothing should be injected."""
    # Re-injecting on every compaction would repeat the brief endlessly.
    if source == "compact":
        return ""

    slug, _why = resolve.resolve(cwd, [])
    if slug == C.UNSORTED_SLUG:
        return ""          # a session at ~/AI_Projects has no project yet; inject nothing
    path = brief_path(slug)
    if not path.exists():
        return ""

    text = path.read_text(encoding="utf-8", errors="replace")
    fm = _frontmatter(text)

    # External drift: rebuilding from the same stale RESUME cannot learn about
    # code that changed outside the pipeline, so withhold and say so rather than
    # present a confidently stale brief (PLAN.md K).
    current = seal.repo_fingerprint(cwd)
    # tree_hash is sha(status+diff) and is the SAME constant for every clean
    # repo, so on its own it cannot see a commit-and-walk-away. Compare HEAD too.
    if current.get("state") == "ok" and fm.get("repo_tree") not in ("unknown", None):
        head_changed = fm.get("repo_head") not in ("unknown", None) and current.get("head") != fm.get("repo_head")
        if current.get("tree_hash") != fm.get("repo_tree") or head_changed:
            return (f"## Resume: {slug}\n\n_Brief withheld: the project repository changed "
                    f"outside this pipeline since the brief was built "
                    f"(generation {fm.get('generation', '?')}). Re-distil to refresh._\n")
    if current.get("state") != "ok" and fm.get("repo_head") not in ("unknown", None):
        return (f"## Resume: {slug}\n\n_Brief withheld: repository fingerprint unavailable, "
                f"so freshness cannot be confirmed._\n")

    n = pending_for(slug)
    if n:
        text += f"\n_resume: pending - {n} segment(s) awaiting distillation._\n"
    return text


def diagnostic() -> str:
    """One line, always printed, so a broken pipeline is visible immediately."""
    queue = store.queue_depth()
    # admission overflow (quarantine/*.json) plus failed jobs (quarantine/failed/*.json)
    quarantined = len(list(C.QUARANTINE.glob("*.json"))) + len(list(store.FAILED.glob("*.json")))
    oldest = "-"
    files = sorted(C.QUEUE.glob("*.json"))
    if files:
        import time
        age = time.time() - files[0].stat().st_mtime
        oldest = f"{age / 3600:.1f}h" if age > 3600 else f"{age / 60:.0f}m"
    # Sessions filed under _unsorted (its journal lines); counting its files read 8 forever.
    uj = C.PROJECTS_DIR / C.UNSORTED_SLUG / "journal.ndjson"
    unsorted = sum(1 for ln in uj.read_text(encoding="utf-8", errors="replace").splitlines()
                   if ln.strip()) if uj.exists() else 0
    cands = 0
    for p in C.lesson_files():
        try:
            cands += p.read_text(encoding="utf-8", errors="replace").count("\n- [candidate]")
        except OSError:
            pass
    paused = " PAUSED" if (C.STATE / "PAUSED").exists() else ""
    return (f"[handoff] queue={queue}{paused} oldest={oldest} quarantined={quarantined} "
            f"unsorted={unsorted} lessons-to-review={cands}")
