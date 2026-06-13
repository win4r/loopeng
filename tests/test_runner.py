"""Runner: stop conditions, verification gate, timeout handling, ledger output."""

import sys

from loopeng.ledger import Ledger
from loopeng.runner import render_template, run_loop
from loopeng.spec import parse_spec

PY = sys.executable or "python3"


def make_spec(verify_cmd, agent_cmd=None, **limits):
    data = {
        "objective": "obj",
        "prompt": "do it: {{feedback}}",
        "agent": {"type": "shell", "command": agent_cmd or [PY, "-c", "pass"]},
        "verify": {"command": verify_cmd},
    }
    if limits:
        data["limits"] = limits
    return parse_spec(data)


def test_render_template_substitutes_known_keys_only():
    assert render_template("a {{x}} b {{missing}}", {"x": "1"}) == "a 1 b {{missing}}"


def test_verification_gate_pass_terminates_immediately(tmp_path):
    spec = make_spec(verify_cmd=[PY, "-c", "import sys; sys.exit(0)"])
    result = run_loop(spec, tmp_path)
    assert result.status == "success"
    assert result.passed is True
    assert result.iterations == 1


def test_verification_gate_fail_runs_to_max_iterations(tmp_path):
    spec = make_spec(
        verify_cmd=[PY, "-c", "import sys; sys.exit(1)"],
        max_iterations=3,
        max_consecutive_failures=99,  # high, so the cap is what stops us
    )
    result = run_loop(spec, tmp_path)
    assert result.status == "exhausted"
    assert result.passed is False
    assert result.iterations == 3


def test_consecutive_failure_circuit_breaker(tmp_path):
    spec = make_spec(
        verify_cmd=[PY, "-c", "import sys; sys.exit(1)"],
        max_iterations=10,
        max_consecutive_failures=2,  # trips before the iteration cap
    )
    result = run_loop(spec, tmp_path)
    assert result.status == "blocked"
    assert result.iterations == 2


def test_per_command_timeout_is_a_failure(tmp_path):
    spec = make_spec(
        verify_cmd=[PY, "-c", "import time; time.sleep(5)"],
        max_iterations=1,
        max_consecutive_failures=1,
        command_timeout=1,
    )
    result = run_loop(spec, tmp_path)
    assert result.status in ("blocked", "exhausted")
    iteration_records = [
        r for r in Ledger(result.ledger_path).records() if r.get("event") == "iteration"
    ]
    assert iteration_records[0]["verify_timed_out"] is True
    assert iteration_records[0]["verify_exit"] == 124


def test_max_iterations_override(tmp_path):
    spec = make_spec(
        verify_cmd=[PY, "-c", "import sys; sys.exit(1)"],
        max_iterations=9,
        max_consecutive_failures=99,
    )
    result = run_loop(spec, tmp_path, max_iterations=2)
    assert result.iterations == 2


def test_ledger_has_run_start_iterations_and_run_end(tmp_path):
    spec = make_spec(verify_cmd=[PY, "-c", "import sys; sys.exit(0)"])
    result = run_loop(spec, tmp_path)
    records = Ledger(result.ledger_path).records()
    events = [r["event"] for r in records]
    assert events[0] == "run_start"
    assert "iteration" in events
    assert events[-1] == "run_end"
    assert records[-1]["status"] == "success"
