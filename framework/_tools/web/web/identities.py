"""Named browser selection, per-caller windows, discovery and launch guards.

All browser state stays on act; helpers read it at call time, including after a fork.
"""
import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from threading import Event, Lock, Thread, local

from . import act
from .config import LANES

NAME = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
PROC_DIR = Path("/proc")
HWMON_DIR = Path("/sys/class/hwmon")


def select(name: str) -> None:
    if not NAME.fullmatch(name):
        raise act.ActError("identity name must match ^[a-z][a-z0-9-]{0,23}$", code=2)
    act.IDENTITY = name
    suffix = "" if name == "personal" else f"-{name}"
    act.IDENTITY_DIR = act.STATE_DIR / ("identity" if not suffix else f"identities/{name}")
    act.SESSION = f"web-identity{suffix}"
    act.BROWSER_UNIT = f"web-identity-browser{suffix}"
    act.DISPLAY_UNIT = f"web-identity-display{suffix}"
    act.LOCK_FILE = "act.lock" if not suffix else f"act-{name}.lock"


def caller() -> str:
    if act.OWNER is not None:
        return act.OWNER
    pid, seen = os.getppid(), set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            stat = (PROC_DIR / str(pid) / "stat").read_text()
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            fields = stat.rsplit(")", 1)[1].split()
            if comm in ("claude", "codex"):
                act.OWNER = f"{comm}:{pid}:{int(fields[19])}"
                return act.OWNER
            pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            break
    act.OWNER = "forwarded" if "WEB_FORWARDED" in os.environ else "terminal"
    return act.OWNER


def caller_key(owner: str | None = None) -> str:
    return hashlib.sha256((owner or caller()).encode()).hexdigest()[:16]


def session_name() -> str:
    # Hash both identity and caller: fixed length, including for 24-character identity names.
    return "web-id-" + hashlib.sha256(f"{act.IDENTITY}:{caller()}".encode()).hexdigest()[:16]


def lease_path(name: str | None = None, owner: str | None = None) -> Path:
    return act.STATE_DIR / "leases" / (name or act.IDENTITY) / f"{caller_key(owner)}.json"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        owner, ts = data["owner"], data["ts"]
        if not isinstance(owner, str) or not owner or type(ts) not in (int, float) or not math.isfinite(ts):
            return None
        if path.stem != caller_key(owner):
            return None
        if "target_id" not in data or (data["target_id"] is not None and not isinstance(data["target_id"], str)):
            return None
        if not isinstance(data.get("targets", []), list) or not all(isinstance(t, str) for t in data.get("targets", [])):
            return None
        labels = data.get("labels", {})
        if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None


def _live(data: dict, now: float) -> bool:
    if now - data["ts"] > act.LEASE_IDLE:
        return False
    owner = data["owner"]
    if owner.startswith(("claude:", "codex:")):
        try:
            _, pid, start = owner.split(":")
            return act.alive(int(pid), int(start))
        except (ValueError, OverflowError):
            return False
    return True


def records(name: str | None = None) -> list[dict]:
    return [data for path in (act.STATE_DIR / "leases" / (name or act.IDENTITY)).glob("*.json")
            if (data := _read(path)) is not None]


def lease(name: str, now: float | None = None) -> dict | None:
    """The calling session's live window record; never another session's record."""
    data = _read(lease_path(name))
    return data if data and _live(data, time.time() if now is None else now) else None


def _save(data: dict) -> None:
    path = lease_path(owner=data["owner"])
    path.parent.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.parent.chmod(0o700)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    act._write_private(path, json.dumps(data))


def acquire_locked() -> None:
    data = _read(lease_path()) or {"owner": caller(), "target_id": None}
    data["ts"] = time.time()
    _save(data)


def acquire() -> None:
    # A final close holds the lifecycle lock while stopping the process. New callers register afterwards.
    with act._lock(f"browser-{act.IDENTITY}.lock"), act._lock():
        acquire_locked()


def renew_handoff(target: str) -> None:
    """A closed/released handoff must not recreate its caller record during the next poll."""
    with act._lock(f"browser-{act.IDENTITY}.lock"), act._lock():
        data = _read(lease_path())
        if not data or target not in data.get("targets", []):
            raise act.ActError("session window closed during the handoff")
        acquire_locked()


def _caller_lock(owner: str | None = None) -> str:
    return f"caller-{act.IDENTITY}-{caller_key(owner)}.lock"


_operation = local()


@contextlib.contextmanager
def operation(*, release: bool = False):
    """Serialize only this caller; the identity lock covers record updates, never page commands."""
    key = (os.getpid(), act.IDENTITY, caller())
    current = getattr(_operation, "current", None)
    if current and current[0] == key:
        yield current[1]
        return
    with act._lock(_caller_lock()):
        acquire()
        stop, mutex, errors = Event(), Lock(), []

        def renew():
            with mutex:
                if errors:
                    raise errors[0]
                acquire()

        def heartbeat():
            while not stop.wait(act.LEASE_IDLE / 3):
                try:
                    renew()
                except Exception as e:
                    errors.append(e)
                    return

        worker = Thread(target=heartbeat, name="web-window", daemon=True)
        _operation.current = (key, renew)
        worker.start()
        try:
            yield renew
        finally:
            _operation.current = current
            stop.set()
            worker.join()
            if release:
                with act._lock():
                    lease_path().unlink(missing_ok=True)
                if errors:
                    raise errors[0]
            else:
                renew()


