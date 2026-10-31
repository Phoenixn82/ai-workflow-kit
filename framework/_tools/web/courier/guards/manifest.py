"""What courier/install-guards.sh changed, so courier/uninstall-guards.sh undoes exactly that and nothing else.

    python3 manifest.py <manifest> <stamp> file <src> <dst> <mode>             install a file (created | replaced)
    python3 manifest.py <manifest> <stamp> register <json> <template> <hook>   hookreg add (an entry it added)
    python3 manifest.py <manifest> <stamp> patched <file> <backup>             a file the install patched
    python3 manifest.py <manifest> <stamp> undo <patch>                        the exact reverse

The manifest (JSON, 0600) maps each path to what an install did there, plus the sha256 of what the latest install
left in it, and lists the directories an install created:
- the guard's own files (hook script, rules): created (it wasn't there) or replaced (+ the backup of the original).
  The FIRST install's record stays, so it describes the file as it was before any install;
- shared files: registered (an entry the install added to settings.json / hooks.json; + whether the install created
  the file) or patched (the shared-log hook). These carry the backup the run that changed them made, since other
  tools edit them too.

undo: registrations first (no hook may point at a script that is already gone). A file still holding exactly what
the install wrote goes back byte for byte: deleted if the install created it, else restored from its backup. One
edited since: a registration has just the guard's entry removed (a file the install created is deleted once nothing
else is in it), the patch is reversed (later edits stay), and the guard's own files are left alone with a WARNING
(exit 1). Undone entries leave the manifest; once it is empty it is deleted, and so are the directories the install
created, if empty. A registration or file that was there before the install isn't in the manifest: never touched.
The hook script stays while a registration of it couldn't be removed.

No manifest but guard pieces present (an install from before the manifest): deregister the guard, delete the
hook script / rules only where they are byte-identical to the repo copies, reverse the shared-log patch; what
can't be undone that way is left with a WARNING (exit 1).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hookreg  # noqa: E402 - the sibling module, by path

PREFIX = "{:<10} {}"
REPO_RULES = Path(__file__).resolve().parent / "webcred.rules"
REPO_HOOK = Path(__file__).resolve().parents[1] / "webcred-guard.sh"


def _say(word: str, what) -> None:
    print(PREFIX.format(word + ":", what))


def _warn(what: str) -> None:
    print(f"WARNING:   {what}", file=sys.stderr)


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _load(manifest: Path) -> dict:
    try:
        data = json.loads(manifest.read_text())
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise SystemExit(f"WARNING:   {manifest} is not a guards manifest; not touching anything")
    data.setdefault("version", 1)
    data.setdefault("entries", {})
    data.setdefault("dirs", [])
    return data


def _mkdirs(data: dict, d: Path) -> None:
    """mkdir -p d, recording each directory it creates."""
    missing = []
    while not d.exists():
        missing.append(d)
        d = d.parent
    for x in reversed(missing):
        x.mkdir()
        data["dirs"].append(str(x))


def _write(path: Path, payload: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _save(manifest: Path, data: dict) -> None:
    _mkdirs(data, manifest.parent)
    _write(manifest, (json.dumps(data, indent=2) + "\n").encode(), 0o600)


def _note(data: dict, path: Path, entry: dict) -> None:
    """The first install's record stays (a shared file takes this run's backup); the sha256 follows what the latest
    install wrote."""
    e = data["entries"].setdefault(str(path), entry)
    if e["action"] in ("registered", "patched"):   # what this run's change can be undone from
        e.update({k: entry[k] for k in ("backup", "created") if k in entry})
    e["sha256"] = _sha(path)


def install_file(manifest: Path, stamp: str, src: Path, dst: Path, mode: str) -> int:
    payload = src.read_bytes()
    if dst.is_file() and dst.read_bytes() == payload:
        _say("unchanged", dst)
        return 0
    data = _load(manifest)
    if dst.exists():
        backup = f"{dst}.bak-webcred-{stamp}"
        shutil.copy2(dst, backup)
        _say("backup", backup)
        entry = {"action": "replaced", "backup": backup}
    else:
        entry = {"action": "created"}
        _mkdirs(data, dst.parent)
    _write(dst, payload, int(mode, 8))
    _say("installed", dst)
    _note(data, dst, entry)
    _save(manifest, data)
    return 0


def _has_guard(path: Path) -> bool:
    try:
        pre = json.loads(path.read_text()).get("hooks", {}).get("PreToolUse", [])
        return any(hookreg.ours(e) for e in pre)
    except (OSError, ValueError, AttributeError):
        return False


def register(manifest: Path, stamp: str, path: Path, template: str, hook: str) -> int:
    data = _load(manifest)
    was = json.dumps(data)
    existed, before = path.exists(), _has_guard(path)
    if not existed:
        _mkdirs(data, path.parent)
    rc = hookreg.main("add", str(path), template, hook, stamp)
    if rc == 0 and not before and _has_guard(path):   # hookreg backed an existing file up first
        _note(data, path, {"action": "registered", "created": not existed,
                           "backup": f"{path}.bak-webcred-{stamp}" if existed else None})
    if json.dumps(data) != was:
        _save(manifest, data)
    return rc


def patched(manifest: Path, path: Path, backup: str) -> int:
    data = _load(manifest)
    _note(data, path, {"action": "patched", "backup": backup})
    _save(manifest, data)
    return 0


def _undo_registration(path: Path, e: dict, stamp: str) -> bool:
    current, backup = _sha(path), Path(e.get("backup") or "")
    if current is None:
        _say("unchanged", f"{path} (not there)")
        return True
    if current == e.get("sha256"):   # untouched since the install: back to the bytes it had before
        if e.get("created"):
            path.unlink()
            _say("removed", f"{path} (the install created it)")
            return True
        if backup.is_file():
            shutil.copy2(backup, path)
            _say("restored", f"{path} (from {backup})")
            return True
    data = hookreg.load(str(path))
    if data is None:
        return False
    rest = hookreg.without_guard(data)
    if rest == data:
        _say("unchanged", f"{path} (no guard entry)")
        return True
    if e.get("created") and rest == {}:   # the install created the file and nothing else is in it
        path.unlink()
        _say("removed", f"{path} (the install created it)")
        return True
    return hookreg.main("remove", str(path), "", "", stamp) == 0


def _undo_file(path: Path, e: dict, stamp: str, patch: str) -> bool:
    current, installed = _sha(path), e.get("sha256")
    if e["action"] == "created":
        if current is None:
            _say("unchanged", f"{path} (not there)")
        elif current == installed:
            path.unlink()
            _say("removed", path)
        else:
            _warn(f"{path} changed since the install; left as it is (remove it by hand if it should go)")
            return False
        return True
    backup = Path(e.get("backup") or "")
    if (current is None or current == installed) and backup.is_file():   # replaced or patched, untouched since
        shutil.copy2(backup, path)
        _say("restored", f"{path} (from {backup})")
        return True
    if e["action"] == "patched" and current is not None and _reverse_patch(path, patch, stamp):
        return True
    why = "changed since the install" if backup.is_file() else f"its backup {backup} is gone"
    _warn(f"{path}: {why}; left as it is")
    return False


def _reverse_patch(path: Path, patch: str, stamp: str) -> bool:
    """Take the redaction patch back out (a dry run first; later edits elsewhere in the file stay)."""
    cmd = ["patch", "--reverse", "--silent", "--no-backup-if-mismatch", "-r", "-", str(path)]
    with open(patch) as p:
        if subprocess.run([cmd[0], "--dry-run", *cmd[1:]], stdin=p, capture_output=True).returncode != 0:
            return False
    shutil.copy2(path, f"{path}.bak-webcred-{stamp}")
    _say("backup", f"{path}.bak-webcred-{stamp}")
    with open(patch) as p:
        subprocess.run(cmd, stdin=p, check=True)
    _say("reverted", f"{path} (later edits kept)")
    return True


def _legacy(stamp: str, patch: str) -> int:
    """No manifest, but guard pieces are present (installed before install-guards.sh kept one): deregister the guard,
    delete its files only where they are byte-identical to the repo copies, reverse the shared-log patch. Anything
    it can't undo that way is left with a WARNING (exit 1)."""
    home = Path.home()
    hook, rules = home / ".claude/hooks" / hookreg.MARK, home / ".codex/rules/webcred.rules"
    registries = [home / ".claude/settings.json", home / ".codex/hooks.json"]
    log = home / ".claude/hooks/codex-claude-shared-log.sh"
    redacted = log.is_file() and "redact_web_cred" in log.read_text(errors="replace")
    if not (hook.exists() or rules.exists() or redacted or any(_has_guard(r) for r in registries)):
        _say("unchanged", "nothing recorded to undo and no guard installed here")
        return 0
    _say("legacy", "no manifest (an install from before it existed): undoing the guard as the repo describes it")
    ok = registered_off = True
    for r in registries:   # deregister first: no hook may point at a script that is already gone
        if not r.exists():
            continue
        data = hookreg.load(str(r))   # None (with a WARNING): can't tell, so it may still register the guard
        if data is None or (any(hookreg.ours(e) for e in data.get("hooks", {}).get("PreToolUse", []))
                            and hookreg.main("remove", str(r), "", "", stamp) != 0):
            ok = registered_off = False
    for path, repo in ((hook, REPO_HOOK), (rules, REPO_RULES)):
        if not path.exists():
            _say("unchanged", f"{path} (not there)")
        elif path == hook and not registered_off:
            _warn(f"kept {path}: a registration of it is still in place")
            ok = False
        elif path.read_bytes() == repo.read_bytes():
            path.unlink()
            _say("removed", path)
        else:
            _warn(f"{path} differs from the repo copy ({repo}); left as it is")
            ok = False
    if redacted and not _reverse_patch(log, patch, stamp):
        _warn(f"{log} changed since the redaction went in; take redact_web_cred out by hand")
        ok = False
    return 0 if ok else 1


