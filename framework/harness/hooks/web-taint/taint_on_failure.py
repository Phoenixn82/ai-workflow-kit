"""Independent stdlib emergency writer, permitted by controller Ruling 25."""
import json
import os
from pathlib import Path
import re
import sys
import stat
import tempfile
import time


def identity(raw: str, *, events: tuple = ('PostToolUse', 'PostToolUseFailure'),
             default_event: str | None = 'PostToolUse') -> dict:
    """Recover only decoded top-level metadata before a malformed JSON suffix.

    Walk keys with JSONDecoder, never search result text for a session id.
    A broken value ends recovery. No trusted id means explicit failure.
    """
    decoder = json.JSONDecoder()
    values = {}
    cursor = 0
    raw = raw.lstrip()
    if not raw.startswith('{'):
        raise ValueError('identity')
    cursor = 1
    while True:
        try:
            while cursor < len(raw) and raw[cursor].isspace():
                cursor += 1
            if cursor < len(raw) and raw[cursor] == '}':
                break
            key, cursor = decoder.raw_decode(raw, cursor)
            while cursor < len(raw) and raw[cursor].isspace():
                cursor += 1
            if not isinstance(key, str) or raw[cursor:cursor + 1] != ':':
                break
            cursor += 1
            while cursor < len(raw) and raw[cursor].isspace():
                cursor += 1
            value, cursor = decoder.raw_decode(raw, cursor)
            if key in ('session_id', 'cwd', 'hook_event_name'):
                values[key] = value
            while cursor < len(raw) and raw[cursor].isspace():
                cursor += 1
            if raw[cursor:cursor + 1] != ',':
                break
            cursor += 1
        except (ValueError, RecursionError):
            break
    session = values.get('session_id')
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', session):
        raise ValueError('identity')
    if not isinstance(values.get('cwd', ''), str):
        values['cwd'] = ''
    if values.get('hook_event_name') not in events:
        values['hook_event_name'] = default_event
    return values


def state_dirs() -> tuple[Path, Path]:
    primary = Path(os.environ.get('WEB_TAINT_STATE_DIR', Path.home() / '.local/state/web-taint'))
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid())))
    fallback = Path(os.environ.get('WEB_TAINT_FALLBACK_DIR', runtime / 'web-taint'))
    return primary, fallback


def state_exists(agent: str, session: str) -> bool:
    """No shared imports: a record or an unreadable existing directory taints."""
    if agent not in ('claude', 'codex') or not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', session):
        return True
    for directory in state_dirs():
        try:
            info = directory.stat()
        except FileNotFoundError:
            if directory.is_symlink():
                return True
            continue
        except OSError:
            return True
        if not stat.S_ISDIR(info.st_mode) or not info.st_mode & 0o444 or not info.st_mode & 0o111:
            return True
        try:
            with os.scandir(directory):
                pass
            (directory / (agent + '-' + session + '.json')).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


def atomic_record(directory: Path, record: dict) -> None:
    path = directory / (record['agent'] + '-' + record['session_id'] + '.json')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.' + path.name + '-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(record, file, sort_keys=True)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def write_record(record: dict) -> None:
    if (record['agent'] not in ('claude', 'codex')
            or not re.fullmatch(r'[A-Za-z0-9_-]+', record['session_id'])):
        raise ValueError('identity')
    for directory in state_dirs():
        try:
            atomic_record(directory, record)
            return
        except OSError:
            pass
    raise OSError('taint stores unavailable')


def mark_failure(meta: dict, *, agent: str = 'claude') -> None:
    write_record({'agent': agent, 'session_id': meta['session_id'],
                  'rules': ['SCANNER'], 'source': 'SCANNER',
                  'cwd': meta.get('cwd', ''), 'time': time.time()})


def main() -> int:
    try:
        # The supervisor passes the recovered metadata separately; replayed
        # stdin stays the exact original payload even when it is malformed.
        raw = sys.argv[1] if len(sys.argv) == 2 else sys.stdin.read()
        meta = identity(raw)
        mark_failure(meta)
        print(json.dumps({'hookSpecificOutput': {
            'hookEventName': meta['hook_event_name'], 'additionalContext': 'rules SCANNER'}}))
        return 0
    except Exception:
        print('SCANNER', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
