"""`loopeng status --json` output + staleness reporting."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from loopeng.cli import main
from loopeng.events import utcnow_iso
from loopeng.heartbeat import HEARTBEAT_FILENAME
from loopeng.runner import run_loop
from loopeng.spec import parse_spec

PY = sys.executable or "python3"
DEAD_PID = 2**31 - 1


def _run(tmp_path):
    data = {
        "objective": "o",
        "prompt": "{{feedback}}",
        "agent": {"type": "shell", "command": [PY, "-c", "pass"]},
        "verify": {"command": [PY, "-c", "import sys; sys.exit(0)"]},
    }
    run_loop(parse_spec(data), tmp_path)


def _write_heartbeat(tmp_path, **overrides):
    state = tmp_path / ".loopeng"
    state.mkdir(parents=True, exist_ok=True)
    data = {
        "heartbeat_schema_version": 1,
        "run_id": "r1",
        "pid": os.getpid(),
        "cwd": ".",
        "spec_path": "loop.yaml",
        "spec_fingerprint": "x",
        "phase": "running_agent",
        "iteration": 1,
        "max_iterations": 5,
        "consecutive_failures": 0,
        "started_at": utcnow_iso(),
        "updated_at": utcnow_iso(),
        "last_event": "agent_started",
    }
    data.update(overrides)
    (state / HEARTBEAT_FILENAME).write_text(json.dumps(data), encoding="utf-8")


def test_status_json_is_valid_after_run(tmp_path, capsys, monkeypatch):
    _run(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out.strip())  # must be valid JSON
    assert report["heartbeat_present"] is True
    assert report["run_id"]
    assert report["phase"] == "completed"
    assert report["last_event"]["event"] == "run_end"


def test_status_json_when_no_state(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out.strip())
    assert report["heartbeat_present"] is False
    assert report["stale"] is True


def test_status_live_pid_not_stale_even_when_old(tmp_path, capsys, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    _write_heartbeat(tmp_path, pid=os.getpid(), phase="running_agent", updated_at=old)
    monkeypatch.chdir(tmp_path)
    main(["status", "--json"])
    report = json.loads(capsys.readouterr().out.strip())
    assert report["stale"] is False  # a live pid means a live (possibly slow) run
    assert report["pid_alive"] is True
    assert report["phase"] == "running_agent"


def test_status_stale_when_no_pid_and_old(tmp_path, capsys, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    _write_heartbeat(tmp_path, pid=None, updated_at=old)
    monkeypatch.chdir(tmp_path)
    main(["status", "--json"])
    report = json.loads(capsys.readouterr().out.strip())
    assert report["stale"] is True


def test_status_dir_reads_target_not_cwd(tmp_path, capsys):
    # process cwd is NOT tmp_path; --dir must be honored.
    _write_heartbeat(tmp_path, run_id="rX", pid=os.getpid())
    assert main(["status", "--dir", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out.strip())
    assert report["heartbeat_present"] is True
    assert report["run_id"] == "rX"


def test_status_marks_stale_on_dead_pid(tmp_path, capsys, monkeypatch):
    _write_heartbeat(tmp_path, pid=DEAD_PID)  # fresh timestamp, dead pid
    monkeypatch.chdir(tmp_path)
    main(["status", "--json"])
    report = json.loads(capsys.readouterr().out.strip())
    assert report["stale"] is True
    assert report["pid_alive"] is False
