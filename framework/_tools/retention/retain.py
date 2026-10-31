#!/usr/bin/env python3
"""Archive-first retention (audit 2026-10-12 H4). MOVES, never deletes, into
~/.local/share/archive/<YYYY-MM-DD>/<original absolute path>, with <date>/manifest.jsonl
(class, source, dest, size, mtime, sha256) written before each move.

  bak        *.bak* files idle > 14 d under ~/.claude ~/.codex ~/AI_Projects/_tools ~/AI_Projects/_system
             (skips git-tracked paths in any enclosing repo, and .git / node_modules trees)
  codex-img  ~/.codex/generated_images/<entry> idle > 21 d
  codex-arch ~/.codex/archived_sessions/<entry> idle > 21 d
  pre-merge  ~/.claude/projects/*/memory.pre-merge-2026-10-12 dirs (no age gate, per the plan)

The weekly run then records the Claude CLI version check (installed vs npm registry; never installs)
and re-applies the plugin-cache patches (PATCHES.md).
A real run first checks the global stop (_system/automations/HALT, then preflight.py, as lane-intel does) and
does nothing at all when it is blocked; --dry-run and restore skip that check.

usage: retain.py [--dry-run] [--over-1gb-ok]          archive + version check + repatch
       retain.py restore <manifest> [path] [--dry-run] move entries back (all, or one path / dir prefix)
       retain.py version-check [--dry-run]
       retain.py --self-test
"""
import argparse, datetime, errno, fnmatch, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
BAK_DAYS, CODEX_DAYS = 14, 21
LIMIT = 1 << 30  # ponytail: a run moving > 1 GiB stops for the user's line (H4), even though moves are reversible
SKIP_DIRS = {".git", "node_modules"}  # ponytail: node_modules *.bak are vendored package files, not our backups


def global_stop():
    """Why the global stop blocks a run (HALT file, then preflight.py: same gate as lane-intel's run.sh), else None."""
    auto = Path.home() / "AI_Projects/_system/automations"
    if (auto / "HALT").exists():
        return "HALT present"
    r = subprocess.run([sys.executable, str(auto / "preflight.py")], capture_output=True, text=True)
    return None if r.returncode == 0 else f"preflight blocked (rc {r.returncode}): {r.stdout.strip()[:200]}"


def archive_root():
    return Path.home() / ".local/share/archive"


def idle_days(p, now):
    # ponytail: newest of mtime AND ctime over the whole entry: `cp -p` backups keep the source's old mtime
    # (vault_rag.py.bak-20261012 was made today with a 09-01 mtime), ctime says when the copy appeared.
    newest = 0.0
    for f in [p, *(p.rglob("*") if p.is_dir() and not p.is_symlink() else ())]:
        st = f.lstat()
        newest = max(newest, st.st_mtime, st.st_ctime)
    return (now - newest) / 86400


def file_sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(p):
    """sha256 of a file, a symlink's target string, or a dir tree (sorted relpath + per-file sha)."""
    if p.is_symlink():
        return hashlib.sha256(b"L" + os.fsencode(os.readlink(p))).hexdigest()
    if not p.is_dir():
        return file_sha(p)
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        rel = os.fsencode(f.relative_to(p))
        if f.is_symlink():
            h.update(b"L " + rel + b" " + os.fsencode(os.readlink(f)) + b"\n")
        elif f.is_dir():
            h.update(b"D " + rel + b"\n")
        else:
            h.update(b"F " + rel + b" " + file_sha(f).encode() + b"\n")
    return h.hexdigest()


def size_of(p):
    if p.is_dir() and not p.is_symlink():
        return sum(f.lstat().st_size for f in p.rglob("*") if not f.is_dir() or f.is_symlink())
    return p.lstat().st_size


def tracked(p, cache):
    """True if p (or, for a dir, anything under it) is tracked by any enclosing git repo."""
    for repo in p.parents:
        if not (repo / ".git").exists():
            continue
        if repo not in cache:
            try:
                r = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True)
                cache[repo] = set(r.stdout.split(b"\0")) if r.returncode == 0 else None
            except OSError:  # no git binary
                cache[repo] = None
        files = cache[repo]
        if files is None:
            return True  # ponytail: unreadable repo -> treat as tracked and leave it alone
        rel = os.fsencode(p.relative_to(repo))
        if rel in files or (p.is_dir() and any(f.startswith(rel + b"/") for f in files)):
            return True
    return False


