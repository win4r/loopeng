"""Metric/baseline verification gate: evaluation, spec parsing, and run-loop integration."""

import sys

import pytest

from loopeng.baseline import BaselineSpec, evaluate_baseline
from loopeng.errors import SpecError
from loopeng.ledger import Ledger
from loopeng.runner import run_loop
from loopeng.spec import parse_spec

PY = sys.executable or "python3"


# --- evaluate_baseline (pure) ---

def _b(direction, value, regex=r"score=([\d.]+)"):
    return BaselineSpec(regex=regex, direction=direction, value=value, name="score")


def test_baseline_directions():
    assert evaluate_baseline(_b("greater", 0.8), "score=0.9")[0] is True
    assert evaluate_baseline(_b("greater", 0.8), "score=0.7")[0] is False
    assert evaluate_baseline(_b("greater_equal", 0.8), "score=0.8")[0] is True
    assert evaluate_baseline(_b("less", 5), "score=3")[0] is True
    assert evaluate_baseline(_b("less_equal", 5), "score=6")[0] is False
    assert evaluate_baseline(_b("equal", 1), "score=1")[0] is True


def test_baseline_metric_not_found():
    ok, actual, reason = evaluate_baseline(_b("greater", 0.5), "no metric here")
    assert ok is False and actual is None and "not found" in reason


def test_baseline_non_numeric():
    ok, actual, reason = evaluate_baseline(_b("greater", 0.5, regex=r"score=(\w+)"), "score=high")
    assert ok is False and "not numeric" in reason


def test_baseline_full_match_when_no_capture_group():
    ok, actual, _ = evaluate_baseline(BaselineSpec(regex=r"[\d.]+", direction="greater", value=10), "n 42 ok")
    assert ok is True and actual == 42.0


# --- spec parsing ---

def _spec_with_baseline(baseline):
    return {
        "objective": "o",
        "prompt": "{{feedback}}",
        "agent": {"type": "shell", "command": ["true"]},
        "verify": {"command": ["true"], "baseline": baseline},
    }


def test_baseline_parsed():
    spec = parse_spec(_spec_with_baseline(
        {"metric": "coverage", "regex": r"cov=([\d.]+)", "direction": "greater_equal", "value": 90}
    ))
    assert spec.verify.baseline.name == "coverage"
    assert spec.verify.baseline.direction == "greater_equal"
    assert spec.verify.baseline.value == 90.0


def test_baseline_absent_by_default():
    spec = parse_spec({
        "objective": "o", "prompt": "p",
        "agent": {"type": "shell", "command": ["true"]},
        "verify": {"command": ["true"]},
    })
    assert spec.verify.baseline is None


@pytest.mark.parametrize("bad", [
    {"regex": "x", "direction": "approximately", "value": 1},  # bad direction
    {"regex": "(unclosed", "direction": "greater", "value": 1},  # bad regex
    {"regex": "x", "direction": "greater", "value": "high"},  # non-numeric value
    {"direction": "greater", "value": 1},  # missing regex
])
def test_baseline_invalid_raises(bad):
    with pytest.raises(SpecError):
        parse_spec(_spec_with_baseline(bad))


# --- run-loop integration ---

def _baseline_run_spec(verify_body, baseline, **limits):
    return parse_spec({
        "objective": "o",
        "prompt": "{{feedback}}",
        "agent": {"type": "shell", "command": [PY, "-c", "pass"]},
        "verify": {"command": [PY, "-c", verify_body], "baseline": baseline},
        "limits": {"max_iterations": limits.get("mi", 3), "max_consecutive_failures": limits.get("mcf", 2)},
    })


_GATE = {"metric": "score", "regex": r"score=([\d.]+)", "direction": "greater_equal", "value": 0.9}


def test_baseline_pass_when_exit0_and_metric_meets(tmp_path):
    spec = _baseline_run_spec("print('score=0.95'); import sys; sys.exit(0)", _GATE)
    result = run_loop(spec, tmp_path)
    assert result.status == "success"
    iteration = [r for r in Ledger(result.ledger_path).records() if r["event"] == "iteration"][0]
    assert iteration["baseline"] == {"ok": True, "actual": 0.95, "metric": "score"}


def test_baseline_fails_even_when_verify_exits_zero(tmp_path):
    spec = _baseline_run_spec("print('score=0.50'); import sys; sys.exit(0)", _GATE, mi=2, mcf=2)
    result = run_loop(spec, tmp_path)
    assert result.status in ("blocked", "exhausted")
    iteration = [r for r in Ledger(result.ledger_path).records() if r["event"] == "iteration"][0]
    assert iteration["result"] == "fail"  # exit 0, but the metric gate failed
    assert iteration["baseline"]["ok"] is False
    assert "baseline not met" in iteration["feedback"]


def test_baseline_not_evaluated_when_verify_exits_nonzero(tmp_path):
    # verify already fails on exit code; the baseline is not consulted (no metric present).
    spec = _baseline_run_spec("import sys; sys.exit(1)", _GATE, mi=1, mcf=1)
    result = run_loop(spec, tmp_path)
    assert result.status == "blocked"
    iteration = [r for r in Ledger(result.ledger_path).records() if r["event"] == "iteration"][0]
    assert iteration["baseline"]["ok"] is True  # default; not evaluated because exit != 0
    assert "baseline not met" not in iteration["feedback"]
