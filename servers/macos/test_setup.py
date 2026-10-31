#!/usr/bin/env python3
"""test_setup.py: runs servers/macos/setup.sh for real in a throwaway Debian container that stands in for the Mac:
uname, id, scutil, pmset, launchctl, brew, sudo and the rest are stand-ins that log what they are asked (sudo just
runs the command; the container runs as root). Needs Docker; run it on a test box, from anywhere:
    python3 servers/macos/test_setup.py
Nothing outside the containers is touched; they are removed at the end. About a minute.
  1. every lane's --dry-run plan runs to the end and writes nothing
  2. every lane for real: each LaunchDaemon plist parses (plistlib). A job without a UserName runs as root, so it
     may run only macOS's own programs and use root-owned paths no other account can change: nothing from Homebrew's
     prefix, /usr/local or a home folder. wake-watch runs as you with /usr/bin/python3 -I, its state and log folders
     are yours, and alert.conf gets a read entry for you. Tailscale comes as the signed app (cask tailscale-app);
     nothing runs brew through sudo
  3. a second run rewrites no plist and reloads no job
  4. over an earlier run's root jobs (which the check in 2 must flag): Homebrew's root tailscaled is booted out and
     its plist kept as a dotfile (launchd skips those), wake-watch's root job becomes yours, and a pause (HALT) in the
     old state folder stays a pause
  5. run from an SSH session that came in over Tailscale, Homebrew's tailscaled is left running (retiring it would
     cut the session and the run off), with a warning and a re-run step; a re-run over the home network retires it.
     on_tailnet tells tailnet addresses (100.64.0.0/10, fd7a:115c:a1e0::/48) from others
  6. when SSH_CONNECTION may be stale (audit R3-3) it is left running too: inside tmux with a LAN value left from
     when the shell started, inside tmux with none, inside screen, under a tmux process with TMUX unset, a LAN value
     with no sshd above the shell (an editor's remote server, mosh, nohup), none while someone else has the Mac's
     screen, none under sshd. A fresh direct ssh over the home network (sshd above the shell, no multiplexer) still
     retires it. unsure_why on a table of environments and process trees (macOS ps output: program paths, -zsh,
     "tmux: server (...)", screen-4.9.1), each unsure one printing why
  7. ways in that a fresh-looking session hides (round-3 review): an ssh to this Mac from itself (ssh you@mac.local
     typed inside a Tailscale session, a forward, a tunnel), a herdr pane made at the screen, and a terminal in Screen
     Sharing or any shell while an ssh or Screen Sharing connection from a Tailscale address is open (macOS netstat):
     left running; LAN connections, outgoing ones and closed ones do not hold it back
  8. thor's pf rule before Ollama on every interface (audit R4-1), from a Mac with no pf rule: setup.sh loads the rule
     (the boot job's own commands, run synchronously) before it writes or starts the Ollama agent, and before it
     reloads the boot job, whose own run would race it. When installing the rule fails, loading it fails, or the run
     dies between the two, no agent is written (one an earlier run left, which starts Ollama without waiting, is
     stopped and set aside first) and nothing listens on every interface after a restart either, also when the earlier
     run's boot job loads the rule. The agent waits, as you, until the boot job has recorded this boot's id (kern.bootsessionuuid)
     after a successful load: at a restart it waits while the boot job is late, stays waiting when the load fails,
     although the record from the boot before is still there, and starts Ollama on 0.0.0.0:11434 once the load succeeds.
     A re-run whose load fails stops a running gated agent; the next good run starts it again; a second run changes
     nothing; a re-run over ssh with nobody logged in at the screen (no gui domain) finishes and leaves the agent to
     the next login. Root jobs' inline scripts are held to the rule in 2: every path in them root-owned
"""
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
IMG = "debian:stable"
ME = "tester"
PATH = "/stub:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SYSTEM = ("/sbin/", "/usr/sbin/", "/bin/", "/usr/bin/", "/usr/libexec/", "/System/")
USER_WRITABLE = ("/opt/homebrew", "/usr/local", "/Users/", "/home/", "/tmp/", "/private/tmp/", "/var/tmp/")

