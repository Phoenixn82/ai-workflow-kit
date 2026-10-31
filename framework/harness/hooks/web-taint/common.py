"""Hook adapter for the shared stdlib scanner and state helpers."""
import os
from pathlib import Path
import sys

sys.path.insert(0, os.environ.get('WEB_PKG_DIR') or str(Path.home() / 'AI_Projects/_tools/web'))
from web import taint
from web.fence import unfence
from web.tripwire import RULES, scan

# Task 5 exposes patchable constants, not env overrides. Apply the hook's test
# overrides to that shared module, so all imported helpers use the same stores.
if 'WEB_TAINT_STATE_DIR' in os.environ:
    taint.STATE_DIR = Path(os.environ['WEB_TAINT_STATE_DIR'])
if 'WEB_TAINT_EXEMPT_FILE' in os.environ:
    taint.EXEMPT_FILE = Path(os.environ['WEB_TAINT_EXEMPT_FILE'])

from web.taint import (state_path, is_tainted, mark, clear, untaint,
                       exempt_add, exempt_list, exempt_rm, exempt_match, update)

exempt = exempt_match


MAX_DEPTH = 32
MAX_NODES = 100_000
MAX_CHARS = 8 * 1024 * 1024


def texts(obj: object, *, depth: int = 0, budget: list[int] | None = None):
    """Bounded walk of all strings, including keys and nested result channels."""
    if budget is None:
        budget = [MAX_NODES, MAX_CHARS]
    budget[0] -= 1
    if depth > MAX_DEPTH or budget[0] < 0:
        raise ValueError('result limits')
    if isinstance(obj, str):
        budget[1] -= len(obj)
        if budget[1] < 0:
            raise ValueError('result limits')
        yield obj
    elif isinstance(obj, dict):
        for pair in obj.items():
            for value in pair:
                yield from texts(value, depth=depth + 1, budget=budget)
    elif isinstance(obj, list):
        for value in obj:
            yield from texts(value, depth=depth + 1, budget=budget)


def touched_files(data: dict) -> list[str]:
    """Only built-in file tools with a known, complete file set can skip scans."""
    inputs, response = data.get('tool_input'), data.get('tool_response')
    if not isinstance(inputs, dict) or not isinstance(response, dict):
        return []
    if data.get('hook_event_name') != 'PostToolUse' or data.get('error'):
        return []
    if data.get('tool_name') == 'Read':
        path = inputs.get('file_path')
        file = response.get('file')
        if response.get('type') != 'text' or not isinstance(file, dict):
            return []
        if file.get('filePath', path) != path:
            return []
        paths = [path]
    elif data.get('tool_name') == 'Grep':
        paths = response.get('filenames')
        if not isinstance(paths, list) or response.get('numFiles') != len(paths):
            return []
    else:
        return []
    if not paths or not all(isinstance(path, str) and Path(path).is_absolute() for path in paths):
        return []
    return paths
