#!/usr/bin/env python3
"""In-process hook runner and output merger for dispatch.sh (audit 2026-10-12, item G).

  run.py PreToolUse      hook JSON on stdin; runs the PreToolUse hooks in ONE interpreter,
                         in settings order, each only when its own matcher applies; prints one merged result.
  run.py --merge EVENT   NUL-separated hook stdouts on stdin (from dispatch.sh); prints one merged result.
  run.py --self-test     merge/precedence unit checks.

The hooks themselves are not touched: each .py runs via runpy exactly as `python3 <script>` would
(script dir on sys.path, fresh globals, own stdin/stdout, SystemExit honoured, own timeout);
webcred-guard.sh is a bash wrapper around `python3 -I -c "$PROG"`, so its heredoc body runs here.
Merge rules (code.claude.com/docs/en/hooks): permissionDecision deny > defer > ask > allow;
every additionalContext is kept; plain stdout counts as context only on UserPromptSubmit/SessionStart;
output of a hook that exits non-0/2, times out or prints unparseable JSON is dropped (non-blocking error),
except for a SAFETY hook: then the call is denied ("safety hook <name> failed: <why>", fail closed).
The web-taint guard has a separate state-aware failure rule: present/unreadable state denies; absent allows.
"""
import io
import json
import os
import signal
import sys
# re/runpy/subprocess/traceback are imported where used: --merge needs only json (traceback alone costs ~33 ms).

H = __import__('os').path.expanduser("~/.claude/hooks")
HV = __import__('os').path.expanduser("~/AI_Projects/pipeline/handoff-v2/hooks")
# Same order, matchers and timeouts as settings.json.hooks.original. Edit here when PreToolUse hooks change.
PRE = [
    ("Read|Edit|MultiEdit|Write|Grep|Glob|Bash|NotebookEdit", H + "/secret-firewall/block-secrets.py", 10),
    ("Read", HV + "/read-discipline.py", 5),
    ("Bash", HV + "/bash-inspection.py", 5),
    ("Bash|Read|Edit|MultiEdit|Write|NotebookEdit|Grep|Glob", H + "/webcred-guard.sh", 5),
    ("Bash", H + "/odin-model-guard.py", 10),
    ("Agent|Workflow", H + "/route-guard.py", 5),  # cost guard (2026-10-13): fails open, not in SAFETY
    ("*", H + "/camera-guard.py", 5),  # no AI uses a camera (the user, 2026-10-20)
    ("*", H + "/web-taint/guard.py", 35),  # reviewer agent call (25 s) while a session is flagged
]
# A failure (non-0/2 exit, timeout, unparseable stdout) of a hook whose path contains one of these denies the call.
SAFETY = ("secret-firewall", "block-secrets", "webcred-guard", "odin-model-guard", "camera-guard")
TEXT_EVENTS = ("UserPromptSubmit", "SessionStart")
RANK = {"allow": 1, "ask": 2, "defer": 3, "deny": 4}
LEGACY = {"approve": "allow", "block": "deny"}


class HookTimeout(BaseException):  # BaseException: the hooks' own `except Exception` must not swallow it
    pass


def _alarm(signum, frame):
    raise HookTimeout()


def webcred_prog(path):
    """The python body of webcred-guard.sh, or None if the wrapper has grown any other bash."""
    import re
    src = open(path, encoding="utf-8").read()
    m = re.search(r"^read -r -d '' PROG <<'PY'\n(.*?)\nPY\n", src, re.S | re.M)
    if not m:
        return None
    rest = [ln for ln in (src[:m.start()] + src[m.end():]).splitlines() if ln.strip() and not ln.startswith("#")]
    return m.group(1) if rest == ['exec /usr/bin/python3 -I -c "$PROG"'] else None


