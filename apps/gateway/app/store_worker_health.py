"""Local scheduler heartbeat. Probes perform no network or database requests."""
import json
import os
from pathlib import Path
import sys
import time

class WorkerHealth:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.queue_ok = False
        self.queue_checked = 0.0

    @classmethod
    def from_environment(cls):
        return cls(os.getenv('STORE_WORKER_HEALTH_PATH'))

    def clear(self):
        if self.path:
            self.path.unlink(missing_ok=True)

    def beat(self, queue_ok=None):
        if not self.path:
            return
        now = time.monotonic()
        if queue_ok is not None:
            self.queue_ok, self.queue_checked = queue_ok, now
        temporary = self.path.with_suffix('.new')
        try:
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump({'beat': now, 'queue_ok': self.queue_ok,
                           'queue_checked': self.queue_checked}, stream, allow_nan=False)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

def healthy(path, ready=False):
    try:
        with Path(path).open(encoding='utf-8') as stream:
            payload = stream.read(1025)
        if len(payload) > 1024:
            return False
        record, now = json.loads(payload), time.monotonic()
        beat = record['beat']
        if type(beat) not in (int, float) or not 0 <= now - beat <= 90:
            return False
        if ready:
            checked = record['queue_checked']
            return record['queue_ok'] is True and type(checked) in (int, float) and 0 <= now - checked <= 90
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False

if __name__ == '__main__':
    mode = sys.argv[1:]
    path = os.getenv('STORE_WORKER_HEALTH_PATH', '')
    raise SystemExit(0 if mode in (['live'], ['ready']) and path and healthy(path, mode == ['ready']) else 1)
