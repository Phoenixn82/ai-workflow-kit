"""Detached judge for one flag (stdin JSON from scan.py). A false alarm removes only its own flag; the
record goes away when no flag is pending and none was an attack. An attack is sticky for the session and
sends one silent Telegram FYI. A judge failure leaves the flag pending (outward actions stay reviewed)."""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agents
import common

ATTEMPTS, RETRY_SECONDS = 3, float(os.environ.get('WEB_TAINT_JUDGE_RETRY', '5'))


def log(session: str, verdict: str | None) -> None:
    try:
        path = common.state_path('claude', session).parent / 'guard.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(json.dumps({'ts': time.time(), 'session': session, 'event': 'judge',
                                   'verdict': verdict or 'none'}) + '\n')
    except OSError:
        pass


def main() -> int:
    card = json.loads(sys.stdin.read())
    agent, session, flag_id = card['agent'], card['session'], card['flag_id']
    for attempt in range(ATTEMPTS):
        verdict = agents.judge(card['snippets'], card['rules'], card['source'], card['detail'])
        if verdict:
            break
        time.sleep(RETRY_SECONDS if attempt < ATTEMPTS - 1 else 0)
    if verdict == 'false_alarm' and not card.get('complete'):
        verdict = 'partial'  # the judge did not see every hit, so it cannot clear the flag
    log(session, verdict)
    if verdict in (None, 'partial'):
        return 1
    became_attack = []

    def change(record):
        if not record or flag_id not in record.get('pending', []):
            return record  # cleared meanwhile, or not this flag's record
        record = {**record, 'pending': [item for item in record['pending'] if item != flag_id]}
        if verdict == 'attack':
            became_attack.append(record.get('verdict') != 'attack')
            record['verdict'] = 'attack'
        return record if record['pending'] or record['verdict'] == 'attack' else None
    common.update(agent, session, change)
    if any(became_attack) and 'WEB_TAINT_STATE_DIR' not in os.environ:
        from web import notify
        notify.ping(f"🛡️ {'Codex' if agent == 'codex' else 'Claude'} session {session[:8]} read outside text that "
                    f"tried to instruct agents ({', '.join(card['rules'])}, from {card['source']} {card['detail']}). "
                    'It keeps working; sending, pushing and deploys now go through a reviewer agent. Nothing to do.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
