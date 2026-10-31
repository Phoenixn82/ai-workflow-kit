#!/usr/bin/env python3
"""
codex-goal-dispatcher — spawn Codex with a pre-loaded /goal prompt.

Promoted out of _archive to the live skill dir on 2026-06-13 (was a runpy shim into
_archive — a single point of failure if the archive is ever cleaned). Two critical
bridge fixes applied at promotion:
  1. Resolve the REAL codex binary (config CODEX_CLI_PATH / newest native build) instead
     of bare `codex`. The PATH `codex` is a stale npm shim (0.128) that cannot parse the
     current ~/.codex/config.toml (service_tier="default", model="gpt-5.5", plugins table)
     — it errors on config load and EXITS 0, so a dispatch looked "spawned" but did nothing.
  2. Detect liveness via codex.exe (the modern native binary) not node.exe+codex.js (the
     old npm packaging) — otherwise node_pid was always null and babysit always read "dead",
     endangering the IRON RULE (a healthy run can be misread as stalled).

Inputs:
  --project-dir   absolute path to project root
  --prompt-file   absolute path to filled goal_prompt.md
  --status-file   absolute path where status.json will be written/updated
  --budget-min    budget in minutes (default 180)
  --no-spawn      skip the Windows Terminal spawn (dry run / smoke test)
  --headless      HANDS-FREE: run `codex exec` to completion in-process (no TUI, no
                  keystroke), capturing exit code + stdout/stderr to the run dir and
                  driving status.json running -> completed/failed. Blocks until Codex exits.
  --smoke-test    self-test mode: writes a fake goal-run dir under TEMP, opens
                  Codex with a trivial prompt, cleans up

Side effects (visible/TUI path — the default, for when the user wants to watch):
  1. Resolves the real codex binary and asserts it parses the live config (fail loudly if not)
  2. Writes initial status.json
  3. Opens a new Windows Terminal tab (or new cmd window if wt missing) in the project dir,
     seeded with: "<resolved codex.exe>" --enable goals --dangerously-bypass-approvals-and-sandbox "<seed>"

Side effects (--headless path — fully autonomous, no human interaction):
  1. Resolves the real codex binary and asserts it parses the live config (fail loudly if not)
  2. Writes initial status.json (status="running")
  3. Runs `cx long-unattended-build workspace-write <run>/last-message.txt <seed>` in <project>
     (model + effort from the lanes.json route, CX_TIMEOUT = budget) and BLOCKS until it
     exits. exec is non-interactive (per `codex exec --help`): it runs the goal to completion
     with no `/goal use goal` keystroke. Captures stdout/stderr to <run>/codex-exec.log and
     writes status="completed" (exit 0) or "failed" (nonzero) with the exit code + last message.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

CONFIG_TOML = Path.home() / ".codex" / "config.toml"
ROUTE = "long-unattended-build"  # task class in _system/lanes.json routes; the headless run goes through `cx ROUTE`
CODEX_BIN_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "OpenAI" / "Codex" / "bin"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_claude_session_id() -> str:
    for key in ("CLAUDE_SESSION_ID", "CLAUDECODE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        val = os.environ.get(key, "").strip()
        if val:
            return val

    token_file = Path(tempfile.gettempdir()) / f"claude-session-{os.getppid()}.txt"
    try:
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
    except Exception:
        pass

    token = f"claude-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getppid()}"
    try:
        token_file.write_text(token, encoding="utf-8")
    except Exception:
        pass
    return token


def build_claude_spawn_env(project_dir: Path, claude_session_id: str) -> dict[str, str]:
    env = os.environ.copy()
    env["CLAUDE_SPAWN"] = "1"
    env["CLAUDE_SESSION_ID"] = claude_session_id
    env["CLAUDE_PROJECT"] = str(project_dir)
    return env


def write_status(status_file: Path, payload: dict) -> None:
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def resolve_codex_binary() -> tuple[str, str]:
    """Resolve the codex binary that can actually parse the current config.

    Order: (1) CODEX_CLI_PATH from ~/.codex/config.toml, (2) newest
    %LOCALAPPDATA%/OpenAI/Codex/bin/<hash>/codex.exe by mtime, (3) bare `codex` on PATH
    (last resort — likely the stale npm shim). Returns (binary, how_resolved).
    The hash dir changes per build, so it is NEVER hardcoded.
    """
    # (1) config CODEX_CLI_PATH
    try:
        if CONFIG_TOML.exists():
            text = CONFIG_TOML.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'''(?im)^\s*CODEX_CLI_PATH\s*=\s*["']([^"']+)["']''', text)
            if m:
                p = Path(m.group(1))
                if p.exists():
                    return str(p), "config CODEX_CLI_PATH"
    except Exception:
        pass
    # (2) newest native build under LOCALAPPDATA
    try:
        if CODEX_BIN_ROOT.exists():
            exes = list(CODEX_BIN_ROOT.glob("*/codex.exe"))
            if exes:
                newest = max(exes, key=lambda f: f.stat().st_mtime)
                return str(newest), f"newest build under {CODEX_BIN_ROOT}"
    except Exception:
        pass
    # (3) PATH fallback
    onpath = shutil.which("codex")
    if onpath:
        return onpath, "PATH `codex` (WARNING: may be the stale npm shim)"
    return "codex", "unresolved (bare `codex`)"


def assert_codex_config_ok(codex_bin: str) -> tuple[bool, str]:
    """Run `<codex_bin> features list` and decide if it parsed the config.

    A STALE binary prints `Error: ...config.toml:...: unknown variant ...` AND exits 0,
    so we cannot trust the exit code — we scan combined output for config-load errors.
    Returns (ok, detail).
    """
    try:
        proc = subprocess.run(
            [codex_bin, "features", "list"],
            capture_output=True, text=True, timeout=20,
        )
        blob = (proc.stdout or "") + (proc.stderr or "")
        low = blob.lower()
        if "config.toml" in low and ("error" in low or "unknown variant" in low or "expected" in low):
            first = next((ln for ln in blob.splitlines() if ln.strip()), blob[:160])
            return False, f"config-load error: {first.strip()[:200]}"
        return True, "features list parsed config OK"
    except FileNotFoundError:
        return False, f"binary not found: {codex_bin}"
    except subprocess.TimeoutExpired:
        return True, "features list timed out (binary launched; assuming OK)"
    except Exception as exc:
        return True, f"could not run features list ({exc}); proceeding"


def find_terminal() -> tuple[str, list[str]]:
    if shutil.which("wt"):
        return "wt", []
    if shutil.which("powershell"):
        return "powershell", ["-NoExit", "-Command"]
    return "cmd", ["/k"]


def build_seed_prompt(prompt_file: Path) -> str:
    return (
        f"/goal Execute the plan at {prompt_file}. "
        f"Read the file first to load the verification matrix, then implement every check "
        f"to PASS. Update status.json next to the plan as you go. "
        f"Stop only when every checkbox in the verification section is green or budget hits."
    )


def build_headless_prompt(prompt_file: Path) -> str:
    """Plain (non-slash) instructions for `codex exec`, with the plan INLINED.

    `codex exec` is non-interactive — there is no `/goal` TUI to drive, so the seed is plain
    English. We INLINE the full plan text rather than telling Codex to `read` it from disk: a
    disk read goes through Codex's shell, which on this machine loads the user's PowerShell
    profile + AOS hooks that can emit "Access is denied" noise and make Codex wrongly conclude
    the whole workspace is unreadable, aborting before it does any work. Inlining the plan
    sidesteps that entirely — Codex has the full spec in its context from turn one. It runs through
    `cx long-unattended-build` (see run_headless).
    """
    try:
        plan_text = prompt_file.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as exc:
        plan_text = f"(could not inline plan from {prompt_file}: {exc} — read it yourself)"
    return (
        "You are running a Codex goal autonomously and non-interactively. Implement the plan below "
        "so that every check in its verification section PASSES, then stop. Work entirely from this "
        "prompt — the full plan is inlined here, so you do NOT need to read it from disk first.\n\n"
        "===== GOAL PLAN =====\n"
        f"{plan_text}\n"
        "===== END PLAN =====\n\n"
        "Stop only when every checkbox in the verification section is satisfied (or the budget is hit)."
    )


def run_headless(
    codex_bin: str,
    project_dir: Path,
    run_dir: Path,
    status_file: Path,
    status: dict,
    headless_prompt: str,
    budget_min: int,
    spawn_env: dict[str, str],
) -> int:
    """Run `codex exec` to completion in-process. HANDS-FREE — no terminal, no keystroke.

    Captures combined stdout/stderr to <run_dir>/codex-exec.log and the agent's final message to
    <run_dir>/last-message.txt. Drives status.json: running -> completed (exit 0) / failed (nonzero)
    / timeout (budget exceeded). Returns the process exit code (or 124 on timeout).

    Quoting note: subprocess.run is given an ARGV LIST (not a shell string), so Windows quoting of
    the prompt is handled by Python's CreateProcess argv joining — no .bat/cmd re-parsing trap here.
    The visible/.bat path keeps its own quoting wisdom; this path sidesteps it entirely.
    """
    log_file = run_dir / "codex-exec.log"
    last_msg_file = run_dir / "last-message.txt"

    # cx (the one way Claude dispatches Codex) on task class long-unattended-build: model and effort come from the
    # lanes.json route, stdin is closed, MCP and plugins are off; it runs in cwd=project_dir. The seed is plain
    # instructions with the plan inlined, so the /goal TUI feature (`--enable goals`) is not needed here.
    argv = ["cx", ROUTE, "workspace-write", str(last_msg_file), headless_prompt]

    status["status"] = "running"
    status["mode_runtime"] = "headless"
    status["codex_argv"] = argv
    status["exec_log"] = str(log_file)
    status["last_message_file"] = str(last_msg_file)
    status["started_at"] = iso_now()
    write_status(status_file, status)

    print(f"[headless] running: {' '.join(repr(a) for a in argv)}")
    print(f"[headless] capturing combined output to {log_file}")

    timeout_sec = max(60, int(budget_min) * 60)
    spawn_env = {**spawn_env, "CX_TIMEOUT": str(timeout_sec)}
    import time
    t0 = time.monotonic()
    timed_out = False
    rc = None
    out = ""
    err = ""
    try:
        # Capture as BYTES (no text=True): Codex emits UTF-8 with chars (box-drawing, emoji)
        # that the Windows default cp1252 decoder chokes on, crashing the stdout reader thread.
        # Decode ourselves with errors="replace" so capture never aborts the run.
        proc = subprocess.run(
            argv,
            cwd=str(project_dir),
            capture_output=True,
            timeout=timeout_sec + 60,  # cx enforces the budget (CX_TIMEOUT); this is the backstop
            env=spawn_env,
        )
        rc = proc.returncode
        timed_out = rc == 124  # cx's `timeout` killed Codex at the budget
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = 124
        out = (exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")) if exc.stdout else ""
        err = (exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")) if exc.stderr else ""
    except FileNotFoundError:
        rc = 127
        err = f"codex binary not found: {codex_bin}"

    elapsed = round(time.monotonic() - t0, 1)
    combined = f"$ {' '.join(argv)}\n\n===== STDOUT =====\n{out}\n\n===== STDERR =====\n{err}\n"
    log_file.write_text(combined, encoding="utf-8")

    last_message = ""
    try:
        if last_msg_file.exists():
            last_message = last_msg_file.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass

    status["finished_at"] = iso_now()
    status["elapsed_sec"] = elapsed
    status["exit_code"] = rc
    status["last_message"] = last_message[:2000]
    status["last_check"] = iso_now()
    if timed_out:
        status["status"] = "timeout"
        status["error"] = f"codex exec exceeded budget ({budget_min} min)"
    elif rc == 0:
        status["status"] = "completed"
    else:
        status["status"] = "failed"
        status["error"] = (err.strip().splitlines()[-1] if err.strip() else f"exit {rc}")[:300]
    write_status(status_file, status)

    print(f"[headless] codex exec exit={rc} elapsed={elapsed}s status={status['status']}")
    if last_message:
        print(f"[headless] last message: {last_message[:200]}")
    return rc if rc is not None else 1


def write_run_launcher(
    run_dir: Path,
    seed_prompt: str,
    aos_session_id: str,
    codex_bin: str,
    claude_session_id: str,
    claude_project: Path,
) -> Path:
    """Write the per-run .bat that launches the RESOLVED codex binary (not bare `codex`)."""
    launcher = run_dir / "run-goal.bat"
    body = (
        "@echo off\r\n"
        f"title {aos_session_id}\r\n"
        f'cd /d "{run_dir.parent.parent.parent}"\r\n'
        f"set AOS_SESSION_ID={aos_session_id}\r\n"
        "set AOS_AGENT=codex\r\n"
        "set \"CLAUDE_SPAWN=1\"\r\n"
        f"set \"CLAUDE_SESSION_ID={claude_session_id}\"\r\n"
        f"set \"CLAUDE_PROJECT={claude_project}\"\r\n"
        "echo [codex-goal] Starting goals loop. Codex will read the plan on its first turn.\r\n"
        f"echo [codex-goal] Binary: {codex_bin}\r\n"
        "echo [codex-goal] Approvals + sandbox bypassed (--dangerously-bypass-approvals-and-sandbox).\r\n"
        f"echo [codex-goal] AOS_SESSION_ID={aos_session_id}\r\n"
        "echo.\r\n"
        f'python "C:/Users/<you>/agentic-os/bin/aos_lock.py" session-start --session "{aos_session_id}" --agent codex >nul 2>nul\r\n'
        f'"{codex_bin}" --enable goals --dangerously-bypass-approvals-and-sandbox "{seed_prompt}"\r\n'
        f'python "C:/Users/<you>/agentic-os/bin/aos_lock.py" session-end --session "{aos_session_id}" >nul 2>nul\r\n'
        "echo.\r\n"
        "echo [codex-goal] Codex exited. Press any key to close this window.\r\n"
        "pause >nul\r\n"
    )
    launcher.write_bytes(body.encode("utf-8"))
    return launcher


def build_spawn_command(terminal: str, project_dir: Path, launcher_bat: Path, aos_session_id: str) -> list[str]:
    if terminal == "wt":
        return ["wt", "-w", aos_session_id, "new-tab", "-d", str(project_dir), str(launcher_bat)]
    if terminal == "powershell":
        return ["powershell", "-NoExit", "-Command", f'Set-Location "{project_dir}"; & "{launcher_bat}"']
    return ["cmd", "/k", f'cd /d "{project_dir}" && "{launcher_bat}"']


def spawn(cmd: list[str]) -> int:
    if os.name == "nt":
        CREATE_NEW_CONSOLE = 0x00000010
        proc = subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE)
    else:
        proc = subprocess.Popen(cmd)
    return proc.pid


def find_codex_pid(spawn_started_after_iso: str, timeout_sec: float = 8.0) -> int | None:
    """Find the live native codex.exe worker spawned after dispatch started.

    Modern Codex is a native PE binary (codex.exe), NOT node.exe+codex.js. Retry briefly
    because the process takes ~1-2s to appear.
    """
    if os.name != "nt":
        return None
    try:
        import time
        deadline = time.monotonic() + timeout_sec
        spawn_dt = datetime.fromisoformat(spawn_started_after_iso)
        while time.monotonic() < deadline:
            try:
                out = subprocess.check_output(
                    [
                        "powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process -Filter \"Name='codex.exe'\" | "
                        "ForEach-Object { \"$($_.ProcessId)|$($_.CreationDate.ToString('o'))\" }"
                    ],
                    text=True, stderr=subprocess.DEVNULL, timeout=4,
                )
            except Exception:
                out = ""
            best_pid = None
            best_t = None
            for line in out.splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                pid_s, ts = line.split("|", 1)
                try:
                    pid_i = int(pid_s)
                    t = datetime.fromisoformat(ts.strip())
                except Exception:
                    continue
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t < spawn_dt - timedelta(seconds=5):
                    continue
                if best_t is None or t > best_t:
                    best_t = t
                    best_pid = pid_i
            if best_pid is not None:
                return best_pid
            time.sleep(0.5)
    except Exception:
        return None
    return None


def dispatch(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).resolve()
    prompt_file = Path(args.prompt_file).resolve()
    status_file = Path(args.status_file).resolve()

    if not project_dir.exists():
        print(f"ERROR: project dir not found: {project_dir}", file=sys.stderr)
        return 2
    if not prompt_file.exists():
        print(f"ERROR: prompt file not found: {prompt_file}", file=sys.stderr)
        return 2

    codex_bin, how = resolve_codex_binary()
    print(f"[dispatch] codex binary: {codex_bin}  ({how})")

    seed_prompt = build_seed_prompt(prompt_file)
    goal_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{project_dir.name}"
    aos_session_id = f"codex-goal-{goal_id}"
    claude_session_id = resolve_claude_session_id()
    spawn_env = build_claude_spawn_env(project_dir, claude_session_id)

    mode = getattr(args, "mode", "gate")
    cadence = getattr(args, "cadence", None)

    initial_status = {
        "goal_id": goal_id,
        "spawned_at": iso_now(),
        "project_dir": str(project_dir),
        "prompt_file": str(prompt_file),
        "status": "spawning" if mode == "gate" else "watching",
        "mode": mode,
        "cadence": cadence,
        "budget_minutes": args.budget_min,
        "respawn_count": 0,
        "thread_id": None,
        "last_check": None,
        "verification_results": None,
        "feedback_file": str(status_file.parent / "feedback.md"),
        "goal_edits_file": str(status_file.parent / "goal_edits.md"),
        "sweep_count": 0 if mode == "watch" else None,
        "wt_window_name": aos_session_id,
        "codex_binary": codex_bin,
        "codex_binary_source": how,
        "claude_session_id": claude_session_id,
        "claude_project": str(project_dir),
        "terminal": None,
        "pid": None,
    }
    write_status(status_file, initial_status)
    print(f"[dispatch] wrote initial status: {status_file}")

    # Fail loudly if the resolved binary can't parse the live config — a stale binary
    # exits 0 with an error and would otherwise present as a healthy "spawned" no-op.
    cfg_ok, cfg_detail = assert_codex_config_ok(codex_bin)
    initial_status["config_check"] = cfg_detail
    if not cfg_ok:
        print(f"ERROR: codex cannot parse its config — refusing to spawn a no-op. {cfg_detail}", file=sys.stderr)
        print("       Fix: ensure CODEX_CLI_PATH in ~/.codex/config.toml points at the current native build,", file=sys.stderr)
        print("       or `npm i -g @openai/codex@latest` so the PATH shim parses the config too.", file=sys.stderr)
        initial_status["status"] = "spawn-failed"
        initial_status["error"] = f"config-load: {cfg_detail}"
        write_status(status_file, initial_status)
        return 4
    print(f"[dispatch] config check: {cfg_detail}")

    if args.no_spawn:
        print("[dispatch] --no-spawn set, exiting before terminal spawn")
        initial_status["status"] = "dry-run"
        write_status(status_file, initial_status)
        return 0

    if getattr(args, "headless", False):
        print(f"[dispatch] --headless set: running `cx {ROUTE}` to completion in-process (no TUI, no keystroke)")
        headless_prompt = build_headless_prompt(prompt_file)
        run_dir = prompt_file.parent
        return run_headless(
            codex_bin=codex_bin,
            project_dir=project_dir,
            run_dir=run_dir,
            status_file=status_file,
            status=initial_status,
            headless_prompt=headless_prompt,
            budget_min=args.budget_min,
            spawn_env=spawn_env,
        )

    launcher_bat = write_run_launcher(
        prompt_file.parent,
        seed_prompt,
        aos_session_id,
        codex_bin,
        claude_session_id,
        project_dir,
    )
    terminal, _ = find_terminal()
    print(f"[dispatch] terminal chosen: {terminal}")
    cmd = build_spawn_command(terminal, project_dir, launcher_bat, aos_session_id)

    try:
        pid = spawn(cmd)
    except Exception as exc:
        print(f"ERROR: spawn failed: {exc}", file=sys.stderr)
        initial_status["status"] = "spawn-failed"
        initial_status["error"] = str(exc)
        write_status(status_file, initial_status)
        return 3

    node_pid = find_codex_pid(initial_status["spawned_at"])

    initial_status["status"] = "spawned"
    initial_status["terminal"] = terminal
    initial_status["pid"] = pid
    initial_status["node_pid"] = node_pid   # field name kept for dashboard compat; holds the codex.exe pid
    initial_status["codex_pid"] = node_pid
    initial_status["wt_window"] = 0
    initial_status["wt_tab_title"] = aos_session_id
    write_status(status_file, initial_status)

    print(f"[dispatch] spawned Codex parent_pid={pid} codex_pid={node_pid} terminal={terminal} mode={mode}")
    print(f"[dispatch] goal_id={goal_id}")
    print()
    print("Codex is seeded with `/goal` + the path to the plan.")
    print("The plan file Codex will read: " + str(prompt_file))
    if mode == "watch" and cadence:
        print()
        print(f"WATCH MODE — register a recurring fire on the {cadence} cadence:")
        print(f"  In Claude Code: /loop {cadence} /codex-goal-dispatcher resume {goal_id}")
    return 0


def smoke_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="codex-goal-smoke-"))
    print(f"[smoke] working in {tmp}")
    prompt_file = tmp / "goal_prompt.md"
    status_file = tmp / "status.json"
    prompt_file.write_text(
        "# Goal: smoke test\n\nWrite a file `smoke.txt` containing `hello`, then mark complete.\n\n"
        "## Verification\n- [ ] `smoke.txt` exists\n- [ ] contains `hello`\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        project_dir=str(tmp), prompt_file=str(prompt_file), status_file=str(status_file),
        budget_min=10, no_spawn=True, mode="gate", cadence=None,
    )
    rc = dispatch(args)
    print(f"\n[smoke] exit code: {rc}")
    print("[smoke] status.json:")
    print(status_file.read_text(encoding="utf-8"))
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"[smoke] cleaned up {tmp}")
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch a Codex /goal run")
    parser.add_argument("--project-dir", help="absolute path to project root")
    parser.add_argument("--prompt-file", help="absolute path to filled goal prompt md")
    parser.add_argument("--status-file", help="absolute path where status.json lives")
    parser.add_argument("--budget-min", type=int, default=180, help="budget in minutes")
    parser.add_argument("--no-spawn", action="store_true", help="skip terminal spawn (dry run)")
    parser.add_argument("--headless", action="store_true",
                        help="HANDS-FREE: run `codex exec` to completion in-process (no TUI/keystroke); "
                             "captures exit+output to run dir and drives status.json. Blocks until Codex exits.")
    parser.add_argument("--smoke-test", action="store_true", help="self-test mode")
    parser.add_argument("--mode", choices=("gate", "watch"), default="gate",
                        help="gate: one-shot run-to-completion. watch: recurring sweep (--cadence required).")
    parser.add_argument("--cadence", default=None, help="cadence string for watch mode (e.g. '30m').")
    args = parser.parse_args(argv)

    if args.smoke_test:
        return smoke_test()

    missing = [a for a in ("project_dir", "prompt_file", "status_file") if not getattr(args, a)]
    if missing:
        parser.error(f"missing required args: {', '.join(missing)}")
    return dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
