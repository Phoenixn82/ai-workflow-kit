"""Bubblewrap wrapper for a flagged session's Bash: no network, protection read-only, host sockets masked.

Inside: the whole filesystem as usual (cwd, project writes, git, python), but no network namespace, a
fresh /run/user/<uid> (dbus, wayland, ssh-agent, the fallback taint store), /dev/null over every other
listening unix socket this user can reach (browser control, Herdr, Codex, docker), its own pid/ipc
namespaces, and no_new_privs (sudo fails).
"""
import os
from pathlib import Path
import shlex
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from policy import PROTECTED_PATHS

BWRAP = '/usr/bin/bwrap'
PREFIX = f'{BWRAP} --dev-bind / / '


def sockets(runtime: str) -> list[str]:
    """Listening unix sockets with a path this user can connect to, outside the runtime dir."""
    found = []
    try:
        lines = Path('/proc/net/unix').read_text().splitlines()[1:]
    except OSError:
        return found
    for line in lines:
        fields = line.split()
        path = fields[7] if len(fields) > 7 else ''
        if not path.startswith('/') or path in found or path.startswith(runtime + '/'):
            continue
        try:
            if stat.S_ISSOCK(os.stat(path).st_mode) and os.access(path, os.W_OK):
                found.append(path)
        except OSError:
            pass
    return found


def wrap(command: str) -> str:
    runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    args = ['--tmpfs', runtime]
    for path in PROTECTED_PATHS:
        if path.exists():
            args += ['--ro-bind', str(path), str(path)]
    for path in sockets(runtime):
        args += ['--ro-bind', '/dev/null', path]
    args += ['--unshare-net', '--unshare-pid', '--unshare-ipc', '--die-with-parent', '--new-session',
             '--', '/bin/bash', '-c', command]
    return PREFIX + ' '.join(shlex.quote(arg) for arg in args)
