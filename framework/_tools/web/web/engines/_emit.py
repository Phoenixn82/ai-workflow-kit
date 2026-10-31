"""One-line JSON stdout contract shared by every engine script."""
import importlib.metadata as md
import json
import sys

CAP = 5_000_000


def version(pkg: str) -> str | None:
    try:
        return f"{pkg} {md.version(pkg)}"
    except md.PackageNotFoundError:
        return None


def emit(status=None, final_url=None, html="", markdown=None, headers=None, error=None, version=None, rung=None):
    rec = dict(status=status, final_url=final_url, html=(html or "")[:CAP],
               markdown=markdown[:CAP] if markdown else None, headers={k: str(v) for k, v in dict(headers or {}).items()},
               error=error, version=version, rung=rung)
    sys.stdout.write("\n" + json.dumps(rec, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(fn):
    url = sys.argv[1]
    try:
        emit(**fn(url))
    except Exception as e:  # engine failures become records, never crashes
        emit(error=f"{type(e).__name__}: {e}"[:500])
