"""Typed events are emitted and ledger-compatible (JSON-serializable dicts)."""

import json
import sys

from loopeng.events import EVENT_TYPES, make_event
from loopeng.runner import run_loop
from loopeng.spec import parse_spec

PY = sys.executable or "python3"


def test_make_event_shape_and_serializable():
    event = make_event("run_started", "rid-123", objective="x")
    assert event["type"] == "run_started"
    assert event["run_id"] == "rid-123"
    assert "ts" in event
    assert event["objective"] == "x"
    json.dumps(event)  # serializable


def test_runner_emits_known_ledger_compatible_events(tmp_path):
    collected = []
    data = {
        "objective": "o",
        "prompt": "{{feedback}}",
        "agent": {"type": "shell", "command": [PY, "-c", "pass"]},
        "verify": {"command": [PY, "-c", "import sys; sys.exit(0)"]},
    }
    run_loop(parse_spec(data), tmp_path, on_event=collected.append)

    assert collected
    for event in collected:
        assert isinstance(event, dict)
        assert "type" in event and "run_id" in event and "ts" in event
        json.dumps(event)  # ledger-compatible

    types = {e["type"] for e in collected}
    for expected in (
        "run_started", "iteration_started", "agent_started", "agent_completed",
        "verify_started", "verify_passed", "run_completed", "heartbeat_written",
    ):
        assert expected in types, f"missing emitted event: {expected}"
    # every emitted type is from the known vocabulary (plus the skip notice)
    assert types <= (EVENT_TYPES | {"blast_radius_skipped"})


def test_failed_iteration_emits_verify_failed_and_iteration_failed(tmp_path):
    collected = []
    data = {
        "objective": "o",
        "prompt": "{{feedback}}",
        "agent": {"type": "shell", "command": [PY, "-c", "pass"]},
        "verify": {"command": [PY, "-c", "import sys; sys.exit(1)"]},
        "limits": {"max_iterations": 1, "max_consecutive_failures": 1},
    }
    run_loop(parse_spec(data), tmp_path, on_event=collected.append)
    types = [e["type"] for e in collected]
    assert "verify_failed" in types
    assert "iteration_failed" in types
    assert "run_blocked" in types
