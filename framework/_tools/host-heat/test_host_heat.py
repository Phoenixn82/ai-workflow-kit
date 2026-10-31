#!/usr/bin/env python3
"""Checks for audit 2026-10-13 S1, S9 and S13 (host-heat). Stdlib only; touches nothing outside its own temp dirs.
  python3 test_host_heat.py [HOST_HEAT]       S9: remote-install.sh installs only the private staging dir's
                                              regular file (normal user; every command that changes the
                                              system is a stub, so nothing is installed even on failure)
                                              S13: remote-install.sh, run into a temp root, keeps every command,
                                              env file and unit it replaces (also a symlink) under a backup name
                                              no earlier backup has; an identical rerun changes nothing
                                              (as root also: an identical command another account owns is
                                              replaced by root's, not left in place)
  sudo python3 test_host_heat.py [HOST_HEAT]  S1: a root `--check --dry-run` (the timer's code path, no actions)
                                              never writes, chowns or deletes through a symlinked CSV dir,
                                              ancestor or month file; a FIFO month file cannot stall it before
                                              the state save; the CSV is written as the user (`nobody`)
  sudo python3 test_host_heat.py --installed  S1 for the pair on this machine: /usr/local/bin/host-heat with
                                              /etc/host-heat/env (whichever installer ran last: the framework's
                                              or servers/linux/setup.sh). The same fixtures, owned by the env's
                                              HOST_HEAT_USER, with that user (not `nobody`) as the writer; with
                                              no HOST_HEAT_USER, the CSV dir must be root's alone
HOST_HEAT defaults to ./host-heat; pass an older copy to watch the S1 check fail on it."""
import glob
import importlib.machinery
import importlib.util
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True  # a root run must not leave a root-owned __pycache__ beside the tool


def load(path):
    loader = importlib.machinery.SourceFileLoader("host_heat", path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("host_heat", loader))
    loader.exec_module(mod)
    return mod


def tree(root):
    """Every path under root, not followed: mode, owner, size, mtime and the link target or file content."""
    out = {}
    for d, dirs, files in os.walk(root):
        for p in [d] + [os.path.join(d, n) for n in dirs + files]:
            st = os.lstat(p)
            body = (os.readlink(p) if stat.S_ISLNK(st.st_mode) else
                    open(p, "rb").read() if stat.S_ISREG(st.st_mode) else None)
            out[os.path.relpath(p, root)] = (st.st_mode, st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns, body)
    return out


