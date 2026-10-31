"""Bound Codex adapter runtime and replace failures with safe hook output.

Pre errors must emit deny JSON, exit 0, when state exists or is unreadable.
Post errors use Task 6's independent emergency writer, now keyed to codex.
Source contracts: fixtures/codex-source-notes.md, denial and trust sections.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def refusal() -> bytes:
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny', 'permissionDecisionReason': 'SCANNER'}}).encode() + b'\n'


def state_exists(session: str) -> bool:
    """Independent of web/adapter imports; all present/unreadable paths deny."""
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', session):
        return True
    from taint_on_failure import state_exists as stored
    return stored('codex', session)


RULE_IDS = r'(?:T[0-9]+|SCANNER)(?:,(?:T[0-9]+|SCANNER))*'


def _context_pattern() -> str:
    from scan import FLAGGED, WARNING, WITHHELD
    return (f'rules {RULE_IDS}|flagged {RULE_IDS}{re.escape(FLAGGED)}|warning {RULE_IDS}{re.escape(WARNING)}'
            f'|{re.escape(WITHHELD)}')


CONTEXT = _context_pattern()


def valid_output(output: bytes, kind: str) -> bool:
    if not output.strip():
        return True
    try:
        value = json.loads(output)
        h = value['hookSpecificOutput']
        if set(value) != {'hookSpecificOutput'} or not isinstance(h, dict) or h.get('hookEventName') != kind:
            return False
        if kind == 'PreToolUse' and h.get('permissionDecision') == 'allow':
            from sandbox import PREFIX
            updated = h.get('updatedInput')
            return (set(h) == {'hookEventName', 'permissionDecision', 'updatedInput'} and isinstance(updated, dict)
                    and any(isinstance(updated.get(key), str) and updated[key].startswith(PREFIX)
                            for key in ('command', 'cmd')))
        if kind == 'PreToolUse':
            return (set(h) == {'hookEventName', 'permissionDecision', 'permissionDecisionReason'}
                    and h['permissionDecision'] == 'deny'
                    and isinstance(h['permissionDecisionReason'], str) and bool(h['permissionDecisionReason']))
        # Scanner emits fixed rule ids only. Never relay arbitrary child output.
        return (set(h) == {'hookEventName', 'additionalContext'}
                and isinstance(h['additionalContext'], str)
                and re.fullmatch(CONTEXT, h['additionalContext']) is not None)
    except (ValueError, UnicodeError, TypeError, KeyError, RecursionError):
        return False


def supervise(raw: bytes, adapter: Path = ROOT / 'codex_adapter.py', *, timeout: float = 45) -> bytes:
    try:
        event = json.loads(raw)
        kind, session = event['hook_event_name'], event['session_id']
        if kind not in ('PreToolUse', 'PostToolUse') or not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', session):
            raise ValueError('metadata')
    except (ValueError, UnicodeError, TypeError, KeyError, RecursionError):
        # Partial post payloads still mark the top-level identity if recoverable.
        try:
            from taint_on_failure import identity
            meta = identity(raw.decode('utf-8', errors='replace'),
                            events=('PreToolUse', 'PostToolUse'), default_event=None)
        except Exception:
            return refusal()
        if meta.get('hook_event_name') == 'PostToolUse':
            # Let a failed emergency write reach main's post block JSON.
            # A pre-only hookSpecificOutput would be ignored by the post hook.
            return post_failure(meta)
        return refusal()
    try:
        result = subprocess.run(['python3', '-I', '-S', str(adapter)], input=raw,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        if result.returncode == 0 and valid_output(result.stdout, kind):
            return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    if kind == 'PreToolUse':
        return refusal() if state_exists(session) else b''
    return post_failure({'session_id': session, 'cwd': event.get('cwd', ''), 'hook_event_name': kind})


def post_failure(meta: dict) -> bytes:
    from taint_on_failure import mark_failure
    mark_failure(meta, agent='codex')
    return b'{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"rules SCANNER"}}\n'


def main() -> int:
    try:
        sys.stdout.buffer.write(supervise(sys.stdin.buffer.read()))
    except Exception:
        # Codex errors allow. Last-resort pre denial and post result rejection
        # use source-supported shapes without exceptions or tool text.
        print(json.dumps({'decision': 'block', 'reason': 'SCANNER: check disk space and both taint stores; repair and run web untaint in your own terminal before restarting.'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