def run_hook(path, raw, timeout):
    """Run one hook in this interpreter. Returns (exit_code, stdout, stderr); exit_code None = timed out."""
    prog = webcred_prog(path) if path.endswith(".sh") else None
    if path.endswith(".sh") and prog is None:  # ponytail: wrapper changed shape -> run it the old way
        import subprocess
        try:
            p = subprocess.run(["bash", path], input=raw, capture_output=True, timeout=timeout)
            return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return None, "", ""
    out, err = io.BytesIO(), io.BytesIO()
    saved = sys.stdin, sys.stdout, sys.stderr, sys.argv, sys.path[:]
    # Locals keep the wrappers alive: a collected TextIOWrapper closes its BytesIO before we read it.
    si = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    so = io.TextIOWrapper(out, encoding="utf-8", write_through=True)
    se = io.TextIOWrapper(err, encoding="utf-8", write_through=True)
    sys.stdin, sys.stdout, sys.stderr = si, so, se
    code = 0
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        if prog is None:
            sys.argv = [path]
            sys.path.insert(0, os.path.dirname(path))  # what `python3 <script>` puts first
            import runpy
            runpy.run_path(path, run_name="__main__")
        else:
            sys.argv = ["-c"]
            exec(compile(prog, "<string>", "exec"), {"__name__": "__main__", "__builtins__": __builtins__})
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except HookTimeout:
        code = None
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        so.flush()
        se.flush()
        sys.stdin, sys.stdout, sys.stderr, sys.argv, sys.path[:] = saved
    return code, out.getvalue().decode("utf-8", "replace"), err.getvalue().decode("utf-8", "replace")


def safety_failure(path, code, out):
    """Why a safety hook's result cannot be trusted (-> deny), or None. Non-safety hooks always None."""
    if not any(s in path for s in SAFETY):
        return None
    if code is None:
        return "timed out"
    if code not in (0, 2):
        return "exit %s" % code
    if code == 0 and out.strip():
        try:
            if isinstance(json.loads(out), dict):
                merge("PreToolUse", [out, "{}"])  # a dict merge() chokes on would be dropped by its fallback
                return None
        except Exception:
            pass
        return "unparsable output"
    return None


def guard_failure_denies(agent: str, session_id: str) -> bool:
    """Independent of guard/web imports: present or unreadable state fails closed."""
    import re
    if (not isinstance(agent, str) or not isinstance(session_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_]+", agent)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id)):
        return True
    import stat
    primary = os.environ.get("WEB_TAINT_STATE_DIR", os.path.expanduser("~/.local/state/web-taint"))
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/" + str(os.getuid()))
    fallback = os.environ.get("WEB_TAINT_FALLBACK_DIR", os.path.join(runtime, "web-taint"))
    for directory in (primary, fallback):
        try:
            info = os.stat(directory)
        except FileNotFoundError:
            if os.path.islink(directory):
                return True
            continue
        except OSError:
            return True
        if not stat.S_ISDIR(info.st_mode) or not info.st_mode & 0o444 or not info.st_mode & 0o111:
            return True
        try:
            with os.scandir(directory):
                pass
            os.lstat(os.path.join(directory, "%s-%s.json" % (agent, session_id)))
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


SANDBOX_PREFIX = "/usr/bin/bwrap --dev-bind / / "


def guard_output_valid(code, out):
    """The guard is silent on allow, emits one structured deny on refusal, or allows a sandboxed command."""
    if code != 0:
        return False
    if not out.strip():
        return True
    try:
        value = json.loads(out)
        h = value["hookSpecificOutput"]
        if (set(value) == {"hookSpecificOutput"} and isinstance(h, dict)
                and set(h) == {"hookEventName", "permissionDecision", "updatedInput"}
                and h["hookEventName"] == "PreToolUse" and h["permissionDecision"] == "allow"
                and isinstance(h["updatedInput"], dict)
                and str(h["updatedInput"].get("command", "")).startswith(SANDBOX_PREFIX)):
            return True
        return (set(value) == {"hookSpecificOutput"} and isinstance(h, dict)
                and set(h) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}
                and h["hookEventName"] == "PreToolUse" and h["permissionDecision"] == "deny"
                and isinstance(h["permissionDecisionReason"], str) and bool(h["permissionDecisionReason"]))
    except (ValueError, TypeError, KeyError, RecursionError):
        return False