def release() -> None:
    with act._lock(_caller_lock()), act._lock():
        lease_path().unlink(missing_ok=True)


def _infos(port: str) -> list[dict]:
    return [t for t in act.cdp(port, "Target.getTargets")["targetInfos"] if t.get("type") == "page"]


def _owned(data: dict, infos: list[dict]) -> list[dict]:
    # Keep historical IDs: a popup can outlive its opener and still belongs to the same caller.
    ids = set(data.get("targets", []))
    if data.get("target_id"):
        ids.add(data["target_id"])
    while True:
        children = {t["targetId"] for t in infos if t.get("openerId") is not None and t["openerId"] in ids}
        if children <= ids:
            break
        ids.update(children)
    found = [t for t in infos if t["targetId"] in ids]
    data["targets"] = list(dict.fromkeys([*data.get("targets", []), *(t["targetId"] for t in found)]))
    return found


def _close_targets(port: str, data: dict, infos: list[dict]) -> None:
    for target in reversed(_owned(data, infos)):
        result = act.cdp(port, "Target.closeTarget", targetId=target["targetId"])
        if not result.get("success"):
            raise act.ActError("couldn't close session target")


def _reap_locked(port: str, infos: list[dict]) -> None:
    for data in records():
        if data["owner"] == caller() or _live(data, time.time()):
            continue
        try:
            # An active operation is authoritative even if a heartbeat was delayed.
            with act._lock(_caller_lock(data["owner"]), blocking=False):
                _close_targets(port, data, infos)
                lease_path(owner=data["owner"]).unlink(missing_ok=True)
        except BlockingIOError:
            continue


def bind(port: str) -> str:
    """Find or create the caller's own window under the short identity lock."""
    with act._lock():
        infos = _infos(port)
        _reap_locked(port, infos)
        data = _read(lease_path()) or {"owner": caller(), "target_id": None}
        own = _owned(data, infos)
        ids = {t["targetId"] for t in own}
        if data["target_id"] not in ids:
            data["target_id"] = own[0]["targetId"] if own else act.cdp(
                port, "Target.createTarget", url="about:blank", newWindow=True, background=True)["targetId"]
            if data["target_id"] not in data["targets"]:
                data["targets"].append(data["target_id"])
        data["ts"] = time.time()
        _save(data)
        return data["target_id"]


def activate(port: str) -> str:
    target = bind(port)
    listed = act.agent_browser(["tab", "list", "--json"], port=port, capture=True, timeout=30)
    with contextlib.suppress(ValueError, KeyError, TypeError, AttributeError):
        if any(t.get("active") and t.get("targetId") == target for t in json.loads(listed.stdout)["data"]["tabs"]):
            return target   # already selected: re-selecting would drop the refs of this session's last snapshot
    result = act.agent_browser(["tab", target], port=port, capture=True, timeout=30)
    if result.returncode:
        raise act.ActError("couldn't select this session's browser target")
    return target


def page_targets(port: str) -> list[dict]:
    """Caller-only CDP metadata, also used by Google login and the phone relay."""
    with act._lock():
        data = _read(lease_path())
        if data is None:
            return []
        targets = _owned(data, _infos(port))
        _save(data)
    return [{**t, "id": t["targetId"]} for t in targets]


def close_locked(port: str | None) -> None:
    if port:
        infos = _infos(port)
        _reap_locked(port, infos)
        if data := _read(lease_path()):
            _close_targets(port, data, infos)
    lease_path().unlink(missing_ok=True)


def _tab_locked(port: str, args: list[str]) -> tuple[dict, bool]:
    """Update tab metadata under the identity lock; daemon selection happens afterwards."""
    rest = [a for a in args[1:] if a != "--json"]
    data = _read(lease_path())
    if data is None:
        raise act.ActError("session window record disappeared")
    targets = _owned(data, _infos(port))
    _save(data)
    labels = data.setdefault("labels", {})

    def row(t):
        tid = t["targetId"]
        return {"tabId": f"t{data['targets'].index(tid) + 1}", "targetId": tid,
                "url": t.get("url", ""), "title": t.get("title", ""),
                "active": tid == data["target_id"], **({"label": labels[tid]} if tid in labels else {})}

    rows = [row(t) for t in targets]
    if not rest or rest == ["list"]:
        result = {"tabs": rows}
    elif rest[0] == "new":
        operands, label = rest[1:], None
        if "--label" in operands:
            i = operands.index("--label")
            if i + 1 == len(operands):
                raise act.ActError("tab new --label needs a label", code=2)
            label = operands[i + 1]
            operands = operands[:i] + operands[i + 2:]
            if not label or label in labels.values():
                raise act.ActError("tab label must be nonempty and unique in this session", code=2)
        if len(operands) > 1 or any(a.startswith("--") for a in operands):
            raise act.ActError("use tab new [--label LABEL] [URL]", code=2)
        # CDP cannot choose an existing window for createTarget. Give explicit new tabs their own private window.
        tid = act.cdp(port, "Target.createTarget", url=operands[0] if operands else "about:blank",
                      newWindow=True, background=True)["targetId"]
        data["target_id"] = tid
        data["targets"].append(tid)
        if label:
            labels[tid] = label
        _save(data)
        result = row({"targetId": tid, "url": operands[0] if operands else "about:blank"})
    else:
        closing = rest[0] == "close"
        refs = rest[1:] if closing else rest
        if len(refs) > 1:
            raise act.ActError("use tab [close] [TAB]", code=2)
        ref = refs[0] if refs else data["target_id"]
        chosen = next((t for t in rows if ref in (t["tabId"], t["targetId"], t.get("label"))), None)
        if chosen is None:
            raise act.ActError("no such tab in this session", code=2)
        if closing:
            if not act.cdp(port, "Target.closeTarget", targetId=chosen["targetId"]).get("success"):
                raise act.ActError("couldn't close session target")
            result = {"closed": chosen["tabId"]}
        else:
            data["target_id"] = chosen["targetId"]
            _save(data)
            result = {**chosen, "active": True}
    return result, bool(rest and rest[0] not in ("list", "close"))