def plan(now=None):
    """-> (items, kept): items = [(cls, path, size)], kept = {cls: {reason: n}}."""
    now, home = now or time.time(), Path.home()
    items, kept, cache = [], {}, {}

    def consider(cls, p, days):
        try:
            consider_(cls, p, days)
        except OSError as e:  # vanished or unreadable mid-walk: leave it, say so
            print(f"skip {p}: {e}", file=sys.stderr)

    def consider_(cls, p, days):
        if days is not None and idle_days(p, now) <= days:
            kept.setdefault(cls, {}).setdefault("young", 0)
            kept[cls]["young"] += 1
        elif tracked(p, cache):
            kept.setdefault(cls, {}).setdefault("git-tracked", 0)
            kept[cls]["git-tracked"] += 1
        else:
            items.append((cls, p, size_of(p)))

    for cls, d in (("codex-img", home / ".codex/generated_images"), ("codex-arch", home / ".codex/archived_sessions")):
        if d.is_dir():
            for p in sorted(d.iterdir()):
                consider(cls, p, CODEX_DAYS)
    for p in sorted((home / ".claude/projects").glob("*/memory.pre-merge-2026-10-12")):
        if p.is_dir() and not p.is_symlink():
            consider("pre-merge", p, None)
    taken = [p for _, p, _ in items]
    for root in (home / ".claude", home / ".codex", home / "AI_Projects/_tools", home / "AI_Projects/_system"):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for n in sorted(filenames):
                p = Path(dirpath, n)
                if fnmatch.fnmatch(n, "*.bak*") and not any(p.is_relative_to(t) for t in taken):
                    consider("bak", p, BAK_DAYS)
    return items, kept


def move(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"{dst} already exists")
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        # ponytail: cross-device fallback = copy, verify, only then remove the source
        isdir = src.is_dir() and not src.is_symlink()
        shutil.copytree(src, dst, symlinks=True) if isdir else shutil.copy2(src, dst, follow_symlinks=False)
        if digest(dst) != digest(src):
            shutil.rmtree(dst) if isdir else dst.unlink()
            raise OSError(f"copy of {src} did not verify; source kept")
        shutil.rmtree(src) if isdir else src.unlink()


def mb(n):
    return f"{n / 1e6:.1f} MB"


def archive(dry, over_ok=False, now=None):
    items, kept = plan(now)
    classes = ("bak", "codex-img", "codex-arch", "pre-merge")
    for cls, p, size in items:
        print(f"{'would move' if dry else 'move'} [{cls}] {mb(size):>10}  {p}")
    total = sum(s for _, _, s in items)
    for cls in classes:
        sel = [s for c, _, s in items if c == cls]
        k = ", ".join(f"{r} {n}" for r, n in sorted(kept.get(cls, {}).items())) or "none"
        print(f"total [{cls}] {len(sel)} entries, {mb(sum(sel))} (kept: {k})")
    print(f"total {len(items)} entries, {mb(total)}")
    if dry or not items:
        return 0
    if total > LIMIT and not over_ok:
        print(f"refusing: {mb(total)} > 1 GiB; rerun with --over-1gb-ok after the user says yes", file=sys.stderr)
        return 3
    day = datetime.date.fromtimestamp(now or time.time()).isoformat()
    manifest, errors = archive_root() / day / "manifest.jsonl", 0
    manifest.parent.mkdir(parents=True, exist_ok=True)
    for cls, p, size in items:
        dest = archive_root() / day / str(p).lstrip("/")
        try:
            rec = {"class": cls, "source": str(p), "dest": str(dest), "size": size,
                   "mtime": p.lstat().st_mtime, "sha256": digest(p)}
            with open(manifest, "a") as fh:  # write-ahead: the line exists before the move
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            move(p, dest)
            if digest(dest) != rec["sha256"]:
                raise OSError(f"{dest} does not match its manifest sha256")
        except OSError as e:
            errors += 1
            print(f"error: {p}: {e}", file=sys.stderr)
    print(f"manifest: {manifest} ({len(items) - errors} moved, {errors} errors)")
    return 1 if errors else 0