def s1(path, installed=False):
    hh = load(path)
    # installed: the user the env names (host-heat's import put /etc/host-heat/env into os.environ), whether or not
    # this host-heat honours it; the fixtures then show who really writes
    owner = os.environ.get("HOST_HEAT_USER") if installed else "nobody"
    if installed and not owner:  # root writes the CSV itself: only safe where no other account controls a step
        p = os.path.abspath(hh.CSV_DIR)
        while True:
            st = os.lstat(p) if os.path.lexists(p) else None
            if st and (stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022):
                print(f"FAIL S1 installed: no HOST_HEAT_USER, so root writes the CSV in {hh.CSV_DIR}; {p} is not root's alone")
                return [f"S1 installed: {p}"]
            if p == "/":
                print(f"ok   S1 installed: no HOST_HEAT_USER; root writes the CSV in {hh.CSV_DIR}, root's alone")
                return []
            p = os.path.dirname(p)
    user = pwd.getpwnam(owner)
    month = time.strftime("%Y-%m") + ".csv"
    fails = []
    cases = ("CSV dir is a symlink", "an ancestor is a symlink", "the month file is a symlink", "the month file is a FIFO",
             "plain dir")

    class Stalled(Exception):  # not an OSError: host-heat's rd() swallows those
        pass

    def stalled(*_):
        raise Stalled("check() still blocked after 30 s")
    signal.signal(signal.SIGALRM, stalled)
    for case in cases:
        base = tempfile.mkdtemp(prefix="host-heat-s1.")
        try:
            os.chmod(base, 0o755)
            prot = os.path.join(base, "prot")  # stands in for /etc: root's, with a file old enough for the prune
            os.mkdir(prot, 0o755)
            keep = os.path.join(prot, "keep.csv")
            with open(keep, "w") as f:
                f.write("root-owned\n")
            os.utime(keep, (time.time() - 200 * 86400,) * 2)
            home = os.path.join(base, "home")  # the user's home: the user controls everything under it
            os.mkdir(home, 0o755)
            os.chown(home, user.pw_uid, user.pw_gid)

            def mine(p, target=None):
                os.symlink(target, p) if target else os.mkdir(p)
                os.lchown(p, user.pw_uid, user.pw_gid)
                return p
            csv_dir = {"CSV dir is a symlink": lambda: mine(f"{home}/a", prot),
                       "an ancestor is a symlink": lambda: mine(f"{home}/.local", prot) + "/state/host-heat",
                       "the month file is a symlink": lambda: mine(f"{home}/c"),
                       "the month file is a FIFO": lambda: mine(f"{home}/d"),
                       "plain dir": lambda: f"{home}/ok/host-heat"}[case]()
            if case == "the month file is a symlink":  # at a root file whose header fits, so a root append goes in
                r, fans = hh.sensors()
                with open(f"{prot}/target.csv", "w") as f:
                    f.write(",".join(["ts", "level"] + [x["key"] for x in r] + list(fans) + ["load1", "mit"]) + "\n")
                mine(f"{csv_dir}/{month}", f"{prot}/target.csv")
            if case == "the month file is a FIFO":  # opening it blocks: the writer must not hold up the state save
                os.mkfifo(f"{csv_dir}/{month}")
                os.lchown(f"{csv_dir}/{month}", user.pw_uid, user.pw_gid)
            hh.CSV_DIR, hh.STATE_DIR, hh.HALT_FILES = csv_dir, f"{base}/state", []
            if not installed:  # installed: the writer as configured
                hh.OWNER = user.pw_name
            before = tree(prot)
            signal.alarm(30)  # the fixed code kills a stuck writer after CSV_WAIT (10 s); older code blocks for good
            try:
                hh.check(True, None, {})  # dry run: no sysfs write, no poweroff; the CSV row is still written
            except Exception as e:  # noqa: BLE001 - the old code raised on some cases; the tree check decides
                print(f"  ({case}: check raised {type(e).__name__}: {e})")
            finally:
                signal.alarm(0)
            bad = []
            if tree(prot) != before:
                bad.append("root's fixture was changed through the symlink")
            if not os.path.exists(f"{base}/state/state.dry.json"):
                bad.append("check() did not finish (state not saved: the shutdown decision would be skipped)")
            if case == "plain dir":
                p = f"{csv_dir}/{month}"
                st = os.stat(p) if os.path.exists(p) else None
                if not st or st.st_uid != user.pw_uid or len(open(p).read().splitlines()) != 2:
                    bad.append("the CSV was not written (header + one row) as the user")
            print(f"{'FAIL' if bad else 'ok  '} S1 {case}{' (installed, as ' + user.pw_name + ')' if installed else ''}"
                  + "".join(f"\n       {b}" for b in bad))
            fails += bad
        finally:
            shutil.rmtree(base)
    return fails