def tab(port: str, args: list[str]) -> subprocess.CompletedProcess:
    """Keep tab handles, labels and enumeration inside this caller's target set."""
    with act._lock():
        result, select = _tab_locked(port, args)
    if select:
        activate(port)
    if "--json" in args:
        out = json.dumps({"success": True, "data": result}) + "\n"
    elif "tabs" in result:
        out = "\n".join(f"{'*' if t['active'] else ' '} {t['tabId']} {t.get('label', '')} {t['url']}"
                        for t in result["tabs"]) + "\n"
    else:
        out = json.dumps(result) + "\n"
    return subprocess.CompletedProcess(args, 0, out, "")


def handoff_identity() -> str:
    try:
        name = (act.STATE_DIR / "handoff.identity").read_text().strip()
        return name if NAME.fullmatch(name) else "personal"
    except (OSError, ValueError):
        return "personal"


def handoff_busy() -> bool:
    from . import bg
    return (bool(act.handoff_pid() or bg.live_pid("handoff")) and handoff_identity() == act.IDENTITY
            and handoff_owner() in (None, caller()))


def handoff_owner() -> str | None:
    try:
        return (act.STATE_DIR / "handoff.owner").read_text().strip() or None
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def hold_handoff_identity():
    """Called under the global handoff claim; don't unlink a newer run's file."""
    path = act.STATE_DIR / "handoff.identity"
    name, owner = act.IDENTITY, act.marker_owner("handoff")
    act._write_private(path, name)
    owner_path = act.STATE_DIR / "handoff.owner"
    act._write_private(owner_path, caller())
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            if path.read_text() == name and act.marker_owner("handoff") == owner:
                path.unlink()
                owner_path.unlink(missing_ok=True)


def running() -> list[str]:
    result = act._systemctl("list-units", "--state=active", "--plain", "--no-legend", "web-identity-browser*")
    if result.returncode:
        raise act.ActError("couldn't count running identity browsers")
    names = []
    for line in result.stdout.splitlines():
        unit = line.split()[0] if line.split() else ""
        if unit == "web-identity-browser.service":
            names.append("personal")
        elif unit.startswith("web-identity-browser-") and unit.endswith(".service"):
            names.append(unit[len("web-identity-browser-"):-len(".service")])
    return sorted(set(names))


def temperature() -> float | None:
    for sensor in HWMON_DIR.glob("hwmon*"):
        with contextlib.suppress(OSError, ValueError):
            if (sensor / "name").read_text().strip() == "coretemp":
                return int((sensor / "temp1_input").read_text()) / 1000
    return None


def guard_launch() -> None:
    names = running()
    if len(names) >= LANES["act"]["max_browsers"]:
        raise act.ActError(f"identity browser cap ({LANES['act']['max_browsers']}) reached: {', '.join(names)}; "
                           "use `web act --as NAME close` on an unused identity", code=3)
    t = temperature()
    if t is not None and t >= LANES["act"]["max_launch_temp_c"]:
        raise act.ActError(f"odin is at {t:g}°C; not starting another browser", code=3)


def names() -> list[str]:
    return ["personal", *sorted(p.name for p in (act.STATE_DIR / "identities").glob("*")
                               if p.is_dir() and p.name != "personal")]


def status() -> list[dict]:
    active, now = running(), time.time()
    rows = []
    for name in names():
        profile = act.STATE_DIR / ("identity" if name == "personal" else f"identities/{name}")
        windows = []
        for data in records(name):
            if _live(data, now) and data.get("target_id"):
                kind = data["owner"].split(":", 1)[0]
                windows.append({"owner_kind": kind if kind in ("claude", "codex", "terminal", "forwarded") else None,
                                "target_id": data["target_id"], "idle_s": max(0, int(now - data["ts"]))})
        rows.append({"name": name, "profile": profile.exists(), "running": name in active,
                     "windows": windows})
    return rows