def restore(manifest, path=None, dry=False):
    want = os.path.abspath(os.path.expanduser(path)) if path else None
    entries = [json.loads(l) for l in open(manifest) if l.strip()]
    if want:
        entries = [e for e in entries if want in (e["source"], e["dest"]) or e["source"].startswith(want.rstrip("/") + "/")]
    if not entries:
        print(f"nothing in {manifest} matches {path}", file=sys.stderr)
        return 1
    errors = 0
    for e in entries:
        src, dst = Path(e["source"]), Path(e["dest"])
        if not (dst.exists() or dst.is_symlink()):
            if (src.exists() or src.is_symlink()) and digest(src) == e["sha256"]:
                print(f"already in place: {src}")
            else:
                errors += 1
                print(f"error: {dst} missing and {src} not restored", file=sys.stderr)
            continue
        if src.exists() or src.is_symlink():
            errors += 1
            print(f"error: {src} exists; not overwriting (archive copy kept at {dst})", file=sys.stderr)
            continue
        if digest(dst) != e["sha256"]:
            errors += 1
            print(f"error: {dst} changed since archiving; not restoring", file=sys.stderr)
            continue
        print(f"{'would restore' if dry else 'restore'} {dst} -> {src}")
        if not dry:
            try:
                move(dst, src)
                if digest(src) != e["sha256"]:
                    raise OSError("restored copy does not match the manifest sha256")
            except OSError as ex:
                errors += 1
                print(f"error: {src}: {ex}", file=sys.stderr)
    return 1 if errors else 0


def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def version_check(dry):
    """Record installed vs registry Claude CLI version. Never installs: only `mise which`, `<bin> --version`,
    `npm view`; the wrapper is not executed (an Omarchy-regenerated one would `mise use -g` = install)."""
    home = Path.home()
    rec = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        rec["pinned"] = tomllib.loads((home / ".config/mise/config.toml").read_text())["tools"].get("claude")
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        rec["pinned"] = None
    try:
        # an Omarchy-regenerated wrapper has a live `mise use -g` line again (it floats the pin back to latest)
        rec["wrapper_pinned"] = not re.search(r"^\s*mise use", (home / ".local/bin/claude").read_text(), re.M)
    except OSError:
        rec["wrapper_pinned"] = None
    binary = run(["mise", "which", "claude"])
    out = run([binary, "--version"]) if binary else None
    rec["installed"] = out.split()[0] if out else None
    rec["registry"] = run(["npm", "view", "@anthropic-ai/claude-code", "version"])
    rec["update_available"] = bool(rec["installed"] and rec["registry"] and rec["installed"] != rec["registry"])
    print("version-check:", json.dumps(rec))
    if not dry:
        log = home / ".local/state/retention/versions.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    return rec


def repatch(dry):
    sys.stdout.flush()  # keep the journal in order: the children write straight to the same stdout
    rc = subprocess.run([sys.executable, str(HERE / "repatch.py"), *(["--dry-run"] if dry else [])]).returncode
    cdt = Path.home() / ".local/bin/chrome-devtools-repatch"
    if cdt.exists():
        if dry:  # ponytail: chrome-devtools-repatch ignores argv and always applies, so dry-run only names it
            print(f"would run {cdt}")
        else:
            rc |= subprocess.run([str(cdt)]).returncode
    return rc