def merge(event, outs):
    outs = [o for o in outs if o.strip()]
    if len(outs) < 2:
        return outs[0] if outs else ""  # one hook spoke: pass it through byte for byte
    top, ctx, best, reasons, legacy = {}, [], None, [], []
    for o in outs:
        s = o.strip()
        if not (s.startswith("{") and s.endswith("}")):
            if event in TEXT_EVENTS:
                ctx.append(s)
            continue
        try:
            j = json.loads(s)
        except ValueError:
            continue
        if not isinstance(j, dict):
            continue
        h = j.pop("hookSpecificOutput", None) or {}
        if h.get("additionalContext"):
            ctx.append(h["additionalContext"])
        d = h.get("permissionDecision") or (LEGACY.get(j.get("decision")) if event == "PreToolUse" else None)
        r = h.get("permissionDecisionReason") or j.get("reason")
        if d in RANK:
            if best is None or RANK[d] > RANK[best]:
                best, reasons = d, []
            if d == best and r and r not in reasons:
                reasons.append(r)
        if "updatedInput" in h:
            top.setdefault("_updatedInput", h["updatedInput"])  # ponytail: first wins; only web-taint/guard.py sets it
        if j.get("decision") == "block":
            top["decision"] = "block"
            if j.get("reason") and j["reason"] not in legacy:
                legacy.append(j["reason"])
        for k, v in j.items():
            if k not in ("decision", "reason"):
                top.setdefault(k, v)  # ponytail: universal fields, first wins; no hook sets them today
        if j.get("continue") is False:
            top["continue"] = False
    hso = {"hookEventName": event}
    if best:
        hso["permissionDecision"] = best
        if reasons:
            hso["permissionDecisionReason"] = "\n".join(reasons)
    if "_updatedInput" in top:
        hso["updatedInput"] = top.pop("_updatedInput")
    if ctx:
        hso["additionalContext"] = "\n\n".join(ctx)
    if legacy:
        top["reason"] = "\n".join(legacy)
    if len(hso) > 1:
        top["hookSpecificOutput"] = hso
    return json.dumps(top) if top else ""


def pre_tool_use(raw, hooks=PRE):
    try:
        data = json.loads(raw.decode("utf-8-sig", "replace") or "{}")
        tool = str(data.get("tool_name", ""))
        session_id = data.get("session_id", "")
    except Exception:
        session_id = ""
        tool = None  # unparseable: let every hook see it, as before
    signal.signal(signal.SIGALRM, _alarm)
    outs = []
    argv, path0 = sys.argv[:], sys.path[:]
    for matcher, path, timeout in hooks:
        if tool is not None and matcher not in ("", "*") and tool not in matcher.split("|"):  # ponytail: matchers here are plain name lists
            continue
        try:
            code, out, err = run_hook(path, raw, timeout)
        except HookTimeout:  # alarm landed in run_hook's own except/finally (hook ended ~at its timeout): restore, drop
            signal.setitimer(signal.ITIMER_REAL, 0)
            sys.stdin, sys.stdout, sys.stderr, sys.argv, sys.path[:] = (
                sys.__stdin__, sys.__stdout__, sys.__stderr__, argv, path0)
            code, out, err = None, "", ""
        except Exception as e:  # run_hook itself failed (e.g. webcred-guard.sh missing): the hook never ran
            code, out, err = 1, "", "dispatch: %s could not run: %r\n" % (path, e)
        is_guard = path.replace(os.sep, "/").endswith("/web-taint/guard.py")
        if is_guard:
            # Hook stderr can contain input/exception text. Emit fixed codes only.
            if not guard_output_valid(code, out):
                sys.stderr.write("dispatch: GUARD failed\n")
                if guard_failure_denies("claude", session_id):
                    outs.append(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                        "permissionDecision": "deny", "permissionDecisionReason": "GUARD: state exists or is unreadable"}}))
                continue
            outs.append(out)
            continue
        sys.stderr.write(err)
        why = safety_failure(path, code, out)
        if why:  # fail closed: a broken guard must not let the call through
            why = "safety hook %s failed: %s" % (os.path.basename(path), why)
            sys.stderr.write("dispatch: %s\n" % why)
            out = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": why}})
        elif code == 2:  # exit 2 = deny with stderr as the reason
            out = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": err.strip()}})
        elif code != 0:
            sys.stderr.write("dispatch: %s %s, output dropped\n" % (path, "timed out" if code is None else "exit %s" % code))
            continue
        outs.append(out)
    try:
        res = merge("PreToolUse", outs)
    except Exception:  # a malformed hook output must not crash the dispatcher and drop every guard's deny
        res = next((o for o in outs if '"deny"' in o), "")
    return res


