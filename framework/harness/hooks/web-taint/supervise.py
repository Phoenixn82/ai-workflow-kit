"""Read once, bound scanner runtime, replay the payload to an independent writer."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from taint_on_failure import identity


def main() -> int:
    try:
        raw = sys.stdin.buffer.read()
        meta = identity(raw.decode('utf-8', errors='replace'))
        try:
            result = subprocess.run(['timeout', '-k', '1', '20', 'python3', '-I', '-S', str(ROOT / 'scan.py')],
                                    input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            result = None
        if result is not None and result.returncode == 0:
            sys.stdout.buffer.write(result.stdout)
            return 0
        result = subprocess.run(['python3', '-I', '-S', str(ROOT / 'taint_on_failure.py'), json.dumps(meta)],
                                input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            raise ValueError('fallback')
        sys.stdout.buffer.write(result.stdout)
        return 0
    except Exception:
        print(json.dumps({'continue': False, 'stopReason':
              'Taint protection failed: check disk space and permissions in both taint stores; repair them and run web untaint in your own terminal before restarting.'}))
        return 0


if __name__ == '__main__':
    sys.exit(main())
