"""Stall detection: no_output_timeout (silent hang) and no_progress_limit (no new evidence)."""

import sys
import threading
import time

from loopeng.ledger import Ledger
from loopeng.proc import EXIT_TIMEOUT, run_proc
from loopeng.resume import resolve_resume
from loopeng.runner import run_loop
from loopeng.spec import fingerprint, parse_spec

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


def test_overall_timeout_fires_before_no_output_timeout(tmp_path):
    # timeout(1) < no_output_timeout(5): the overall timeout wins -> timed_out, not stalled.
    result = run_proc([PY, "-c", "import time; time.sleep(5)"], cwd=tmp_path, timeout=1, no_output_timeout=5)
    assert result.timed_out is True
    assert result.stalled is False
    assert "TIMEOUT" in result.stderr and "STALLED" not in result.stderr


def test_no_output_timeout_does_not_deadlock_on_large_stdin(tmp_path):
    # Regression for the review's CRITICAL: child floods stderr (> pipe buffer) then sleeps
    # without reading a large stdin. The read loop must drain output + the stall deadline fire,
    # rather than the up-front stdin write deadlocking. Guarded so a regression fails fast.
    code = "import sys, time; sys.stderr.write('x' * 200000); sys.stderr.flush(); time.sleep(10)"
    box = {}

    def go():
        box["r"] = run_proc(
            [PY, "-c", code], cwd=tmp_path, timeout=30, no_output_timeout=2, stdin_text="P" * 200000
        )

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(15)
    assert not thread.is_alive(), "run_proc deadlocked on large stdin + output flood"
    assert box["r"].stalled is True  # killed on inactivity after the flood, not hung


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


def test_no_progress_streak_resets_on_changed_feedback(tmp_path):
    # Distinct feedback on iter 1, then identical from iter 2 on: must NOT stop at the change
    # boundary; stop only after `limit` consecutive identical-feedback failures.
    verify = (
        "import pathlib, sys\n"
        "c = pathlib.Path('count.txt'); n = int(c.read_text()) if c.exists() else 0\n"
        "c.write_text(str(n + 1))\n"
        "sys.stderr.write('first' if n == 0 else 'stuck')\n"
        "sys.exit(1)\n"
    )
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "{{feedback}}",
            "agent": {"type": "shell", "command": [PY, "-c", "pass"]},
            "verify": {"command": [PY, "-c", verify]},
            "limits": {"max_iterations": 10, "max_consecutive_failures": 9, "no_progress_limit": 2},
        }
    )
    result = run_loop(spec, tmp_path)
    assert result.status == "no_progress"
    assert result.iterations == 3  # 'first', then 'stuck','stuck' -> stop at the 2nd identical


def test_no_progress_on_blast_radius_violations(git_repo):
    # Agent writes the SAME forbidden file each iteration -> identical violation feedback.
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "{{feedback}}",
            "agent": {"type": "shell", "command": ["sh", "-lc", "echo x > .env"]},
            "verify": "true",
            "limits": {"forbidden_paths": [".env"], "max_iterations": 10,
                       "max_consecutive_failures": 9, "no_progress_limit": 2},
        }
    )
    result = run_loop(spec, git_repo)
    assert result.status == "no_progress"
    assert result.iterations == 2


def test_stalled_agent_is_non_fatal_when_verify_passes(tmp_path):
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "{{feedback}}",
            "agent": {"type": "shell", "command": [PY, "-c", "import time; time.sleep(5)"]},
            "verify": {"command": [PY, "-c", "import sys; sys.exit(0)"]},
            "limits": {"max_iterations": 1, "command_timeout": 30, "no_output_timeout": 1},
        }
    )
    result = run_loop(spec, tmp_path)
    assert result.status == "success"  # the agent stalled, but verify still ran and passed
    iteration = [r for r in Ledger(result.ledger_path).records() if r["event"] == "iteration"][0]
    assert iteration["agent_stalled"] is True
    assert iteration["verify_exit"] == 0


def test_resume_refuses_no_progress_without_force(tmp_path):
    spec = _identical_failure_spec(2)
    result = run_loop(spec, tmp_path)
    assert result.status == "no_progress"
    ledger = tmp_path / ".loopeng" / "ledger.jsonl"
    refused = resolve_resume(ledger, fingerprint(spec))
    assert not refused.resumable
    assert refused.reason == "no_progress_not_resumable"
    assert resolve_resume(ledger, fingerprint(spec), force=True).resumable
