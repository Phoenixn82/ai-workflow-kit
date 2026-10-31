"""Lane health check (claude/codex/gemini/freellm).

Spec: PHASE_5_DISPATCH.md § 3.1
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"command not found"


def _check_claude() -> dict:
    return {"status": "healthy", "version": "running", "latency_ms": 0, "notes": "current process"}


def _check_codex() -> dict:
    exe = shutil.which("codex")
    if not exe:
        return {"status": "down", "version": None, "latency_ms": None, "notes": "codex CLI not found"}
    t0 = time.time()
    rc, out, err = _run([exe, "--version"], 5.0)
    if rc != 0:
        return {"status": "down", "version": None, "latency_ms": None, "notes": f"version probe failed: {err.strip()[:80]}"}
    version = out.strip().splitlines()[0] if out else "unknown"
    latency = int((time.time() - t0) * 1000)
    return {"status": "healthy", "version": version, "latency_ms": latency, "notes": "version probe ok"}


def _check_gemini() -> dict:
    exe = shutil.which("gemini")
    if not exe:
        return {"status": "down", "version": None, "latency_ms": None, "notes": "gemini CLI not found"}
    t0 = time.time()
    rc, out, err = _run([exe, "--version"], 5.0)
    if rc != 0:
        return {"status": "down", "version": None, "latency_ms": None, "notes": f"version probe failed: {err.strip()[:80]}"}
    version = out.strip().splitlines()[0] if out else "unknown"
    latency = int((time.time() - t0) * 1000)
    return {"status": "healthy", "version": version, "latency_ms": latency, "notes": "version probe ok"}


def _check_freellm() -> dict:
    t0 = time.time()
    try:
        req = urllib.request.Request("http://127.0.0.1:3001/api/ping")  # /v1/models needs the key since v0.13 (401)
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            latency = int((time.time() - t0) * 1000)
            if resp.status == 200:
                return {"status": "healthy", "version": "running", "latency_ms": latency, "notes": "200 from /api/ping"}
            return {"status": "degraded", "version": None, "latency_ms": latency, "notes": f"status {resp.status}"}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return {"status": "down", "version": None, "latency_ms": None, "notes": f"no response on 3001: {str(e)[:80]}"}


CHECKS = {
    "claude": _check_claude,
    "codex": _check_codex,
    "gemini": _check_gemini,
    "freellm": _check_freellm,
}


def _emit_table(results: dict) -> str:
    rows = ["lane     status   version          latency  notes",
            "-------  -------  ---------------  -------  -----"]
    for lane, info in results.items():
        rows.append(
            f"{lane:<7}  {info['status']:<7}  {(info['version'] or '-')[:15]:<15}  "
            f"{(str(info['latency_ms']) + 'ms' if info['latency_ms'] is not None else '-'):<7}  {info['notes']}"
        )
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane", choices=["claude", "codex", "gemini", "freellm", "all"], default="all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    lanes = list(CHECKS.keys()) if args.lane == "all" else [args.lane]
    results = {lane: CHECKS[lane]() for lane in lanes}

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(_emit_table(results))

    all_healthy = all(r["status"] == "healthy" for r in results.values())
    return 0 if all_healthy else 1


if __name__ == "__main__":
    sys.exit(main())
