"""Read-lane engines: how to run each rung, pinned to the versions the Phase A bake-off measured
(bakeoff/runs/2026-09-30/read/<engine>/*/*.json "version"). Each script prints one JSON line (see _emit.py)."""
from pathlib import Path

E = Path(__file__).parent
# Scrapling runs in its own uv tool env (installed with the [all] extra, browsers via `scrapling install`).
SCRAPLING_PY = str(Path.home() / ".local/share/uv/tools/scrapling/bin/python")
SCRAPLING = "scrapling[all]==0.4.15"
ZENDRIVER = "zendriver==0.17.0"
MARKDOWNIFY = "markdownify==1.2.3"   # _md.py's converter, same version as this project's lock


def uvpy(*pkgs: str) -> list[str]:
    argv = ["uv", "run", "--no-project", "--quiet", "--python", "3.12"]
    for p in (*pkgs, MARKDOWNIFY):
        argv += ["--with", p]
    return argv + ["python"]


SCRAPLING_SETUP = f"uv tool install '{SCRAPLING}' && scrapling install"

ENGINES = {
    "scrapling-get":     dict(argv=[SCRAPLING_PY, str(E / "scrapling_get.py")], headed=False, timeout=60, setup=SCRAPLING_SETUP),
    "scrapling-fetch":   dict(argv=[SCRAPLING_PY, str(E / "scrapling_fetch.py")], headed=True, timeout=120, setup=SCRAPLING_SETUP),
    "scrapling-stealth": dict(argv=[SCRAPLING_PY, str(E / "scrapling_stealth.py")], headed=True, timeout=150, setup=SCRAPLING_SETUP),
    # Drives the system /usr/bin/chromium; the uv env is fetched and cached on first use.
    "zendriver":         dict(argv=uvpy(ZENDRIVER) + [str(E / "zendriver_.py")], headed=True, timeout=120, setup=None),
}