STUBS = {
    # every stand-in logs its call to /state/calls
    "uname": '[ "${1:-}" = -s ] && { echo Darwin; exit 0; }\nexec /usr/bin/uname "$@"\n',
    "id": f'case "$*" in -u) echo 501 ;; -un) echo {ME} ;; -g) echo 20 ;; *) exec /usr/bin/id "$@" ;; esac\n',
    "scutil": "echo testmac\n",
    "pmset": 'echo "pmset $*" >> /state/calls\n[ "${1:-}" != -g ] || printf " sleep 1\\n disksleep 10\\n womp 0\\n"\n',
    "generic": 'echo "${0##*/} $*" >> /state/calls\ncase "${0##*/} ${1:-}" in\n'
               '  "defaults read") exit 1 ;;\n  "systemsetup -getremotelogin") echo "Remote Login: Off" ;;\n'
               '  "xcode-select -p") echo /Library/Developer/CommandLineTools ;;\nesac\nexit 0\n',
    # /state/nogui: nobody is logged in at the screen, so there is no gui/501 domain (an ssh-only session)
    "launchctl": 'echo "launchctl $*" >> /state/calls\nmkdir -p /state/loaded\ncase "$1" in\n'
                 '  bootstrap) if [ "${2%%/*}" = gui ] && [ -e /state/nogui ]; then\n'
                 '      echo "Bootstrap failed: 125: Domain does not support specified action" >&2; exit 125; fi\n'
                 '    l=${3##*/}; touch "/state/loaded/${l%.plist}" ;;\n'
                 '  bootout) rm -f "/state/loaded/${2##*/}" ;;\n'
                 '  print) case "$2" in gui/*/*|system/*) [ -e "/state/loaded/${2##*/}" ] ;; *) [ ! -e /state/nogui ] ;; esac ;;\n'
                 'esac\n',
    "brew": 'echo "brew $*" >> /state/calls\nmkdir -p /state/brew\ncase "$1" in\n  --prefix) echo /opt/homebrew ;;\n'
            '  list) [ -e "/state/brew/$3" ] ;;\n  install) touch "/state/brew/${!#}" ;;\n'
            '  services) [ "$2" != start ] || touch "/state/brew/svc-$3"\n'
            '    [ "$2" != list ] || [ ! -e /state/brew/svc-colima ] || echo "colima started" ;;\nesac\n',
    "sudo": 'while [ $# -gt 0 ]; do case "$1" in -v) exit 0 ;; -*) shift ;; *) break ;; esac; done\n'
            'echo "sudo $*" >> /state/calls\nexec "$@"\n',
    # netstat -anW -p tcp: the connections in /state/netstat (macOS format), none without it
    "netstat": '[ ! -f /state/netstat ] || cat /state/netstat\n',
    # stat -f %Su: the owner; of /dev/console, who has the Mac's screen (/state/console)
    "stat": 'if [ "${1:-}" = -f ] && [ "${2:-}" = %Su ]; then shift 2\n'
            '  if [ "$1" = /dev/console ]; then cat /state/console; exit; fi\n'
            '  exec /usr/bin/stat -c %U "$@"; fi\nexec /usr/bin/stat "$@"\n',
    # ps -o ppid= -o comm= -p PID, the one form setup.sh uses, answered from /proc as macOS ps answers it (the program
    # path, padded); debian:stable has no ps
    "ps": 'for p; do :; done\n[ -r "/proc/$p/stat" ] || exit 1\ns=$(cat "/proc/$p/stat"); set -- ${s##*) }\n'
          'exe=$(readlink "/proc/$p/exe" 2>/dev/null) || exe=$(cat "/proc/$p/comm")\necho "   $2 $exe"\n',
    "chmod": 'if [ "${1:-}" = +a ]; then echo "chmod $*" >> /state/calls\n'
             '  printf "%s\\n" "$2" >> "/state/acl$(printf %s "$3" | tr / _)"; exit 0; fi\nexec /bin/chmod "$@"\n',
    "ls": 'if [ "${1:-}" = -le ]; then /bin/ls -l "$2" || exit\n'
          '  f="/state/acl$(printf %s "$2" | tr / _)"; [ ! -f "$f" ] || awk \'{print " " NR-1 ": " $0}\' "$f"; exit 0; fi\n'
          'exec /bin/ls "$@"\n',
    # at /usr/sbin/sysctl: this boot's id (/state/bootsession); a reboot writes a new one
    "sysctl": '[ "$*" = "-n kern.bootsessionuuid" ] && [ -s /state/bootsession ] || exit 1\ncat /state/bootsession\n',
    # at /sbin/pfctl: logs whether the Ollama agent was there at load time; /state/pf-fail fails the load,
    # /state/pf-interrupt kills the setup.sh run first (it dies after installing the rule, before loading it)
    "pfctl": f'a=/home/{ME}/Library/LaunchAgents/org.homeserver.ollama.plist\n'
             'if [ -e "$a" ]; then w=there; else w=absent; fi\necho "pfctl $* (agent $w)" >> /state/calls\n'
             'if [ -e /state/pf-interrupt ]; then for p in /proc/[0-9]*; do\n'
             '  case "$(tr "\\0" " " < "$p/cmdline" 2>/dev/null)" in *servers/macos/setup.sh*) kill -9 "${p#/proc/}" ;; esac\n'
             'done; exit 1; fi\n[ ! -e /state/pf-fail ] || { echo "pfctl: stand-in load failure" >&2; exit 1; }\n',
    # at /opt/homebrew/bin/ollama: records that Ollama was started, with which address, as whom
    "ollama": 'echo "ollama $* OLLAMA_HOST=${OLLAMA_HOST:-} uid=$(/usr/bin/id -u)" >> /state/ollama-ran\n',
}
GENERIC = ["systemsetup", "defaults", "xcode-select", "docker"]
PREPARE = f"""set -eu
useradd -u 501 -M -d /home/{ME} {ME}
mkdir -p /home/{ME} /stub /state /usr/libexec/ApplicationFirewall
for f in /stubsrc/*; do install -m 755 "$f" /stub/; done
for g in {' '.join(GENERIC)}; do ln -s generic /stub/$g; done
ln -s /stub/generic /usr/libexec/ApplicationFirewall/socketfilterfw
echo {ME} > /state/console
mkdir -p /opt/fake; for n in tmux screen sshd-session herdr; do cp /bin/bash /opt/fake/$n; done   # stand-in programs
install -m 755 /stubsrc/sysctl /usr/sbin/sysctl; install -m 755 /stubsrc/pfctl /usr/sbin/pfctl   # /sbin is /usr/sbin
mkdir -p /opt/homebrew/bin; install -m 755 /stubsrc/ollama /opt/homebrew/bin/ollama
echo 11111111-0000-4000-8000-000000000001 > /state/bootsession
touch /state/calls /state/ollama-ran /state/agent.log; chmod 666 /state/ollama-ran /state/agent.log
"""
# what an earlier run of setup.sh left (PREFIX /opt/homebrew): Homebrew's tailscaled and wake-watch, both as root
OLD_TAILSCALE = (b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key>'
                 b'<string>homebrew.mxcl.tailscale</string><key>ProgramArguments</key><array>'
                 b'<string>/opt/homebrew/opt/tailscale/bin/tailscaled</string></array><key>RunAtLoad</key><true/>'
                 b'</dict></plist>\n')
OLD_WAKE = (b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key>'
            b'<string>org.homeserver.wake-watch</string><key>ProgramArguments</key><array>'
            b'<string>/opt/homebrew/bin/python3</string><string>/usr/local/bin/wake-watch</string><string>check</string>'
            b'</array><key>StartInterval</key><integer>120</integer></dict></plist>\n')
# what an earlier run left for thor: Ollama on every interface at login, without waiting for the pf rule
OLD_OLLAMA = (b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key>'
              b'<string>org.homeserver.ollama</string><key>ProgramArguments</key><array>'
              b'<string>/opt/homebrew/bin/ollama</string><string>serve</string></array><key>EnvironmentVariables</key>'
              b'<dict><key>OLLAMA_HOST</key><string>0.0.0.0:11434</string></dict><key>RunAtLoad</key><true/>'
              b'<key>KeepAlive</key><true/></dict></plist>\n')
# and its pf boot job, which loaded the rule but recorded nothing
OLD_PF = (b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key>'
          b'<string>org.homeserver.pf</string><key>ProgramArguments</key><array><string>/sbin/pfctl</string>'
          b'<string>-E</string><string>-a</string><string>com.apple/homeserver</string><string>-f</string>'
          b'<string>/etc/pf.anchors/homeserver</string></array><key>RunAtLoad</key><true/></dict></plist>\n')
