"""Collect Codex output signals from source strings before formatting."""
from contextvars import ContextVar
import json

from . import tripwire

rules = ContextVar('web_output_rules', default=None)
MAX_DEPTH = 32
MAX_NODES = 100_000
MAX_CHARS = 8 * 1024 * 1024


def _walk(value, found, *, depth, budget, machine=False) -> None:
    # Machine ingress stays machine ingress at every depth, so serialized
    # scalar strings nested inside it are decoded too.
    budget[0] -= 1
    if depth > MAX_DEPTH or budget[0] < 0:
        raise ValueError('result limits')
    if isinstance(value, str):
        budget[1] -= len(value)
        if budget[1] < 0:
            raise ValueError('result limits')
        found.update(hit.rule for hit in tripwire.scan(value, profile='session'))
        stripped = value.strip()
        if not stripped:
            return
        try:
            decoded = json.loads(stripped) if machine or stripped.startswith(('{', '[')) else None
        except ValueError:
            decoded = None
        if decoded is None:
            lines = [line for line in value.splitlines() if line.strip()]
            if len(lines) < 2:
                return
            try:
                decoded = [json.loads(line) for line in lines]
            except ValueError:
                return  # Plain or incomplete output still receives the raw scan.
        _walk(decoded, found, depth=depth + 1, budget=budget, machine=machine)
    elif isinstance(value, dict):
        for key, item in value.items():
            _walk(key, found, depth=depth + 1, budget=budget, machine=machine)
            _walk(item, found, depth=depth + 1, budget=budget, machine=machine)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item, found, depth=depth + 1, budget=budget, machine=machine)


def scan(value, *, machine=False) -> None:
    """Scan raw strings and recursively decoded JSON within shared walk limits."""
    found = rules.get()
    if found is None:
        return
    try:
        _walk(value, found, depth=0, budget=[MAX_NODES, MAX_CHARS], machine=machine)
    except Exception:
        found.add('SCANNER')


def serialized(text: str) -> None:
    """Decode machine output supplied by a browser or remote CLI before emission."""
    scan(text, machine=True)