def self_test_fail_closed():
    """Mocked hooks through pre_tool_use: safety-hook timeout/crash/garbage -> deny, non-safety -> dropped."""
    import shutil
    import tempfile
    from contextlib import redirect_stderr
    t = tempfile.mkdtemp(prefix="dispatch-selftest.")
    try:
        def hook(name, body):
            p = os.path.join(t, name)
            with open(p, "w") as f:
                f.write(body)
            return p
        sleep = "import time\ntime.sleep(5)\n"
        raw = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "wc -l x"}})
        raw = raw.encode()
        def checked(hooks, diagnostic=None):
            err = io.StringIO()
            with redirect_stderr(err):
                result = pre_tool_use(raw, hooks)
            if diagnostic is None:
                assert err.getvalue() == "", err.getvalue()
            else:
                assert diagnostic in err.getvalue(), err.getvalue()
            return result

        cases = [
            (hook("odin-model-guard.py", sleep), "safety hook odin-model-guard.py failed: timed out"),
            (hook("webcred-guard.py", "raise SystemExit(1)\n"), "safety hook webcred-guard.py failed: exit 1"),
            (hook("block-secrets.py", "print('not json')\n"), "safety hook block-secrets.py failed: unparsable output"),
            (hook("secret-firewall.py", "print('{\"hookSpecificOutput\": \"x\"}')\n"),
             "safety hook secret-firewall.py failed: unparsable output"),
            (os.path.join(t, "webcred-guard.sh"), "safety hook webcred-guard.sh failed: exit 1"),  # missing file
        ]
        for path, want in cases:
            r = json.loads(checked([("Bash", path, 0.3)], "dispatch: " + want) or "{}").get("hookSpecificOutput", {})
            assert r.get("permissionDecision") == "deny" and r.get("permissionDecisionReason") == want, (path, r)
        ok = hook("odin-model-guard-ok.py", "pass\n")  # safety hook, exit 0, silent -> allowed (no output)
        assert checked([("Bash", ok, 1)]) == ""
        opt = hook("read-discipline.py", sleep)  # non-safety hook timing out: dropped, as before
        assert checked([("Bash", opt, 0.3)], "timed out, output dropped") == ""
        m = json.loads(checked([("Bash", opt, 0.3), ("Bash", cases[0][0], 0.3)],
                              "dispatch: " + cases[0][1]))["hookSpecificOutput"]
        assert m["permissionDecision"] == "deny" and m["permissionDecisionReason"] == cases[0][1], m
    finally:
        shutil.rmtree(t, ignore_errors=True)


def self_test():
    d1 = json.dumps({"decision": "block", "reason": "A", "hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "A"}})
    d2 = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                            "permissionDecisionReason": "B"}})
    c1 = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "C"}})
    ask = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                             "permissionDecisionReason": "Q"}})
    allow = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})
    assert merge("PreToolUse", []) == ""
    assert merge("PreToolUse", ["", d1 + "\n"]) == d1 + "\n"  # single output passes through untouched
    m = json.loads(merge("PreToolUse", [d1, c1, d2]))
    assert m["decision"] == "block" and m["reason"] == "A", m
    assert m["hookSpecificOutput"] == {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "A\nB", "additionalContext": "C"}, m
    m = json.loads(merge("PreToolUse", [allow, ask, c1]))["hookSpecificOutput"]
    assert m["permissionDecision"] == "ask" and m["permissionDecisionReason"] == "Q", m
    m = json.loads(merge("PreToolUse", [ask, d2]))["hookSpecificOutput"]
    assert m["permissionDecision"] == "deny" and m["permissionDecisionReason"] == "B", m
    m = json.loads(merge("PreToolUse", ["plain text", "{broken}", c1, d2]))["hookSpecificOutput"]
    assert m["additionalContext"] == "C" and m["permissionDecision"] == "deny", m
    u = json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "N"}})
    m = json.loads(merge("UserPromptSubmit", [u, "<hh>\nrow\n</hh>\n"]))
    assert m == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                        "additionalContext": "N\n\n<hh>\nrow\n</hh>"}}, m
    m = json.loads(merge("UserPromptSubmit", [u, json.dumps({"decision": "block", "reason": "R"})]))
    assert m["decision"] == "block" and m["reason"] == "R", m
    m = json.loads(merge("SessionStart", ["[handoff] queue=0", u.replace("UserPromptSubmit", "SessionStart")]))
    assert m["hookSpecificOutput"]["additionalContext"] == "[handoff] queue=0\n\nN", m
    assert webcred_prog(H + "/webcred-guard.sh"), "webcred-guard.sh no longer has the expected wrapper shape"
    self_test_fail_closed()
    print("run.py self-test passed")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a == ["--self-test"]:
        self_test()
    elif a[:1] == ["--merge"] and len(a) == 2:
        sys.stdout.write(merge(a[1], sys.stdin.buffer.read().decode("utf-8", "replace").split("\0")))
    elif a == ["PreToolUse"]:
        sys.stdout.write(pre_tool_use(sys.stdin.buffer.read()))