AGENT = f"/home/{ME}/Library/LaunchAgents/org.homeserver.ollama.plist"
READY = "/var/run/org.homeserver.pf.ready"


class Mac:
    def __init__(self, stubs):
        self.name = f"kit-mac-{uuid.uuid4().hex[:8]}"
        subprocess.run(["docker", "run", "-d", "--rm", "--name", self.name, "-v", f"{KIT}:/kit:ro",
                        "-v", f"{stubs}:/stubsrc:ro", IMG, "sleep", "1800"], check=True, capture_output=True)
        self.sh(PREPARE)

    def sh(self, script, check=True, input=None):
        r = subprocess.run(["docker", "exec", "-i", self.name, "bash", "-c", script], input=input,
                           capture_output=True)
        if check and r.returncode:
            raise RuntimeError(f"{script[:80]!r}: exit {r.returncode}\n{r.stderr.decode()[-2000:]}")
        return r

    def out(self, script):
        return self.sh(script).stdout.decode()

    def setup(self, lane, dry=False, ssh=None, env=(), via=None, umask=None):
        """via: a stand-in program in /opt/fake (tmux, screen, sshd-session, herdr) that runs setup.sh as its child, so it is
        above it in the process tree; env: more VAR=value for the run; umask: the run's umask."""
        e = ["-e", f"HOME=/home/{ME}", "-e", f"USER={ME}", "-e", f"PATH={PATH}"]
        if ssh:
            e += ["-e", f"SSH_CONNECTION={ssh}"]
        for v in env:
            e += ["-e", v]
        cmd = ["bash", "/kit/servers/macos/setup.sh", lane] + (["--dry-run"] if dry else [])
        if via:   # "; exit $?" keeps bash from replacing itself with setup.sh
            cmd = [f"/opt/fake/{via}", "-c", " ".join(cmd) + "; exit $?"]
        if umask:
            cmd = ["bash", "-c", f"umask {umask}; exec " + " ".join(cmd)]
        return subprocess.run(["docker", "exec", *e, "-w", f"/home/{ME}", self.name, *cmd],
                              capture_output=True, text=True)

    def put(self, path, data):
        self.sh(f'mkdir -p "$(dirname {path})" && cat > {path}', input=data)

    def close(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


def root_owned(mac, path):
    """Problems with path and its parents: each must belong to root and be writable by nobody else."""
    out = mac.out(f'p={path}; while :; do [ ! -e "$p" ] || stat -L -c "%u %a %n" "$p"; '
                  f'[ "$p" = / ] && break; p=$(dirname "$p"); done')
    return [f"{n} belongs to uid {u} or others may write it (mode {m})"
            for u, m, n in (line.split(" ", 2) for line in out.splitlines()) if u != "0" or int(m, 8) & 0o022]


def audit(mac):
    """Problems with the installed LaunchDaemons: root jobs that run or use anything another account can change,
    and a wake-watch job that is not yours, not macOS's python3 -I, or uses Homebrew's folder."""
    problems = []
    for n in mac.out("ls -A /Library/LaunchDaemons 2>/dev/null || true").split():
        if n.startswith("."):
            continue   # launchd skips dotfiles: the script's backups
        p = f"/Library/LaunchDaemons/{n}"
        try:
            d = plistlib.loads(mac.sh(f"cat {p}").stdout)
        except Exception as e:
            problems.append(f"{n}: not a valid plist ({e})")
            continue
        args = d.get("ProgramArguments") or [d.get("Program", "")]
        inline = [p for i, a in enumerate(args[:-1]) if a == "-c" for p in re.findall(r"(?<![\w.$/-])/[\w./-]+", args[i + 1])]
        paths = [a for a in args if a.startswith("/")] + inline + \
            [d[k] for k in ("StandardOutPath", "StandardErrorPath") if k in d]
        user = d.get("UserName", "root")
        if user == "root":
            if not args[0].startswith(SYSTEM):
                problems.append(f"{n}: root runs {args[0]}, not one of macOS's own programs")
            for a in paths:
                if a.startswith(USER_WRITABLE):
                    problems.append(f"{n}: root uses {a}, in a folder your account can change")
                problems += [f"{n}: {x}" for x in root_owned(mac, a)]
        elif args[0].startswith(("/opt/homebrew", "/usr/local/opt", "/usr/local/Cellar")):
            problems.append(f"{n}: runs {args[0]} from Homebrew's folder")
        if d.get("Label") == "org.homeserver.wake-watch" and (user != ME or args[:2] != ["/usr/bin/python3", "-I"]):
            problems.append(f"{n}: wake-watch should run as {ME} with /usr/bin/python3 -I, runs as {user}: {args}")
    return problems


def check(results, name, ok, detail=""):
    results.append((name, ok))
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok or not detail else f"\n     {detail}"))


def plists(mac):
    return mac.out("cd /Library/LaunchDaemons 2>/dev/null && md5sum $(ls -A) || true")


def reboot(mac):
    """A restart: a new boot id. The pf record from the boot before stays, so the agent must not take it for this one."""
    mac.sh(f"echo {uuid.uuid4()} > /state/bootsession")


def boot_job(mac):
    """launchd at boot: runs the pf LaunchDaemon (as root); its exit code, None when it is not installed."""
    p = "/Library/LaunchDaemons/org.homeserver.pf.plist"
    if mac.sh(f"test -f {p}", check=False).returncode:
        return None
    args = plistlib.loads(mac.sh(f"cat {p}").stdout)["ProgramArguments"]
    return subprocess.run(["docker", "exec", mac.name, *args], capture_output=True).returncode


def login(mac):
    """launchd at login: starts the Ollama agent as you, with its environment, in the background (stderr to
    /state/agent.log); False when there is no agent."""
    if mac.sh(f"test -f {AGENT}", check=False).returncode:
        return False
    d = plistlib.loads(mac.sh(f"cat {AGENT}").stdout)
    env = [x for k, v in d.get("EnvironmentVariables", {}).items() for x in ("-e", f"{k}={v}")]
    subprocess.run(["docker", "exec", "-d", "-u", "501:20", *env, mac.name, "/bin/sh", "-c", 'exec "$@" 2>>/state/agent.log',
                    "sh", *d["ProgramArguments"]], check=True, capture_output=True)
    return True


def ollama_started(mac, seconds):
    """What the stand-in Ollama recorded within `seconds` ('' = it was not started)."""
    end = time.time() + seconds
    while True:
        out = mac.out("cat /state/ollama-ran")
        if out or time.time() > end:
            return out
        time.sleep(0.5)