def undo(manifest: Path, stamp: str, patch: str) -> int:
    if not manifest.exists():
        return _legacy(stamp, patch)
    data = _load(manifest)
    entries = data["entries"]
    ok = registered_off = True
    for path, e in sorted(entries.items(), key=lambda kv: kv[1].get("action") != "registered"):
        if e.get("action") == "registered":
            done = _undo_registration(Path(path), e, stamp)
            registered_off = registered_off and done
        elif Path(path).name == hookreg.MARK and not registered_off:   # a registration may still run it
            _warn(f"kept {path}: a registration of it could not be removed")
            done = False
        else:
            done = _undo_file(Path(path), e, stamp, patch)
        if done:
            del entries[path]
        ok = ok and done
    if entries:
        _save(manifest, data)
        return 1
    manifest.unlink()
    for d in reversed(data["dirs"]):
        try:
            os.rmdir(d)
            _say("removed", f"{d}/")
        except OSError:   # not empty (something else lives there now) or already gone
            pass
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    m, stamp, op, *rest = argv
    m = Path(m)
    if op == "file" and len(rest) == 3:
        return install_file(m, stamp, Path(rest[0]), Path(rest[1]), rest[2])
    if op == "register" and len(rest) == 3:
        return register(m, stamp, Path(rest[0]), rest[1], rest[2])
    if op == "patched" and len(rest) == 2:
        return patched(m, Path(rest[0]), rest[1])
    if op == "undo" and len(rest) == 1:
        return undo(m, stamp, rest[0])
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) if len(sys.argv) > 3 else __doc__)