def s9():
    me = pwd.getpwuid(os.getuid()).pw_name
    base = tempfile.mkdtemp(prefix="host-heat-s9.")
    fails = []
    try:
        stubs, log = f"{base}/bin", f"{base}/log"
        os.mkdir(stubs)
        for c in ("install", "cp", "rm", "mkdir", "runuser", "systemctl", "modprobe", "ln", "mv", "chmod", "chown"):
            with open(f"{stubs}/{c}", "w") as f:  # install stops the run: nothing past it is reached
                f.write(f'#!/bin/sh\necho "{c} $*" >> "{log}"\nexit {1 if c == "install" else 0}\n')
            os.chmod(f"{stubs}/{c}", 0o755)

        def stage(mode=0o700, src="file"):
            d = tempfile.mkdtemp(dir=base)
            os.chmod(d, mode)
            if src == "file":
                shutil.copy(f"{HERE}/host-heat", f"{d}/host-heat")
            elif src == "symlink":  # e.g. pointing at a file another account planted in /tmp
                open(f"{base}/planted", "w").close()
                os.symlink(f"{base}/planted", f"{d}/host-heat")
            elif src == "dir":
                os.mkdir(f"{d}/host-heat")
            return d
        good = stage()
        os.symlink(good, f"{base}/link")
        root = f"{base}/root"  # remote-install.sh's destinations go under it (empty: nothing to back up first)
        cases = [("private dir, regular file", me, good, 1),
                 ("no staging dir (the old 3-argument call)", me, "", 3),
                 ("staging dir does not exist", me, f"{base}/missing", 3),
                 ("staging dir is a symlink", me, f"{base}/link", 3),
                 ("staging dir is mode 755", me, stage(0o755), 3),
                 ("staging dir owned by another user", "root", good, 3),
                 ("host-heat is a symlink", me, stage(src="symlink"), 3),
                 ("host-heat is missing", me, stage(src=None), 3),
                 ("host-heat is a directory", me, stage(src="dir"), 3)]
        for name, user, d, want in cases:
            if os.path.exists(log):
                os.remove(log)
            r = subprocess.run(["bash", f"{HERE}/remote-install.sh", user, "-", "-", d], capture_output=True, text=True,
                               env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "HOST_HEAT_TEST_ROOT": root})
            ran = open(log).read().splitlines() if os.path.exists(log) else []
            ok = r.returncode == want and (ran[-1:] == [f"install -m 755 {d}/host-heat {root}/usr/local/bin/host-heat"]
                                           if want == 1 else not ran)
            print(f"{'ok  ' if ok else 'FAIL'} S9 {name}: exit {r.returncode}, "
                  f"{'reached: ' + ran[-1] if ran else 'no change attempted'}")
            if not ok:
                fails.append(f"S9 {name}: {r.stderr.strip()}")
    finally:
        shutil.rmtree(base)
    return fails