def logout(mac):
    """Stops the agents login() started and forgets what Ollama recorded."""
    mac.sh('for p in /proc/[0-9]*; do case "$(tr "\\0" " " < "$p/cmdline" 2>/dev/null)" in '
           '*"org.homeserver"".ollama"*) kill "${p#/proc/}" 2>/dev/null ;; esac; done; : > /state/ollama-ran; : > /state/agent.log')


def main():
    results = []
    stubs = Path(tempfile.mkdtemp(prefix="kit-mac-stubs-"))
    for name, body in STUBS.items():
        (stubs / name).write_text("#!/bin/bash\n" + body)
    stubs.chmod(0o755)
    macs = []
    try:
        # 1. dry runs
        mac = Mac(stubs)
        macs.append(mac)
        for lane in ("base", "loki", "heimdall", "thor"):
            r = mac.setup(lane, dry=True)
            check(results, f"dry run {lane}: runs to the end", r.returncode == 0, r.stderr[-800:])
            if lane == "heimdall":
                check(results, "dry run heimdall: Tailscale from the cask, no root brew",
                      "brew install --cask tailscale-app" in r.stdout and "services start tailscale" not in r.stdout
                      and not any(l.strip().startswith("+ sudo") and "brew" in l for l in r.stdout.splitlines()),
                      r.stdout[-1500:])
        wrote = mac.out(f"ls -d /Library /etc/homeserver /etc/pf.anchors /usr/local/bin/alert /var/lib/wake-watch {READY} "
                        f"/home/{ME}/Library 2>/dev/null || true")
        check(results, "dry runs write nothing", wrote.strip() == "", wrote)

        # 2. real runs, every lane
        for lane in ("heimdall", "thor", "loki"):
            r = mac.setup(lane)
            check(results, f"run {lane}: runs to the end", r.returncode == 0, r.stderr[-1500:])
        probs = audit(mac)
        check(results, "root jobs run only macOS's programs on root-owned paths; wake-watch is yours", not probs,
              "\n     ".join(probs))
        names = mac.out("ls /Library/LaunchDaemons").split()
        check(results, "both LaunchDaemons installed", names == ["org.homeserver.pf.plist", "org.homeserver.wake-watch.plist"],
              str(names))
        owners = mac.out("stat -c '%U %n' /var/lib/wake-watch /var/log/wake-watch 2>/dev/null || true")
        check(results, "wake-watch state and log folders are yours", owners.split() == [ME, "/var/lib/wake-watch", ME,
              "/var/log/wake-watch"], owners)
        acl = mac.out("cat /state/acl_etc_homeserver_alert.conf 2>/dev/null || true")
        check(results, "alert.conf readable by you (ACL entry), only once", acl == f"user:{ME} allow read\n", acl)
        calls = mac.out("cat /state/calls").splitlines()
        check(results, "nothing runs brew through sudo", not [c for c in calls if c.startswith("sudo") and "brew" in c],
              str([c for c in calls if c.startswith("sudo") and "brew" in c]))
        check(results, "Tailscale: the tailscale-app cask, no brew services for it",
              "brew install --cask tailscale-app" in calls and not [c for c in calls if "services" in c and "tailscale" in c])

        # 3. second run
        before, n = plists(mac), len(calls)
        r = mac.setup("heimdall")
        new = mac.out("cat /state/calls").splitlines()[n:]
        reloads = [c for c in new if c.startswith("launchctl boot") and "system" in c]
        check(results, "second run: no plist rewritten, no job reloaded", r.returncode == 0 and plists(mac) == before
              and not reloads and "+ chmod" not in r.stdout and "+ sudo chmod +a" not in r.stdout, f"{reloads}\n{r.stderr[-500:]}")

        # 4. over an earlier run's root jobs
        old = Mac(stubs)
        macs.append(old)
        old.put("/Library/LaunchDaemons/homebrew.mxcl.tailscale.plist", OLD_TAILSCALE)
        old.put("/Library/LaunchDaemons/org.homeserver.wake-watch.plist", OLD_WAKE)
        old.sh("mkdir -p /state/loaded /usr/local/var/wake-watch && touch /state/loaded/homebrew.mxcl.tailscale "
               "/state/loaded/org.homeserver.wake-watch /usr/local/var/wake-watch/HALT")
        flagged = audit(old)
        check(results, "the check flags the earlier run's root jobs (Homebrew python3 and tailscaled)",
              any("python3" in p for p in flagged) and any("tailscaled" in p for p in flagged), str(flagged))
        r = old.setup("heimdall")
        check(results, "run heimdall over the earlier run: runs to the end", r.returncode == 0, r.stderr[-1500:])
        probs = audit(old)
        check(results, "afterwards no root job runs Homebrew code; wake-watch is yours", not probs, "\n     ".join(probs))
        left = old.out("ls -A /Library/LaunchDaemons; ls /state/loaded").split()
        check(results, "Homebrew's root tailscaled booted out, its plist kept as a dotfile",
              "homebrew.mxcl.tailscale.plist" not in left and "homebrew.mxcl.tailscale" not in left
              and any(x.startswith(".homebrew.mxcl.tailscale.plist.bak-") for x in left)
              and any(x.startswith(".org.homeserver.wake-watch.plist.bak-") for x in left), str(left))
        check(results, "the pause in the old state folder stays a pause",
              old.sh("test -e /var/lib/wake-watch/HALT", check=False).returncode == 0)
        check(results, "next steps say to remove Homebrew's tailscaled and the old state folder",
              "brew uninstall tailscale" in r.stdout and "sudo rm -r /usr/local/var/wake-watch" in r.stdout)

        # 5. over Tailscale: left running; over the home network: retired
        src = (KIT / "servers/macos/setup.sh").read_text()

        def func(name):   # a function from setup.sh; missing: every case that uses it fails
            at = src.find(f"{name}() {{")
            return src[at:src.index("\n}\n", at) + 3] if at >= 0 else f"{name}() {{ return 2; }}\n"
        fn = func("tailnet_addr") + func("on_tailnet")
        # addresses built at runtime, so no tailnet- or LAN-shaped literal sits in the file (the privacy scrub flags them)
        ip = lambda *o: ".".join(map(str, o))
        over_ts = f"{ip(100, 101, 102, 103)} 50022 {ip(100, 90, 1, 2)} 22"
        over_lan = f"{ip(192, 168, 1, 20)} 50022 {ip(192, 168, 1, 10)} 22"
        cases = {over_ts: 0, f"{ip(192, 168, 1, 20)} 50022 {ip(100, 64, 0, 1)} 22": 0,
                 "fd7a:115c:a1e0::1 50022 fd7a:115c:a1e0::2 22": 0, "FD7A:115C:A1E0::1 50022 ::1 22": 0,
                 over_lan: 1, "100.63.255.1 50022 100.128.0.1 22": 1,
                 "10.0.0.5 50022 10.0.0.2 22": 1, "fd7a:115d::1 50022 fe80::1 22": 1, "": 1}
        wrong = []
        for conn, want in cases.items():
            got = subprocess.run(["bash", "-c", "set -euo pipefail\n" + fn + "\non_tailnet"],
                                 env={"PATH": "/usr/bin:/bin", "SSH_CONNECTION": conn}).returncode
            if got != want:
                wrong.append(f"{conn!r}: {got}")
        check(results, "on_tailnet: tailnet addresses yes, others no", not wrong, str(wrong))

        # 6a. unsure_why: ps and stat stand in as shell functions. FAKE_TREE: this shell, its parent, ... up to the one
        # whose parent is launchd (macOS ps -o comm prints the program's path, a login shell as -zsh)
        fake = r"""ME=tester
ps() {
  local p=${!#} n IFS='|'
  [ "$FAKE_TREE" != FAIL ] || return 1
  local -a t=($FAKE_TREE)
  if [ "$p" = "$$" ]; then n=0; else n=$((p - 9000)); fi
  [ "$n" -ge 0 ] && [ "$n" -lt "${#t[@]}" ] || return 1
  if [ $((n + 1)) -lt "${#t[@]}" ]; then echo "  $((9001 + n)) ${t[$n]}"; else echo "    1 ${t[$n]}"; fi
}
stat() { echo "$FAKE_CONSOLE"; }
netstat() { printf '%s' "${FAKE_NETSTAT:-}"; }
"""
        lan_ssh = "bash|-zsh|/usr/libexec/sshd-session|/usr/sbin/sshd"
        terminal = "bash|-zsh|/usr/bin/login|/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"
        in_tmux = "bash|-zsh|/opt/homebrew/bin/tmux"
        tmux_set = "TMUX=/private/tmp/tmux-501/default,4242,0"
        mac_lan, mac_ts, peer_ts = ip(192, 168, 1, 10), ip(100, 90, 1, 2), ip(100, 101, 102, 103)
        self_ssh = f"{mac_lan} 50022 {mac_lan} 22"   # ssh you@mac.local typed on the Mac itself
        # macOS netstat -anW -p tcp lines: local address.port, foreign address.port, state
        ns = lambda *rows: "FAKE_NETSTAT=Active Internet connections (including servers)\n" \
            "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)\n" + \
            "".join(f"tcp4       0      0  {l:<22} {f:<22} {st}\n" for l, f, st in rows) + \
            "tcp4       0      0  *.22                   *.*                    LISTEN\n"
        ts_ssh_open = ns((f"{mac_ts}.22", f"{peer_ts}.50022", "ESTABLISHED"))
        ts_vnc_open = ns((f"{mac_ts}.5900", f"{peer_ts}.50100", "ESTABLISHED"))
        ts6_ssh_open = ns(("fd7a:115c:a1e0::2.22", "fd7a:115c:a1e0::1.50022", "ESTABLISHED"))
        harmless = ns((f"{mac_lan}.22", f"{ip(192, 168, 1, 20)}.50022", "ESTABLISHED"),     # ssh over the home network
                      (f"{mac_ts}.50200", f"{peer_ts}.22", "ESTABLISHED"),                  # this Mac's own ssh out
                      (f"{mac_ts}.22", f"{peer_ts}.50023", "TIME_WAIT"),                    # one that is over
                      (f"{mac_ts}.8090", f"{peer_ts}.50300", "ESTABLISHED"))                # ntfy, not a way in
        # (environment, process tree, who has the Mac's screen) -> 1 = SSH_CONNECTION can be believed, 0 = unsure
        table = [
            ((f"SSH_CONNECTION={over_lan}", "TERM=xterm-256color"), lan_ssh, ME, 1, "fresh ssh over the home network"),
            ((f"SSH_CONNECTION={over_lan}",), lan_ssh, "root", 1, "fresh ssh, nobody at the screen"),
            (("TERM=xterm-256color",), terminal, ME, 1, "Terminal at the Mac's screen"),
            ((), "bash|-zsh|/Applications/Visual Studio Code.app/Contents/MacOS/Electron", ME, 1, "a path with spaces"),
            ((tmux_set, f"SSH_CONNECTION={over_lan}", "TERM=tmux-256color"), in_tmux, ME, 0, "tmux, stale LAN value"),
            ((tmux_set,), in_tmux, ME, 0, "tmux, no SSH_CONNECTION"),
            (("STY=4242.ttys001.testmac", f"SSH_CONNECTION={over_lan}"), "bash|-zsh|/usr/bin/screen", ME, 0,
             "screen, stale LAN value"),
            ((f"SSH_CONNECTION={over_lan}", "TERM=screen-256color"), lan_ssh, ME, 0, "TERM=screen-256color"),
            ((f"SSH_CONNECTION={over_lan}", "TERM=tmux-256color"), lan_ssh, ME, 0, "TERM=tmux-256color"),
            ((f"SSH_CONNECTION={over_lan}",), in_tmux, ME, 0, "a tmux program above, TMUX unset"),
            ((), "bash|zsh|tmux: server (/private/tmp/tmux-501/default)", ME, 0, "tmux server with a process title"),
            ((f"SSH_CONNECTION={over_lan}",), "bash|-zsh|SCREEN", ME, 0, "SCREEN above"),
            ((), "bash|-bash|/opt/homebrew/Cellar/screen/4.9.1/bin/screen-4.9.1", ME, 0, "screen-4.9.1 above"),
            ((), terminal, "root", 0, "no SSH_CONNECTION, nobody at the screen"),
            ((), terminal, "guest", 0, "no SSH_CONNECTION, someone else at the screen"),
            ((), "bash|-bash|su|-zsh|/usr/libexec/sshd-session|/usr/sbin/sshd", ME, 0, "no SSH_CONNECTION under sshd"),
            ((f"SSH_CONNECTION={over_lan}",), "FAIL", ME, 0, "process tree unreadable"),
            ((f"SSH_CONNECTION={over_lan}",), "bash|-zsh|/Users/tester/.vscode-server/bin/node", ME, 0,
             "a LAN value handed down, no sshd above (an editor's remote server)"),
            ((f"SSH_CONNECTION={over_lan}",), "bash|-zsh|/opt/homebrew/bin/mosh-server", ME, 0, "a LAN value under mosh"),
            ((f"SSH_CONNECTION={self_ssh}", "TERM=xterm-256color"), lan_ssh, ME, 0, "ssh from this Mac to itself"),
            ((f"SSH_CONNECTION={ip(127, 0, 0, 1)} 50022 {ip(127, 0, 0, 1)} 22",), lan_ssh, ME, 0,
             "ssh through a forward or tunnel (loopback)"),
            (("SSH_CONNECTION=::1 50022 ::1 22",), lan_ssh, ME, 0, "ssh over IPv6 loopback"),
            (("HERDR_ENV=1", "TERM=xterm-256color"), "bash|-zsh|/usr/bin/herdr", ME, 0, "herdr, HERDR_ENV set"),
            (("TERM=xterm-256color",), "bash|-zsh|/usr/local/bin/herdr", ME, 0, "a herdr program above"),
            (("ZELLIJ=0",), "bash|-zsh|/opt/homebrew/bin/zellij", ME, 0, "zellij, ZELLIJ set"),
            ((), "bash|-zsh|/opt/homebrew/bin/zellij", ME, 0, "a zellij program above"),
            ((), "bash|-zsh|/opt/homebrew/bin/dtach", ME, 0, "a dtach program above"),
            ((), "bash|-zsh|abduco", ME, 0, "an abduco program above"),
            (("TERM=xterm-256color", ts_vnc_open), terminal, ME, 0, "Terminal in Screen Sharing over Tailscale"),
            (("TERM=xterm-256color", ts_ssh_open), terminal, ME, 0, "Terminal at the screen, an ssh from Tailscale open"),
            ((f"SSH_CONNECTION={over_lan}", "TERM=xterm-256color", ts_ssh_open), lan_ssh, ME, 0,
             "fresh ssh over the home network, another from Tailscale open"),
            ((f"SSH_CONNECTION={over_lan}", ts6_ssh_open), lan_ssh, ME, 0, "an ssh from a Tailscale IPv6 address open"),
            ((f"SSH_CONNECTION={over_lan}", "TERM=xterm-256color", harmless), lan_ssh, ME, 1,
             "fresh ssh, LAN, outgoing, closed and non-ssh connections only"),
            (("TERM=xterm-256color", harmless), terminal, ME, 1, "Terminal at the screen, harmless connections only"),
        ]
        wrong = []
        for env, tree, console, want, label in table:
            r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + fake + fn + func("unsure_why") + "\nunsure_why"],
                               env={"PATH": "/usr/bin:/bin", "FAKE_TREE": tree, "FAKE_CONSOLE": console,
                                    **dict(v.split("=", 1) for v in env)}, capture_output=True, text=True)
            if r.returncode != want or (want == 0) != bool(r.stdout.strip()):
                wrong.append(f"{label}: {r.returncode} {r.stdout.strip()!r} {r.stderr.strip()[-200:]!r}")
        check(results, f"unsure_why: {len(table)} environments and process trees, unsure ones say why", not wrong,
              "\n     ".join(wrong))
        ts = Mac(stubs)
        macs.append(ts)
        ts.put("/Library/LaunchDaemons/homebrew.mxcl.tailscale.plist", OLD_TAILSCALE)
        ts.sh("mkdir -p /state/loaded && touch /state/loaded/homebrew.mxcl.tailscale")
        r = ts.setup("base", ssh=over_ts)
        calls = ts.out("cat /state/calls")
        check(results, "over Tailscale: Homebrew's tailscaled left running, with a warning and a re-run step",
              r.returncode == 0 and ts.sh("test -e /Library/LaunchDaemons/homebrew.mxcl.tailscale.plist && "
                                          "test -e /state/loaded/homebrew.mxcl.tailscale", check=False).returncode == 0
              and "bootout system/homebrew.mxcl.tailscale" not in calls and "left running" in r.stderr
              and "re-run: " in r.stdout and "open Tailscale from Applications" not in r.stdout,
              f"{r.returncode} {r.stderr[-600:]}")

        # 6b. SSH_CONNECTION may be stale: left running, each time. The shell was started at the screen or over the
        # home network, and you attach to it now over Tailscale.
        tmux_env = ["TMUX=/private/tmp/tmux-501/default,4242,0", "TERM=tmux-256color"]
        stale = [("inside tmux, a LAN SSH_CONNECTION left from when the shell started", "tmux",
                  dict(ssh=over_lan, env=tmux_env, via="tmux")),
                 ("inside tmux, no SSH_CONNECTION", "tmux", dict(env=tmux_env, via="tmux")),
                 ("inside screen, a LAN SSH_CONNECTION left from when the shell started", "screen",
                  dict(ssh=over_lan, env=["STY=4242.ttys001.testmac", "TERM=screen"], via="screen")),
                 ("under a tmux process with TMUX and TERM cleared, no SSH_CONNECTION", "inside tmux (process",
                  dict(env=["TERM=xterm-256color"], via="tmux")),
                 ("a LAN SSH_CONNECTION with no sshd above the shell (an editor's server, mosh, nohup)",
                  "no sshd is above", dict(ssh=over_lan)),
                 ("no SSH_CONNECTION while someone else has the Mac's screen", "screen is guest's",
                  dict(console="guest")),
                 ("no SSH_CONNECTION under sshd", "runs under sshd", dict(via="sshd-session")),
                 ("an ssh from this Mac to itself (ssh you@mac.local typed in a Tailscale session)",
                  "comes from this Mac itself", dict(ssh=self_ssh, env=["TERM=xterm-256color"], via="sshd-session")),
                 ("a shell in a herdr pane made at the screen", "inside herdr", dict(via="herdr")),
                 ("a terminal in Screen Sharing over Tailscale", "port 5900",
                  dict(netstat=ts_vnc_open.split("=", 1)[1]))]
        for label, says, kw in stale:   # each case starts with the old service in place and running
            ts.put("/Library/LaunchDaemons/homebrew.mxcl.tailscale.plist", OLD_TAILSCALE)
            ts.put("/state/netstat", kw.pop("netstat", "").encode())
            ts.sh(f"touch /state/loaded/homebrew.mxcl.tailscale && echo {kw.pop('console', ME)} > /state/console")
            n = len(ts.out("cat /state/calls").splitlines())
            r = ts.setup("base", **kw)
            new = "\n".join(ts.out("cat /state/calls").splitlines()[n:])
            check(results, f"{label}: Homebrew's tailscaled left running, with the reason and a re-run step",
                  r.returncode == 0 and ts.sh("test -e /Library/LaunchDaemons/homebrew.mxcl.tailscale.plist && "
                                              "test -e /state/loaded/homebrew.mxcl.tailscale", check=False).returncode == 0
                  and "bootout system/homebrew.mxcl.tailscale" not in new and "left running" in r.stderr
                  and says in r.stderr and "outside tmux and screen, re-run: " in r.stdout,
                  f"{r.returncode} {r.stderr[-600:]}")
        ts.sh(f"echo {ME} > /state/console")

        # a fresh direct ssh over the home network: sshd above the shell, no multiplexer, no way in over Tailscale open
        ts.put("/state/netstat", harmless.split("=", 1)[1].encode())
        r = ts.setup("base", ssh=over_lan, env=["TERM=xterm-256color"], via="sshd-session")
        check(results, "then a fresh ssh over the home network: retired",
              r.returncode == 0 and ts.sh("test ! -e /Library/LaunchDaemons/homebrew.mxcl.tailscale.plist && "
                                          "test ! -e /state/loaded/homebrew.mxcl.tailscale", check=False).returncode == 0
              and "open Tailscale from Applications" in r.stdout, r.stderr[-600:])

        # 8. thor's pf rule before Ollama on every interface (R4-1), on a Mac with no pf rule yet
        pf = Mac(stubs)
        macs.append(pf)

        def run_thor(label, umask=None):
            n = len(pf.out("cat /state/calls").splitlines())
            r = pf.setup("thor", umask=umask)
            return r, pf.out("cat /state/calls").splitlines()[n:]

        def agent_state():
            return {"agent": pf.sh(f"test -f {AGENT}", check=False).returncode == 0,
                    "loaded": pf.sh("test -e /state/loaded/org.homeserver.ollama", check=False).returncode == 0,
                    "ready": pf.out(f"cat {READY} 2>/dev/null || true").strip(), "boot": pf.out("cat /state/bootsession").strip()}

        def nothing_after_restart(guard_ok):
            """a restart, the boot job (loading or failing), a login: Ollama must not start on any interface"""
            if not guard_ok:
                pf.sh("touch /state/pf-fail")
            reboot(pf)
            rc = boot_job(pf)
            pf.sh("rm -f /state/pf-fail")
            started = login(pf)
            out = ollama_started(pf, 3)
            logout(pf)
            return rc, started, out

        # 8a. installing the rule fails: setup stops before anything about Ollama
        pf.sh("touch /etc/pf.anchors")   # a file where the folder should be: writing the rule fails
        r, new = run_thor("install fails")
        st = agent_state()
        rc, started, out = nothing_after_restart(True)
        check(results, "R4-1 the pf rule cannot be installed: setup stops; no Ollama agent written or started, none after "
              "a restart", r.returncode != 0 and not st["agent"] and not st["ready"]
              and not [c for c in new if c.startswith("launchctl bootstrap gui/")] and rc is None and not started and out == "",
              f"{r.returncode} {st} {rc} {started} {out!r} {r.stderr[-400:]}")
        pf.sh("rm -f /etc/pf.anchors")

        # 8b. loading it fails
        pf.sh("touch /state/pf-fail")
        r, new = run_thor("load fails")
        pf.sh("rm -f /state/pf-fail")
        st = agent_state()
        installed = pf.sh("test -f /etc/pf.anchors/homeserver && test ! -e /Library/LaunchDaemons/org.homeserver.pf.plist",
                          check=False).returncode == 0
        loads = [c for c in new if c.startswith("pfctl ")]
        rc_fail, started_fail, out_fail = nothing_after_restart(False)
        rc_ok, started_ok, out_ok = nothing_after_restart(True)
        check(results, "R4-1 the pf rule fails to load: setup says so and stops before the boot job and the Ollama agent; "
              "after a restart nothing listens",
              r.returncode == 1 and "the pf rule did not load" in r.stderr and installed and not st["agent"]
              and not st["ready"] and loads == ["pfctl -E -a com.apple/homeserver -f /etc/pf.anchors/homeserver (agent absent)"]
              and not [c for c in new if c.startswith("launchctl bootstrap")] and rc_fail is None and rc_ok is None
              and not started_fail and not started_ok and out_fail == out_ok == "",
              f"{r.returncode} {installed} {st} {loads} {new} {rc_fail} {rc_ok} {started_fail} {started_ok} {r.stderr[-400:]}")

        # 8c. the run dies after installing the rule, before loading it
        pf.sh(f"rm -f /etc/pf.anchors/homeserver {READY}; touch /state/pf-interrupt")
        r, new = run_thor("interrupted")
        pf.sh("rm -f /state/pf-interrupt")
        st = agent_state()
        installed = pf.sh("test -f /etc/pf.anchors/homeserver", check=False).returncode == 0
        rc, started, out = nothing_after_restart(True)
        check(results, "R4-1 the run dies between installing the pf rule and loading it: no Ollama agent, nothing listens "
              "after a restart", r.returncode == 137 and installed and not st["agent"] and not st["ready"]
              and not [c for c in new if c.startswith("launchctl bootstrap")] and rc is None and not started and out == "",
              f"{r.returncode} {installed} {st} {rc} {started} {out!r}")

        # 8d. what an earlier run left: an agent starting Ollama on every interface without waiting, and a boot job that
        # loads the rule but records nothing; then a load that fails
        pf.put(AGENT, OLD_OLLAMA)
        pf.put("/Library/LaunchDaemons/org.homeserver.pf.plist", OLD_PF)
        pf.sh("touch /state/loaded/org.homeserver.ollama /state/loaded/org.homeserver.pf /state/pf-fail")
        r, new = run_thor("old agent, load fails")
        pf.sh("rm -f /state/pf-fail")
        st = agent_state()
        aside = pf.out(f"ls -A /home/{ME}/Library/LaunchAgents").split()
        rc, started, out = nothing_after_restart(False)
        rc_ok, started_ok, out_ok = nothing_after_restart(True)
        check(results, "R4-1 an earlier run's agent (no wait for the rule) is stopped and set aside before the rule; with "
              "the load failing, nothing listens now or after a restart, whether its old boot job fails or loads the rule",
              r.returncode == 1 and not st["agent"] and not st["loaded"] and "stopped and set aside" in r.stderr
              and rc == 1 and rc_ok == 0 and not started_ok and out_ok == "" and not st["ready"]
              and any(a.startswith(".org.homeserver.ollama.plist.bak-") for a in aside)
              and "launchctl bootout gui/501/org.homeserver.ollama" in new
              and new.index("launchctl bootout gui/501/org.homeserver.ollama") < min(
                  [i for i, c in enumerate(new) if c.startswith("pfctl ")] or [len(new)])
              and not started and out == "", f"{r.returncode} {st} {aside} {new} {r.stderr[-400:]}")

        # 8e. a run that succeeds: the rule is loaded before the agent is written and started (umask 077: the record
        # must still be readable by you, or the agent would wait)
        r, new = run_thor("good run", umask="077")
        st = agent_state()
        d = plistlib.loads(pf.sh(f"cat {AGENT}").stdout)
        args = d["ProgramArguments"]
        load_at = [i for i, c in enumerate(new) if c.startswith("pfctl ")]
        boot_at = [i for i, c in enumerate(new) if c == f"launchctl bootstrap gui/501 {AGENT}"]
        job_at = [i for i, c in enumerate(new) if c == "launchctl bootstrap system /Library/LaunchDaemons/org.homeserver.pf.plist"]
        out = r.stdout
        check(results, "R4-1 a good run loads the pf rule (agent not yet there) before it writes and starts the agent, "
              "and before it reloads the boot job (whose own run would race it); it records this boot",
              r.returncode == 0 and load_at and boot_at and job_at and load_at[-1] < job_at[0] < boot_at[0]
              and new[load_at[0]].endswith("(agent absent)") and st["ready"] == st["boot"] and st["loaded"]
              and 0 <= out.find("+ load the rule now") < out.find(f"+ write {AGENT}"),
              f"{r.returncode} {new} {st} {r.stderr[-400:]}")
        check(results, "R4-1 the agent starts Ollama on 0.0.0.0:11434 only through the wait for this boot's pf record",
              args[:2] == ["/bin/sh", "-c"] and READY in args[2] and "kern.bootsessionuuid" in args[2]
              and args[3:] == ["org.homeserver.ollama", "/opt/homebrew/bin/ollama", "serve"]
              and d["EnvironmentVariables"]["OLLAMA_HOST"] == "0.0.0.0:11434" and d.get("KeepAlive") is True, str(d))
        probs = audit(pf)
        login(pf)
        out = ollama_started(pf, 6)
        logout(pf)
        check(results, "R4-1 then Ollama starts at login, as you, on every interface; root jobs still run only macOS's "
              "programs on root-owned paths (their inline scripts included)",
              out.strip() == "ollama serve OLLAMA_HOST=0.0.0.0:11434 uid=501" and not probs, f"{out!r} {probs}")

        # 8f. a restart with the boot job late, then failing, then loading the rule
        reboot(pf)   # the record from the boot before is still in place
        login(pf)
        late = ollama_started(pf, 4)
        log = pf.out("cat /state/agent.log")
        pf.sh("touch /state/pf-fail")
        rc_fail = boot_job(pf)
        pf.sh("rm -f /state/pf-fail")
        failed = ollama_started(pf, 4)
        gone = pf.out(f"cat {READY} 2>/dev/null || true")
        rc_ok = boot_job(pf)
        loaded = ollama_started(pf, 8)
        logout(pf)
        check(results, "R4-1 after a restart the agent waits while the boot job is late (an old record does not count) "
              "and while it fails, and starts Ollama once the rule loads",
              late == "" and "waiting for the pf rule" in log and rc_fail == 1 and failed == "" and gone == ""
              and rc_ok == 0 and loaded.strip() == "ollama serve OLLAMA_HOST=0.0.0.0:11434 uid=501",
              f"{late!r} {log!r} {rc_fail} {failed!r} {gone!r} {rc_ok} {loaded!r}")

        # 8g. a re-run whose load fails while the agent runs; then a good run; then one with nothing to do
        before = pf.out(f"md5sum {AGENT}")
        pf.sh("touch /state/pf-fail")
        r, new = run_thor("re-run, load fails")
        pf.sh("rm -f /state/pf-fail")
        st = agent_state()
        reboot(pf)
        login(pf)
        waits = ollama_started(pf, 3)
        logout(pf)
        check(results, "R4-1 a re-run whose pf rule fails stops the running agent (kept, still waiting for the rule)",
              r.returncode == 1 and st["agent"] and not st["loaded"] and not st["ready"] and pf.out(f"md5sum {AGENT}") == before
              and "launchctl bootout gui/501/org.homeserver.ollama" in new and waits == "", f"{r.returncode} {st} {new}")
        r, new = run_thor("good re-run")
        st = agent_state()
        r2, new2 = run_thor("second good run")
        check(results, "R4-1 the next good run starts it again; a second changes nothing about Ollama or its boot job",
              r.returncode == 0 and f"launchctl bootstrap gui/501 {AGENT}" in new and st["loaded"] and st["ready"] == st["boot"]
              and r2.returncode == 0 and not [c for c in new2 if c.startswith("launchctl boot")]
              and f"+ write {AGENT}" not in r2.stdout and "+ write /Library/LaunchDaemons" not in r2.stdout,
              f"{r.returncode} {new} {st} {r2.returncode} {new2}")
        # 8h. a re-run over ssh with nobody logged in at the screen: no gui domain, so the agent (unchanged, not loaded)
        # is left to the next login instead of failing the run at its last step
        pf.sh("rm -f /state/loaded/org.homeserver.ollama; touch /state/nogui")
        r, new = run_thor("re-run, no login session")
        pf.sh("rm -f /state/nogui")
        st = agent_state()
        check(results, "R4-1 a re-run over ssh with nobody logged in at the screen finishes; the agent is left to the next login",
              r.returncode == 0 and st["agent"] and not st["loaded"] and st["ready"] == st["boot"]
              and not [c for c in new if c.startswith("launchctl bootstrap gui/")] and "Next steps" in r.stdout,
              f"{r.returncode} {new} {st} {r.stderr[-400:]}")
    finally:
        for m in macs:
            m.close()
        for f in stubs.iterdir():
            f.unlink()
        stubs.rmdir()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
