"""Codex 0.162.1 mapping; contracts and limits: fixtures/codex-source-notes.md."""
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Notes item 4: exact local/virtual read tools only. Shell and all MCP stay out.
# There is no core filesystem Read/Grep/Glob. Virtual tools retain their scope.
# Namespaces flatten by concatenation, not a dot: core/src/tools/mod.rs:47.
# Namespace/name literals: ext/history-notes/src/tools.rs:25,71 and
# ext/memories/src/lib.rs:18. See README.md for this source-note clarification.
TOOLS = {
    'view_image': 'Read', 'update_plan': 'TodoWrite',
    'request_user_input': 'AskUserQuestion', 'request_user_input_async': 'AskUserQuestion',
    'tool_search': 'ToolSearch', 'list_available_plugins_to_install': 'ToolSearch',
    'noteslist_files_by_prefix': 'Glob', 'notesread_file': 'Read',
    'notessearch_contents': 'Grep', 'historylist_windows': 'Glob',
    'historylist_items': 'Glob', 'historyread_item': 'Read',
    'historysearch_contents': 'Grep', 'memorieslist': 'Glob',
    'memoriesread': 'Read', 'memoriessearch': 'Grep',
}


PATCH_PATH = re.compile(r'^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$', re.MULTILINE)


def patch_paths(patch, cwd: str) -> list[str] | None:
    """Every source and destination path in an apply_patch body, resolved against cwd; None if none parse."""
    paths = PATCH_PATH.findall(patch) if isinstance(patch, str) else []
    return [str(Path(cwd or '/') / Path(path).expanduser()) for path in paths] or None


def adapt(event: dict) -> dict:
    """Keep hook session_id (CODEX_SESSION_ID), never trust payload agent labels."""
    if not isinstance(event, dict):
        raise ValueError('payload')
    kind = event.get('hook_event_name')
    if kind not in ('PreToolUse', 'PostToolUse'):
        raise ValueError('event')
    name = event.get('tool_name')
    if not isinstance(name, str):
        raise ValueError('tool')
    inputs = event.get('tool_input', {})
    if not isinstance(inputs, dict):
        raise ValueError('input')
    tool = TOOLS.get(name, 'OTHER')
    if name in ('Bash', 'exec_command'):
        tool = 'Bash'
        inputs = {'command': inputs.get('command', inputs.get('cmd', ''))}
    elif name == 'view_image':
        inputs = {'file_path': inputs.get('path')}
    elif name == 'apply_patch' and kind == 'PreToolUse':
        # A patch is a local edit; Edit's protected-path check covers every path it touches.
        tool, inputs = 'Edit', {'file_paths': patch_paths(inputs.get('command'), event.get('cwd', ''))}
    result = {'agent': 'codex', 'session_id': event.get('session_id'),
              'cwd': event.get('cwd', ''), 'hook_event_name': kind,
              'tool_name': tool, 'tool_input': inputs}
    if kind == 'PostToolUse':
        if 'tool_response' not in event:
            raise ValueError('response')
        result['tool_response'] = event['tool_response']
    return result


def main() -> int:
    try:
        raw = json.load(sys.stdin)
        event = adapt(raw)
        if event['hook_event_name'] == 'PostToolUse':
            from scan import main as scan_main
            return scan_main(agent='codex', data=event)
        from guard import _decide
        reason, updated = _decide(event, agent='codex')
        if reason:
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                  'permissionDecision': 'deny', 'permissionDecisionReason': reason}}))
        elif updated:
            original = raw['tool_input']
            key = 'cmd' if 'cmd' in original and 'command' not in original else 'command'
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                  'updatedInput': {**original, key: updated['command']}}}))
        return 0
    except Exception:
        print('CODEX_ADAPTER', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
