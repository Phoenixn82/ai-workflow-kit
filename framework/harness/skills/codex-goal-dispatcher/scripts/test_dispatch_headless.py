#!/usr/bin/env python3
"""Offline check of dispatch.run_headless (review 2026-10-12): it runs `cx long-unattended-build workspace-write`
with CX_TIMEOUT = budget, and cx's own timeout (exit 124) is recorded as status "timeout", not "failed".
A fake `cx` on PATH stands in for Codex. Run: python3 test_dispatch_headless.py"""
import json, os, stat, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fake = d / "bin" / "cx"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\necho "$@" > "$(dirname "$0")/argv"; echo "CX_TIMEOUT=$CX_TIMEOUT" >> "$(dirname "$0")/argv"\n'
                    'echo done > "$4"; exit "${FAKE_RC:-0}"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    for rc, want in ((0, "completed"), (124, "timeout"), (3, "failed")):
        run = d / f"run{rc}"
        run.mkdir()
        env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}", "FAKE_RC": str(rc)}
        got = dispatch.run_headless("codex", d, run, run / "status.json", {}, "seed", 2, env)
        status = json.loads((run / "status.json").read_text())
        argv = (fake.parent / "argv").read_text()
        assert got == rc and status["status"] == want, (rc, got, status)
        assert argv.startswith(f"long-unattended-build workspace-write {run}/last-message.txt seed") and "CX_TIMEOUT=120" in argv, argv
print("ok dispatch headless: cx argv, CX_TIMEOUT=budget, 0 completed / 124 timeout / other failed")
