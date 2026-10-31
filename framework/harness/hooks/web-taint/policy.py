"""Trusted local results, and actions that reach outside the machine or touch the protection itself.

While a session is flagged these lists only route commands: anything not sent to the reviewer runs in
the no-network sandbox (sandbox.py), so a misclassified command fails safely instead of leaking.
"""
import os
from pathlib import Path
import re

LOCAL_TOOLS = frozenset(('Read', 'Grep', 'Glob'))
# ponytail: word lists, not a shell parser; a local script that sends data out is not caught.
NETWORK = re.compile(r'://|(?<![\w./-])(?:web[ \t]+(?:act|handoff|login|cred|intake|audit|sync-google|read|gh|'
                     r'search|repo|crawl|extract)|curl|wget|gh|https?|xh|lynx|w3m|yt-dlp|scrapling|'
                     r'agent-browser|social|youtube|example-intake|cx|codex|claude|lane)(?![\w-])')
SENDING = re.compile(r'(?<![\w./-])(?:git(?:\s+-C\s+\S+)?\s+push|vercel|netlify|npm\s+publish|tg|send\.py|'
                     r'rq\s+push|rsync|scp|ssh|sudo|systemd-run)(?![\w-])')
HOME = Path.home()
# Read-only inside the sandbox; Edit/Write targets resolving under these go to the reviewer.
PROTECTED_PATHS = tuple(HOME / part for part in (
    '.claude/hooks', '.claude/settings.json', '.claude/settings.local.json', '.codex', '.local/state/web-taint',
    'AI_Projects/_tools/web', 'AI_Projects/_system/skills/guard', '.local/bin', '.bashrc', '.bash_profile',
    '.profile', '.config/systemd', '.gitconfig', '.config/git', '.ssh', '.config/gh'))
PROTECTED = ('.claude/settings', '.claude/hooks', '.codex', '/etc/codex', '.local/state/web-taint', 'web-taint',
             'web untaint', 'web exempt', '_tools/web', '.local/bin', '.bashrc', '.profile', '.config/systemd')
OUTSIDE_PATHS = re.compile(r'(?:^|[\s\'"=:])(?:/tmp/|~/Downloads)|/Downloads/')
LOCAL_MCP = frozenset(('computer-use-linux', 'desk-control', 'firefox-devtools',
                       'plugin_chrome-devtools-mcp_chrome-devtools', 'rea'))
EDITS = frozenset(('Edit', 'Write', 'NotebookEdit', 'MultiEdit'))
LOCAL_ACTIONS = frozenset(('Read', 'Grep', 'Glob', 'TodoWrite', 'Agent', 'Task', 'Workflow', 'Skill',
                           'AskUserQuestion', 'ToolSearch', 'TaskStop', 'Monitor', 'ListAgents',
                           'EnterPlanMode', 'ExitPlanMode', 'ScheduleWakeup'))
READ_VERBS = ('get', 'list', 'search', 'read', 'query', 'take', 'screenshot', 'find', 'inspect', 'describe',
              'doctor', 'status', 'observe', 'focused')


def _command(data: dict) -> str | None:
    inputs = data.get('tool_input')
    command = inputs.get('command') if isinstance(inputs, dict) else None
    return command if isinstance(command, str) else None


def local_source(data: dict) -> bool:
    """A result from their own files, fleet or repos (ssh included). Downloads and /tmp count as outside."""
    inputs = data.get('tool_input') if isinstance(data.get('tool_input'), dict) else {}
    if data.get('tool_name') in EDITS:
        # Edit and Write results echo text the agent wrote, or a file it had to Read (and scan) first.
        return True
    if data.get('tool_name') in LOCAL_TOOLS:
        path = str(inputs.get('file_path') or inputs.get('path') or inputs.get('notebook_path') or '')
        return not OUTSIDE_PATHS.search(path)
    command = _command(data)
    # An unknown (empty) command is not provably local.
    return (data.get('tool_name') == 'Bash' and bool(command and command.strip())
            and not NETWORK.search(command) and not OUTSIDE_PATHS.search(command))


def protected(path: str) -> bool:
    try:
        real = Path(os.path.realpath(Path(path).expanduser()))
    except (OSError, ValueError, RuntimeError):
        return True
    return any(real == root or root in real.parents for root in PROTECTED_PATHS)


def outward(event: dict) -> bool:
    tool = event.get('tool_name')
    if tool == 'Bash':
        command = _command(event) or ''
        return bool(NETWORK.search(command) or SENDING.search(command)
                    or any(part in command for part in PROTECTED))
    if tool in EDITS:
        inputs = event.get('tool_input') if isinstance(event.get('tool_input'), dict) else {}
        paths = inputs.get('file_paths') or [inputs.get('file_path') or inputs.get('notebook_path') or '']
        return not isinstance(paths, list) or any(not isinstance(path, str) or protected(path) for path in paths)
    if tool in LOCAL_ACTIONS:
        return False
    if isinstance(tool, str) and tool.startswith('mcp__') and tool.count('__') == 2:
        _, server, name = tool.split('__')
        # Remote connectors (Gmail, Drive) send their arguments off the machine even when they only read.
        return server not in LOCAL_MCP or not any(name == verb or name.startswith(verb + '_') for verb in READ_VERBS)
    return True