def self_test():
    if not shutil.which("git"):
        print("git is not installed; retention self-test requires tracked-file fixtures")
        return 77
    real_home, real_path, real_rename = os.environ["HOME"], os.environ.get("PATH", ""), os.rename
    t0 = time.time()
    later = t0 + 30 * 86400  # test clock: fresh files look 30 days idle (ctime cannot be set backwards)
    with tempfile.TemporaryDirectory() as h:
        H = Path(h)
        os.environ["HOME"] = h
        try:
            # Exercise real git inside the fixture; enclosing repositories must not affect it.
            bin_ = H / "bin"
            bin_.mkdir()
            (bin_ / "real").mkdir()
            (bin_ / "real/git").symlink_to(shutil.which("git"))
            (bin_ / "git").write_text('#!/bin/sh\ncase "$2" in "$HOME"/*) exec "${0%/*}/real/git" "$@";; *) exit 0;; esac\n')
            (bin_ / "git").chmod(0o755)
            def mk(rel, data=b"x", mtime=None, mode=None):
                p = H / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                if mtime:
                    os.utime(p, (mtime, mtime))
                if mode:
                    p.chmod(mode)
                return p

            old = [mk(".claude/settings.json.bak-1", b"one\n", mode=0o600),
                   mk("AI_Projects/_system/a/b.md.bak-20261001", os.urandom(4096)),
                   mk(".codex/config.toml.bak-x", b"cfg"),
                   mk("AI_Projects/_tools/t/untracked.py.bak", b"u")]
            young = mk(".claude/hooks/x.py.bak-new", b"young", mtime=t0 + 25 * 86400)
            mk(".codex/node_modules/pkg/port.h.bak", b"vendored")
            mk("AI_Projects/_tools/t/tracked.py.bak", b"t")
            subprocess.run(["git", "init", "-q", str(H / "AI_Projects/_tools/t")], check=True)
            subprocess.run(["git", "-C", str(H / "AI_Projects/_tools/t"), "add", "tracked.py.bak"], check=True)
            img_old = mk(".codex/generated_images/s1/a.png", os.urandom(2048)).parent
            mk(".codex/generated_images/s2/b.png", b"new", mtime=t0 + 20 * 86400)  # young entry
            arch_old = mk(".codex/archived_sessions/rollout-1.jsonl", b"{}\n")
            pre = mk(".claude/projects/p/memory.pre-merge-2026-10-12/MEMORY.md", b"m").parent
            (pre / "link").symlink_to("MEMORY.md")
            mk(".claude/projects/p/memory.pre-merge-2026-10-12/x.md.bak", b"inside pre-merge")  # no double count
            moved = old + [img_old, arch_old, pre]
            snap = {p: (digest(p), p.lstat().st_mtime) for p in moved}

            os.environ["PATH"] = str(bin_)  # no real npm/mise calls during archive and dry-run checks
            r = subprocess.run([sys.executable, __file__, "--dry-run"], capture_output=True, text=True,
                               env={**os.environ, "PATH": str(bin_)})  # dry-run on the real clock: all young
            assert r.returncode == 0 and "total [bak] 0 entries" in r.stdout and "total [pre-merge] 1 entries" in r.stdout, r.stdout
            assert all(p.exists() for p in moved), "dry-run moved something"

            # the global stop (HALT, then preflight.py, as lane-intel's run.sh) blocks a real run before any move
            # (Astra round 1, #19); the pre-merge class has no age gate, so an unblocked run would move it at once
            auto = H / "AI_Projects/_system/automations"
            auto.mkdir(parents=True)
            for setup, why in (((auto / "HALT").touch, "HALT present"),
                               (lambda: ((auto / "HALT").unlink(), (auto / "preflight.py").write_text("raise SystemExit(2)\n")),
                                "preflight blocked")):
                setup()
                r = subprocess.run([sys.executable, __file__], capture_output=True, text=True, env={**os.environ, "PATH": "/nonexistent"})
                assert r.returncode == 0 and why in r.stdout and "move [" not in r.stdout, (why, r.stdout, r.stderr)
                assert all(p.exists() for p in moved), f"{why}: a blocked run moved something"
            shutil.rmtree(auto)

            global LIMIT
            LIMIT, saved = 1, LIMIT
            assert archive(dry=False, now=later) == 3 and all(p.exists() for p in moved), "1 GiB guard did not stop"
            LIMIT = saved

            calls = []

            def flaky_rename(a, b):  # first move takes the cross-device copy+verify+remove path
                if not calls:
                    calls.append(a)
                    raise OSError(errno.EXDEV, "simulated EXDEV")
                return real_rename(a, b)
            os.rename = flaky_rename
            assert archive(dry=False, now=later) == 0
            os.rename = real_rename
            assert calls, "EXDEV path not exercised"
            day = datetime.date.fromtimestamp(later).isoformat()
            man = archive_root() / day / "manifest.jsonl"
            recs = [json.loads(l) for l in open(man)]
            assert {r["source"] for r in recs} == {str(p) for p in moved}, recs
            for p in moved:
                assert not (p.exists() or p.is_symlink()), f"{p} still at source"
                assert (archive_root() / day / str(p).lstrip("/")).exists()
            for p in (young, H / ".codex/node_modules/pkg/port.h.bak", H / "AI_Projects/_tools/t/tracked.py.bak",
                      H / ".codex/generated_images/s2"):
                assert p.exists(), f"{p} should have been kept"

            one = str(old[0])
            assert restore(man, one) == 0 and old[0].exists() and not old[1].exists()
            mk(str(old[1].relative_to(H)), b"conflict")  # a new file where an archived one belongs
            assert restore(man) == 1, "restore overwrote an existing file"
            assert old[1].read_bytes() == b"conflict"
            old[1].unlink()
            assert restore(man) == 0
            for p, (sha, mtime) in snap.items():
                assert digest(p) == sha, f"{p} not byte-identical after restore"
                assert p.is_dir() or p.lstat().st_mtime == mtime, f"{p} mtime changed"
            assert old[0].stat().st_mode & 0o777 == 0o600
            assert restore(man) == 0, "second restore should be a no-op"

            calls_log = H / "calls.log"
            for name, body in (("mise", 'echo "$0 $*" >> "$LOG"; [ "$1" = which ] && echo "$(dirname "$0")/claude-bin"'),
                               ("npm", 'echo "$0 $*" >> "$LOG"; echo 2.1.297'),
                               ("claude-bin", 'echo "$0 $*" >> "$LOG"; echo "2.1.296 (Claude Code)"')):
                (bin_ / name).write_text(f"#!/bin/sh\n{body}\n")
                (bin_ / name).chmod(0o755)
            mk(".config/mise/config.toml", b'[tools]\nclaude = "2.1.296"\n')
            mk(".local/bin/claude", b'#!/bin/bash\n# the old one ran `mise use -g claude`\nexec mise x "claude" -- "claude" "$@"\n')
            os.environ.update(PATH=f"{bin_}:{real_path}", LOG=str(calls_log))
            assert version_check(dry=False)["installed"] == "2.1.296"
            rec = json.loads((H / ".local/state/retention/versions.jsonl").read_text())
            assert rec["installed"] == "2.1.296" and rec["registry"] == "2.1.297" and rec["update_available"], rec
            assert rec["pinned"] == "2.1.296" and rec["wrapper_pinned"] is True, rec
            mk(".local/bin/claude", b'#!/bin/bash\nexport MISE_MINIMUM_RELEASE_AGE=0\nmise use -g --quiet "claude" || exit 1\n')
            assert version_check(dry=True)["wrapper_pinned"] is False  # Omarchy-style wrapper is flagged
            assert len((H / ".local/state/retention/versions.jsonl").read_text().splitlines()) == 1
            log = calls_log.read_text()
            assert not any(w in log.split() for w in ("install", "use", "upgrade", "up", "i")), log
        finally:
            os.rename = real_rename
            os.environ.update(HOME=real_home, PATH=real_path)
    print("retain.py self-test: ok (plan, dry-run, 1 GiB guard, move incl. EXDEV path, restore one/all, "
          "no-overwrite, byte-identical + mtime, version check never installs)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", nargs="?", default="run", choices=("run", "restore", "version-check"))
    ap.add_argument("args", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--over-1gb-ok", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.cmd == "restore":
        if not 1 <= len(a.args) <= 2:
            ap.error("restore <manifest> [path]")
        return restore(a.args[0], *(a.args[1:]), dry=a.dry_run)
    if a.cmd == "version-check":
        return 0 if version_check(a.dry_run)["installed"] else 1
    stop = None if a.dry_run else global_stop()  # before any move, version log or repatch; restore stays manual
    if stop:
        print(stop)
        return 0
    rc = archive(a.dry_run, a.over_1gb_ok)
    version_check(a.dry_run)
    rp = repatch(a.dry_run)  # always: a stopped (exit 3) or partly failed archive pass must not skip the repatch
    return rc or rp


if __name__ == "__main__":
    sys.exit(main())
