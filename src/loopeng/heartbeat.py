"""Live run state: a single ``.loopeng/heartbeat.json`` rewritten each phase.

Unlike the append-only ledger, the heartbeat is one small JSON object describing
where a run currently is, written atomically (temp file + rename) so a concurrent
``loopeng status`` reader never sees a torn file. Staleness is judged by whether
the recorded pid is still alive and how long ago it was updated.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .events import utcnow_iso

HEARTBEAT_SCHEMA_VERSION = 1
HEARTBEAT_FILENAME = "heartbeat.json"
DEFAULT_STALE_SECONDS = 30

# Phases written to the heartbeat over a run's life.
PHASE_STARTING = "starting"
PHASE_GATHERING_CONTEXT = "gathering_context"
PHASE_RUNNING_AGENT = "running_agent"
PHASE_CHECKING_BLAST_RADIUS = "checking_blast_radius"
PHASE_VERIFYING = "verifying"
PHASE_WRITING_LEDGER = "writing_ledger"
PHASE_COMPLETED = "completed"
PHASE_BLOCKED = "blocked"
PHASE_FAILED = "failed"


class HeartbeatWriter:
    def __init__(
        self,
        path,
        *,
        run_id,
        pid,
        cwd,
        spec_path,
        spec_fingerprint,
        max_iterations,
        started_at,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._base = {
            "heartbeat_schema_version": HEARTBEAT_SCHEMA_VERSION,
            "run_id": run_id,
            "pid": pid,
            "cwd": cwd,
            "spec_path": spec_path,
            "spec_fingerprint": spec_fingerprint,
            "max_iterations": max_iterations,
            "started_at": started_at,
        }

    def update(self, *, phase, iteration, consecutive_failures, last_event) -> dict:
        data = dict(self._base)
        data.update(
            phase=phase,
            iteration=iteration,
            consecutive_failures=consecutive_failures,
            last_event=last_event,
            updated_at=utcnow_iso(),
        )
        tmp = self.path.parent / (self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)  # atomic on POSIX
        return data


def read_heartbeat(path) -> Optional[dict]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def pid_alive(pid) -> bool:
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except (OSError, ValueError, TypeError):
        return False
    return True


def _age_seconds(updated_at) -> Optional[float]:
    if not updated_at:
        return None
    try:
        when = datetime.fromisoformat(updated_at)
    except (ValueError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def is_stale(heartbeat, *, stale_seconds: float = DEFAULT_STALE_SECONDS) -> bool:
    """Whether a run looks dead.

    A live pid is authoritative: the heartbeat only refreshes between phases, and
    a single phase (running_agent / verifying) can legitimately take up to the
    spec's command_timeout, so an age threshold alone would wrongly flag a slow
    but live run. We therefore treat a live pid as not-stale and fall back to the
    age threshold only when there is no pid to check.

    Caveat: pid reuse can rarely make a crashed run's recycled pid read as live.
    """
    if not heartbeat:
        return True
    pid = heartbeat.get("pid")
    if pid is not None:
        return not pid_alive(pid)
    age = _age_seconds(heartbeat.get("updated_at"))
    return age is None or age > stale_seconds