def s13():
    me = pwd.getpwuid(os.getuid()).pw_name
    base = tempfile.mkdtemp(prefix="host-heat-s13.")
    fails = []
    try:
        root, stubs = f"{base}/root", f"{base}/bin"
        os.mkdir(stubs)  # the real coreutils write into root; these only stand in for the system around it
        for c, body in (("systemctl", "echo active"), ("runuser", "exit 0"), ("modprobe", "exit 0"), ("lsblk", "exit 0"),
                        ("date", "echo 20261013-120000")):  # a fixed time: the backup names collide on purpose
            with open(f"{stubs}/{c}", "w") as f:
                f.write(f"#!/bin/sh\n{body}\n")
            os.chmod(f"{stubs}/{c}", 0o755)
        dest = {"command": f"{root}/usr/local/bin/host-heat", "env": f"{root}/etc/host-heat/env",
                "service": f"{root}/etc/systemd/system/host-heat.service",
                "timer": f"{root}/etc/systemd/system/host-heat.timer"}
        for p in dest.values():
            os.makedirs(os.path.dirname(p), exist_ok=True)
        stage = tempfile.mkdtemp(dir=base)
        shutil.copy(f"{HERE}/host-heat", f"{stage}/host-heat")

        def put(p, text, mode=0o644):
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, mode)

        def run(n):
            r = subprocess.run(["bash", f"{HERE}/remote-install.sh", me, "-", "-", stage], capture_output=True, text=True,
                               env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "HOST_HEAT_TEST_ROOT": root})
            if r.returncode:
                fails.append(f"S13 run {n} exited {r.returncode}: {r.stderr.strip()}")

        def kept(p):  # every backup of p: name -> content, or "-> target" for a symlink
            return {b: "-> " + os.readlink(b) if os.path.islink(b) else open(b).read()
                    for b in glob.glob(glob.escape(p) + ".bak-*")}

        def now():  # what each destination is: inode, mtime and content (or link target)
            return {k: (os.lstat(p).st_ino, os.lstat(p).st_mtime_ns, kept(p),
                        "-> " + os.readlink(p) if os.path.islink(p) else open(p).read()) for k, p in dest.items()}
        for k, p in dest.items():  # someone's own versions, and backups from before (one with this run's own name)
            put(p, f"custom-1 {k}\n", 0o755 if k == "command" else 0o644)
            put(f"{p}.bak-20261013-120000", f"earlier backup {k}\n")
        put(f"{dest['command']}.bak-20261003", "the old fixed-name backup\n")
        earlier = {k: kept(p) for k, p in dest.items()}
        run(1)
        for k, p in dest.items():  # customised again; the timer is now a symlink to a file elsewhere
            put(p, f"custom-2 {k}\n")
        os.remove(dest["timer"])
        put(f"{base}/elsewhere.timer", "custom-2 timer, linked\n")
        os.symlink(f"{base}/elsewhere.timer", dest["timer"])
        run(2)
        second = now()
        run(3)  # nothing differs now: nothing may be replaced or backed up
        third = now()
        for k, p in dest.items():
            backups = second[k][2]
            want = {f"custom-1 {k}\n", f"-> {base}/elsewhere.timer" if k == "timer" else f"custom-2 {k}\n",
                    *earlier[k].values()}
            bad = [f"lost: {w.strip()}" for w in sorted(want - set(backups.values()))]
            bad += [f"an earlier backup changed: {b}" for b, c in earlier[k].items() if backups.get(b) != c]
            bad += [f"executable backup: {b}" for b in backups if not os.path.islink(b) and os.stat(b).st_mode & 0o111]
            if os.path.islink(p) or second[k][3].startswith("custom"):
                bad.append("not replaced by the installer's version")
            if k == "command" and second[k][3] != open(f"{HERE}/host-heat").read():
                bad.append("the installed command is not the staged host-heat")
            if third[k] != second[k]:
                bad.append("the identical rerun replaced it or made another backup")
            print(f"{'FAIL' if bad else 'ok  '} S13 {k}: {len(backups)} backups kept" + "".join(f"\n       {b}" for b in bad))
            fails += [f"S13 {k}: {b}" for b in bad]
        if open(f"{base}/elsewhere.timer").read() != "custom-2 timer, linked\n" or os.path.islink(f"{base}/elsewhere.timer"):
            print("FAIL S13 the symlinked timer's target was changed")
            fails.append("S13 symlink target changed")
        if os.geteuid() == 0:  # identical, but another account owns it: root would run what nobody can change
            nobody, n = pwd.getpwnam("nobody"), len(kept(dest["command"]))
            os.chown(dest["command"], nobody.pw_uid, nobody.pw_gid)
            run(4)
            st = os.lstat(dest["command"])
            ok = (st.st_uid == 0 and not stat.S_ISLNK(st.st_mode) and len(kept(dest["command"])) == n + 1
                  and open(dest["command"]).read() == open(f"{HERE}/host-heat").read())
            print(f"{'ok  ' if ok else 'FAIL'} S13 an identical command owned by nobody is replaced by root's and kept")
            fails += [] if ok else ["S13 an identical command owned by nobody was left in place"]
    finally:
        shutil.rmtree(base)
    return fails


if __name__ == "__main__":
    args = sys.argv[1:]
    installed = "--installed" in args
    args = [a for a in args if a != "--installed"]
    target = os.path.abspath(args[0] if args else "/usr/local/bin/host-heat" if installed else f"{HERE}/host-heat")
    if os.geteuid() == 0:
        failed = s1(target, installed) + ([] if installed else s13())
        print("(S9 runs as a normal user: python3 test_host_heat.py)")
    elif installed:
        sys.exit("--installed needs root: sudo python3 test_host_heat.py --installed")
    else:
        failed = s9() + s13()
        print("(S1 needs root: sudo python3 test_host_heat.py)")
    sys.exit(f"{len(failed)} check(s) failed" if failed else 0)
