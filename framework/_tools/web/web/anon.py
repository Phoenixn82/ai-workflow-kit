"""Disposable interactive browsers. Odin owns callers/output; the browser host owns processes/profiles.

The private worker protocol travels on SSH stdin, never through a shell interpolation of page text.
Host watchers enforce idle expiry; Odin watchers also stop browsers when their owning agent dies.
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import act, identities
from .config import LANES

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
SCP = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
POLL = 5


def _root(remote=False):
    return act.STATE_DIR / ("anon-host" if remote else "anon")


def _path(key, remote=False):
    if not re.fullmatch(r"[0-9a-f]{16}", key):
        raise act.ActError("invalid anonymous caller", 2)
    return _root(remote) / f"{key}.json"


def _read(key, remote=False):
    try:
        return json.loads(_path(key, remote).read_text())
    except (OSError, ValueError):
        return None


def _save(key, data, remote=False):
    path = _path(key, remote)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    act._write_private(path, json.dumps(data))


@contextlib.contextmanager
def _browser(key, data):
    """Temporarily point the existing launch/stop helpers exclusively at a throwaway profile."""
    names = ("IDENTITY_DIR", "RT_DIR", "CONFIG_DIR", "BROWSER_UNIT", "DISPLAY_UNIT")
    saved = {name: getattr(act, name) for name in names}
    root = Path(data["dir"])
    act.IDENTITY_DIR, act.RT_DIR, act.CONFIG_DIR = root / "profile", root / "rt", root / "config"
    act.BROWSER_UNIT, act.DISPLAY_UNIT = f"web-anon-browser-{key}", f"web-anon-display-{key}"
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(act, name, value)


def _agent(key, args, port=None, stdin_text=None, *, timeout=3600):
    return subprocess.run(["agent-browser", "--session", f"anon-{key}",
                           *(["--cdp", port] if port else []), *args],
                          env=act.identity_env(), capture_output=True, text=True,
                          input=stdin_text, timeout=timeout)


def _stop(key, data):
    with _browser(key, data):
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            _agent(key, ["close"], timeout=30)
        act._stop_units()
    shutil.rmtree(data["dir"])
    _path(key, True).unlink(missing_ok=True)


def _reap():
    """Called under the host launch lock; skip commands holding their caller lock."""
    for path in _root(True).glob("*.json"):
        try:
            with act._lock(f"anon-host-{path.stem}.lock", blocking=False):
                data = _read(path.stem, True)
                if data and time.time() - data["ts"] > act.LEASE_IDLE:
                    _stop(path.stem, data)
        except BlockingIOError:
            pass


def _guard_launch():
    cap = LANES["act"].get("max_anon_browsers", 12)
    if len(list(_root(True).glob("*.json"))) >= cap:
        raise act.ActError(f"anonymous browser cap ({cap}) reached; release an unused session", 3)
    if os.getloadavg()[0] > (os.cpu_count() or 1) * 1.5:
        raise act.ActError("browser host busy; not starting another anonymous browser", 3)
    temp = identities.temperature()
    if temp is not None and temp >= LANES["act"]["max_launch_temp_c"]:
        raise act.ActError("browser host too hot; not starting another anonymous browser", 3)


def _spawn(*args):
    subprocess.Popen([sys.executable, "-m", "web.anon", *args], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                     close_fds=True)


def _ensure(key):
    with act._lock("anon-launch.lock"):
        _reap()
        data = _read(key, True)
        if data and time.time() - data["ts"] > act.LEASE_IDLE:
            _stop(key, data)
            data = None
        if data:
            with _browser(key, data):
                if act.browser_port():
                    data["ts"] = time.time()
                    _save(key, data, True)
                    return data
            _stop(key, data)
        _guard_launch()
        data = {"dir": tempfile.mkdtemp(prefix=f"web-anon-{key}-"), "ts": time.time(), "token": uuid.uuid4().hex}
        try:
            (Path(data["dir"]) / "uploads").mkdir(mode=0o700)
            with _browser(key, data):
                act._launch_browser(prefix=("nice", "-n", "10"))
            _save(key, data, True)
            _spawn("reap", key, data["token"])
        except BaseException:
            _stop(key, data)
            raise
        return data


def _login_wall(key, port):
    # Fixed metadata only; no login text or scripts supplied by the page become instructions.
    script = """JSON.stringify({password:!!document.querySelector('input[type=password]'),
      signIn:[...document.querySelectorAll('h1,h2,[role=heading]')].some(e=>/sign in|log in/i.test(e.innerText)),
      url:location.href})"""
    try:
        result = _agent(key, ["eval", script, "--json"], port, timeout=10)
        data = json.loads(result.stdout)["data"]["result"]
        data = json.loads(data) if isinstance(data, str) else data
        from .act_routing import identity_url
        return bool(data.get("password") or (data.get("signIn") and identity_url(data.get("url", ""))))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return False


def dispatch(request):
    """Executed on the browser host; returns data only, never emits browser output."""
    key, op = request["key"], request["op"]
    _path(key, True)  # validate before using the key in unit/lock names
    if op not in ("ensure", "run", "stop"):
        raise act.ActError("invalid anonymous operation", 2)
    if op == "run" and (why := act.refused(request["args"])):
        raise act.ActError(why, 2)
    with act._lock(f"anon-host-{key}.lock"):
        if op == "stop":
            if data := _read(key, True):
                _stop(key, data)
            return {}
        data = _ensure(key)
        if op == "ensure":
            return data
        args = request["args"]
        with _browser(key, data):
            port = act.browser_port()
            if not port:
                raise act.ActError("anonymous browser stopped; open the URL again")
            # The identity readback guards also apply to disposable profiles.
            if args[:2] == ["get", "value"]:
                probe = _agent(key, ["get", "attr", args[2], "type", "--json"], port)
                try:
                    password = str(json.loads(probe.stdout)["data"]["value"] or "").lower() == "password"
                except (ValueError, KeyError, TypeError):
                    raise act.ActError("could not check field type") from None
                if password or act._mentions_password(args[2]):
                    raise act.ActError(act.READBACK, 2)
            if args[0] == "eval":
                script = request.get("stdin_text") if "--stdin" in args else act._eval_script(args)
                if act._mentions_password(script or "") or act.STORAGE_JS.search(script or ""):
                    raise act.ActError(act.READBACK, 2)
            result = _agent(key, args, port, request.get("stdin_text"))
            hint = args[0] in ("open", "snapshot", "click", "reload") and _login_wall(key, port)
        data["ts"] = time.time()
        _save(key, data, True)
        return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode, "login": hint,
                "artifact_exists": bool(request.get("artifact") and Path(request["artifact"]).is_file())}


class Unreachable(Exception):
    pass


def _request(host, key, op, **kw):
    request = {"key": key, "op": op, **kw}
    if host == "odin":
        return dispatch(request)
    try:
        result = subprocess.run([*SSH, "thor", "--", "web", "_anon", "request"],
                                input=json.dumps(request), capture_output=True, text=True, timeout=3700)
    except (OSError, subprocess.TimeoutExpired):
        raise Unreachable from None
    if result.returncode == 255:
        raise Unreachable
    # Scan even a malformed worker's diagnostics before reporting a fixed error.
    from . import output_scan
    output_scan.serialized(result.stdout or "")
    output_scan.serialized(result.stderr or "")
    try:
        reply = json.loads(result.stdout)
        if "error" in reply:
            raise act.ActError(reply["error"], reply.get("code", 5))
        if result.returncode or not isinstance(reply, dict):
            raise ValueError
        return reply
    except (ValueError, TypeError):
        raise act.ActError("anonymous browser worker unavailable on thor (install matching web version)") from None


def _thor_up():
    from . import host
    return host.wake_thor()


def _copy(source, dest):
    result = subprocess.run([*SCP, "--", source, dest], capture_output=True, text=True, timeout=120)
    from . import output_scan
    output_scan.serialized(result.stderr or "")
    if result.returncode:
        raise act.ActError("anonymous browser file transfer failed")


def _files(args, data, host):
    """Rewrite local file arguments for the browser host; return the download mapping."""
    args = list(args)
    if args[0] == "upload":
        if len(args) < 3:
            raise act.ActError("upload requires a selector and local file", 2)
        names = set()
        for i in range(2, len(args)):
            if args[i].startswith("--"):
                continue
            source = Path(args[i]).expanduser().resolve(strict=True)
            if source.name in names:
                raise act.ActError("duplicate upload filenames; upload them in separate commands", 2)
            names.add(source.name)
            target = str(Path(data["dir"]) / "uploads" / source.name)
            if host == "thor":
                _copy(str(source), f"thor:{target}")
            else:
                shutil.copyfile(source, target)
            args[i] = target
        return args, None
    if args[0] not in ("download", "screenshot", "pdf"):
        return args, None
    if args[0] == "download":
        if len(args) < 3:
            raise act.ActError("download requires a selector and local path", 2)
        index = 2
    else:
        directory, image_format = Path(tempfile.gettempdir()), "png"
        # Output directory belongs to Odin; never send an Odin directory to the browser host.
        filtered, i = [args[0]], 1
        while i < len(args):
            flag, _, value = args[i].partition("=")
            if flag == "--screenshot-dir":
                if not value:
                    if i + 1 >= len(args):
                        raise act.ActError("--screenshot-dir requires a directory", 2)
                    i += 1
                    value = args[i]
                directory = Path(value).expanduser()
            else:
                filtered.append(args[i])
                if flag == "--screenshot-format":
                    image_format = value or (args[i + 1] if i + 1 < len(args) else "png")
            i += 1
        args = filtered
        value_flags = {"--threshold", "--screenshot-quality", "--screenshot-format", "--screenshot-dir"}
        positions, skip = [], False
        for i in range(1, len(args)):
            if skip:
                skip = False
            elif args[i] in value_flags:
                skip = True
            elif not args[i].startswith("-"):
                positions.append(i)
        index = positions[-1] if positions else len(args)
        if len(positions) == 1 and args[index].startswith(("@", "#", "[")):
            index = len(args)  # selector-only screenshot still needs an Odin destination
        if index == len(args):
            suffix = ".pdf" if args[0] == "pdf" else (".jpeg" if image_format == "jpeg" else ".png")
            directory.mkdir(parents=True, exist_ok=True)
            args.append(str(directory / f"web-anon-{uuid.uuid4().hex}{suffix}"))
    local = str(Path(args[index]).expanduser().resolve())
    remote = str(Path(data["dir"]) / (uuid.uuid4().hex + Path(local).suffix))
    args[index] = remote
    return args, (remote, local)


def run(args, *, output_hidden=None):
    key = identities.caller_key()
    with act._lock(f"anon-client-{key}.lock"):
        record = _read(key)
        if args != ["release"] and (why := act.refused(args)):
            raise act.ActError(why, 2)
        if args == ["release"] or args[:1] == ["close"]:
            if record:
                try:
                    _request(record["host"], key, "stop")
                except Unreachable:
                    raise act.ActError("thor unreachable; anonymous session expires at its idle deadline") from None
                _path(key).unlink(missing_ok=True)
            print("anonymous browser closed")
            return 0
        js = act.runs_js(args)
        if js and act.ALLOW_EVAL not in args:
            raise act.ActError(act.EVAL_REFUSED, 2)
        stdin_text = sys.stdin.read() if args[0] == "eval" and "--stdin" in args else None
        if args[0] == "eval":
            script = stdin_text if stdin_text is not None else act._eval_script(args)
            if act._mentions_password(script or "") or act.STORAGE_JS.search(script or ""):
                raise act.ActError(act.READBACK, 2)
        if js:
            act.log_eval(args, stdin_text)
        args = [arg for arg in args if arg != act.ALLOW_EVAL]
        host = record["host"] if record else ("thor" if _thor_up() else "odin")
        if not record and host == "odin":
            print("web act: thor unreachable, using an odin throwaway", file=sys.stderr)
        try:
            data = _request(host, key, "ensure")
        except Unreachable:
            print("web act: thor unreachable, using an odin throwaway", file=sys.stderr)
            host, data = "odin", _request("odin", key, "ensure")
        token = record["token"] if record else uuid.uuid4().hex
        client = {"host": host, "owner": act.caller(), "ts": time.time(), "token": token}
        _save(key, client)
        if not record:
            _spawn("watch", key, token)
        remote_args, download = _files(args, data, host)
        try:
            result = _request(host, key, "run", args=remote_args, stdin_text=stdin_text,
                              artifact=download[0] if download else None)
        except Unreachable:
            raise act.ActError("thor unreachable during command; open the URL again") from None
        if result["returncode"] == 0 and download and result.get("artifact_exists", True):
            remote, local = download
            if host == "thor":
                _copy(f"thor:{remote}", local)
            else:
                shutil.copyfile(remote, local)
            old, new = (json.dumps(remote)[1:-1], json.dumps(local)[1:-1]) if "--json" in args else (remote, local)
            result["stdout"] = (result.get("stdout") or "").replace(old, new)
        _save(key, {**client, "ts": time.time()})
        if result.get("login"):
            print("this site needs a login: re-run with --as personal", file=sys.stderr)
        print(f"web act: host={host} tier=anon", file=sys.stderr)
        return act.emit_result(args, subprocess.CompletedProcess(args, result["returncode"],
                               result.get("stdout", ""), result.get("stderr", "")), output_hidden=output_hidden)


def status():
    now = time.time()
    return [{"host": data["host"], "caller": data["owner"].split(":", 1)[0],
             "session": f"anon-{path.stem}", "idle_s": max(0, int(now - data["ts"]))}
            for path in _root().glob("*.json") if (data := _read(path.stem))
            and identities._live({**data, "ts": now}, now)]


def watch(key, token, remote=False):
    """Detached, bounded by lease lifetime; never inherits a browser or its output pipes."""
    while data := _read(key, remote):
        if data["token"] != token:
            return
        expired = time.time() - data["ts"] > act.LEASE_IDLE
        if not remote:
            expired = not identities._live({**data, "ts": time.time()}, time.time()) or expired
        if expired:
            try:
                with act._lock(f"anon-{'host' if remote else 'client'}-{key}.lock", blocking=False):
                    # Re-read after locking: a command may have just renewed this lease.
                    fresh = _read(key, remote)
                    if not fresh or fresh["token"] != token:
                        return
                    if fresh["ts"] != data["ts"]:
                        continue
                    if remote:
                        _stop(key, data)
                    else:
                        try:
                            _request(data["host"], key, "stop")
                        except (Unreachable, act.ActError):
                            pass  # the host watcher enforces its own idle deadline
                        _path(key).unlink(missing_ok=True)
                    return
            except BlockingIOError:
                pass
        time.sleep(POLL)


def main(argv):
    if argv == ["request"]:
        try:
            result = dispatch(json.load(sys.stdin))
        except act.ActError as e:
            result = {"error": str(e), "code": e.code}
        except Exception:
            result = {"error": "anonymous browser worker failed", "code": 5}
        print(json.dumps(result))
    elif len(argv) == 3 and argv[0] in ("watch", "reap"):
        watch(argv[1], argv[2], remote=argv[0] == "reap")
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
