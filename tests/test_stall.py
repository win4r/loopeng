"""Stall detection: no_output_timeout (silent hang) and no_progress_limit (no new evidence)."""

import sys
import time

from loopeng.ledger import Ledger
from loopeng.proc import EXIT_TIMEOUT, run_proc
from loopeng.runner import run_loop
from loopeng.spec import parse_spec

PY = sys.executable or "python3"


# --- no_output_timeout (proc level) ---

def test_no_output_timeout_kills_silent_process(tmp_path):
    result = run_proc(
        [PY, "-c", "import time; time.sleep(5)"],  # sleeps, no output
        cwd=tmp_path,
        timeout=30,
        no_output_timeout=1,
    )
    assert result.stalled is True
    assert result.timed_out is True
    assert result.exit_code == EXIT_TIMEOUT
    assert "STALLED" in result.stderr


def test_no_output_timeout_not_triggered_when_output_flows(tmp_path):
    code = "import time\nfor i in range(5):\n    print(i, flush=True)\n    time.sleep(0.2)\n"
    result = run_proc([PY, "-c", code], cwd=tmp_path, timeout=30, no_output_timeout=2)
    assert result.stalled is False
    assert result.exit_code == 0
    assert "0" in result.stdout and "4" in result.stdout


def test_agent_no_output_timeout_is_fast_and_recorded(tmp_path):
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "{{feedback}}",
            "agent": {"type": "shell", "command": [PY, "-c", "import time; time.sleep(5)"]},
            "verify": {"command": [PY, "-c", "import sys; sys.exit(0)"]},
            "limits": {"max_iterations": 1, "max_consecutive_failures": 1,
                       "command_timeout": 30, "no_output_timeout": 1},
        }
    )
    started = time.monotonic()
    result = run_loop(spec, tmp_path)
    elapsed = time.monotonic() - started
    assert elapsed < 10  # killed at ~1s (no_output_timeout), not 30s (command_timeout)
    iteration = [r for r in Ledger(result.ledger_path).records() if r["event"] == "iteration"][0]
    assert iteration["agent_stalled"] is True
    assert iteration["agent_timed_out"] is True


# --- no_progress_limit (no new evidence) ---

def _identical_failure_spec(no_progress_limit):
    return parse_spec(
        {
            "objective": "o",
            "prompt": "{{feedback}}",
            "agent": {"type": "shell", "command": [PY, "-c", "pass"]},
            "verify": {"command": [PY, "-c", "import sys; sys.stderr.write('same error'); sys.exit(1)"]},
            "limits": {"max_iterations": 10, "max_consecutive_failures": 9,
                       "no_progress_limit": no_progress_limit},
        }
    )


def test_no_progress_stops_on_identical_failures(tmp_path):
    result = run_loop(_identical_failure_spec(2), tmp_path)
    assert result.status == "no_progress"
    assert result.iterations == 2  # two identical-feedback failures -> stop (before the breaker at 9)
    records = Ledger(result.ledger_path).records()
    assert records[-1]["event"] == "run_end"
    assert records[-1]["status"] == "no_progress"


def test_no_progress_emits_event(tmp_path):
    events = []
    run_loop(_identical_failure_spec(2), tmp_path, on_event=events.append)
    detected = [e for e in events if e["type"] == "no_progress_detected"]
    assert detected and detected[0]["streak"] == 2


def test_no_progress_disabled_by_default(tmp_path):
    # Same identical failures, but no no_progress_limit -> runs to the breaker, not no_progress.
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "{{feedback}}",
            "agent": {"type": "shell", "command": [PY, "-c", "pass"]},
            "verify": {"command": [PY, "-c", "import sys; sys.exit(1)"]},
            "limits": {"max_iterations": 10, "max_consecutive_failures": 3},
        }
    )
    result = run_loop(spec, tmp_path)
    assert result.status == "blocked"  # not "no_progress"
