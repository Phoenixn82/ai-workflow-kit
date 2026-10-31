"""Known-wall memo (spec §3.2): per domain, the lowest ladder rung (1-based) that worked, so a known walled site
starts there instead of re-proving every cheaper rung is blocked. A domain only moves up past rungs that showed block
evidence (the ladder decides that), and an entry decays after TTL so a site that dropped its wall gets re-probed.
Unknown, expired or unreadable entries start at rung 1.

Format: {"<domain>": {"rung": n, "ts": <epoch seconds the entry was written>}}. A bare int (the first format) is
read as that rung and rewritten as {rung, ts=now} on first read, so it decays like any other entry."""
import contextlib
import json
import os
import tempfile
import time
from pathlib import Path

WALLS_PATH = Path.home() / ".local" / "share" / "web" / "walls.json"
TTL = 14 * 86400


def _rung(entry, now: float) -> int:
    if not isinstance(entry, dict):
        return 1
    ts, rung = entry.get("ts"), entry.get("rung")
    if not isinstance(ts, (int, float)) or now - ts > TTL:
        return 1
    return rung if isinstance(rung, int) and rung >= 1 else 1


class Walls:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or WALLS_PATH)

    def _load(self) -> tuple[dict, bool]:
        """The memo, with first-format bare ints upgraded to {rung, ts=now}; True when that changed anything."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}, False
        if not isinstance(data, dict):
            return {}, False
        legacy = [k for k, v in data.items() if isinstance(v, int) and not isinstance(v, bool)]
        for k in legacy:
            data[k] = {"rung": data[k], "ts": int(time.time())}
        return data, bool(legacy)

    def start_rung(self, domain: str) -> int:
        data, upgraded = self._load()
        if upgraded:   # rewrite now so the TTL applies: no entry lives forever
            with contextlib.suppress(OSError):
                self._write(data)
        return _rung(data.get(domain.lower()), time.time())

    def record(self, domain: str, rung: int) -> None:
        """Set the domain's start rung (up or down). Unchanged live entries aren't rewritten, so `ts` keeps the time
        the rung was last proven, not last used; rung 1 forgets the domain."""
        (data, upgraded), domain, now = self._load(), domain.lower(), time.time()
        if _rung(data.get(domain), now) == rung and (rung > 1 or domain not in data):
            if upgraded:
                self._write(data)
            return
        if rung > 1:
            data[domain] = {"rung": rung, "ts": int(now)}
        else:
            data.pop(domain, None)
        self._write(data)

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".walls-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)   # atomic: readers see the old file or the new one, never half of one
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
