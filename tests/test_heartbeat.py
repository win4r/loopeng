"""Heartbeat writing + staleness detection."""

import os
import sys
from datetime import datetime, timedelta, timezone

from loopeng.events import utcnow_iso
from loopeng.heartbeat import HEARTBEAT_FILENAME, is_stale, pid_alive, read_heartbeat
from loopeng.runner import run_loop
from loopeng.spec import parse_spec

PY = sys.executable or "python3"
DEAD_PID = 2**31 - 1  # implausibly high -> no such process


def _run(tmp_path):
    data = {
        "objective": "o",
        "prompt": "{{feedback}}",
        "agent": {"type": "shell", "command": [PY, "-c", "pass"]},
        "verify": {"command": [PY, "-c", "import sys; sys.exit(0)"]},
    }
    return run_loop(parse_spec(data), tmp_path)


def test_heartbeat_written_with_expected_fields(tmp_path):
    _run(tmp_path)
    hb = read_heartbeat(tmp_path / ".loopeng" / HEARTBEAT_FILENAME)
    assert hb is not None
    for field in (
        "run_id", "pid", "cwd", "spec_path", "spec_fingerprint", "phase",
        "iteration", "max_iterations", "consecutive_failures", "updated_at",
        "started_at", "last_event", "heartbeat_schema_version",
    ):
        assert field in hb, f"missing heartbeat field: {field}"
    assert hb["pid"] == os.getpid()
    assert hb["phase"] == "completed"  # terminal phase after a successful run


def test_pid_alive():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(None) is False
    assert pid_alive(DEAD_PID) is False
    assert pid_alive("not-an-int") is False


def test_is_stale_when_no_heartbeat():
    assert is_stale(None) is True


def test_is_fresh_when_recent_and_alive():
    hb = {"pid": os.getpid(), "updated_at": utcnow_iso()}
    assert is_stale(hb, stale_seconds=30) is False


def test_live_pid_is_authoritative_even_when_old():
    # A live pid means a live run: a slow phase can exceed the age threshold,
    # so age must NOT override a live pid.
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    hb = {"pid": os.getpid(), "updated_at": old}
    assert is_stale(hb, stale_seconds=30) is False


def test_is_stale_when_pid_gone():
    hb = {"pid": DEAD_PID, "updated_at": utcnow_iso()}  # fresh time, dead pid
    assert is_stale(hb) is True


def test_age_fallback_when_no_pid():
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    assert is_stale({"updated_at": old}, stale_seconds=30) is True
    assert is_stale({"updated_at": utcnow_iso()}, stale_seconds=30) is False


def test_read_heartbeat_returns_none_on_corrupt(tmp_path):
    path = tmp_path / ".loopeng" / HEARTBEAT_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert read_heartbeat(path) is None
    assert is_stale(read_heartbeat(path)) is True


def test_pid_alive_rejects_nonpositive_pids():
    """A corrupted heartbeat with pid 0/-1 must read as dead, not target a process group."""
    from loopeng.heartbeat import pid_alive

    assert pid_alive(0) is False
    assert pid_alive(-1) is False
    assert pid_alive(None) is False
    assert pid_alive("notanint") is False
