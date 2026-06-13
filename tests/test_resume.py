"""Resume resolution + restoration (refusals, fingerprint, counter restore)."""

import sys

from loopeng.cli import main
from loopeng.ledger import Ledger
from loopeng.resume import resolve_resume
from loopeng.runner import run_loop
from loopeng.spec import fingerprint, parse_spec

PY = sys.executable or "python3"


def _spec(verify_cmd, agent_cmd=None, **limits):
    data = {
        "objective": "obj",
        "prompt": "do it {{feedback}}",
        "agent": {"type": "shell", "command": agent_cmd or [PY, "-c", "pass"]},
        "verify": {"command": verify_cmd},
    }
    if limits:
        data["limits"] = limits
    return parse_spec(data)


def _ledger(tmp_path):
    return tmp_path / ".loopeng" / "ledger.jsonl"


def test_resume_refused_when_no_ledger(tmp_path):
    decision = resolve_resume(_ledger(tmp_path), "abc123")
    assert not decision.resumable
    assert decision.reason == "no_ledger"


def test_resume_refused_after_successful_run(tmp_path):
    spec = _spec([PY, "-c", "import sys; sys.exit(0)"])
    run_loop(spec, tmp_path)  # succeeds on iteration 1
    decision = resolve_resume(_ledger(tmp_path), fingerprint(spec))
    assert not decision.resumable
    assert decision.reason == "already_succeeded"


def test_resume_restores_iteration_and_failures(tmp_path):
    spec = _spec([PY, "-c", "import sys; sys.exit(1)"], max_iterations=2, max_consecutive_failures=9)
    r1 = run_loop(spec, tmp_path)
    assert r1.status == "exhausted" and r1.iterations == 2

    decision = resolve_resume(_ledger(tmp_path), fingerprint(spec))
    assert decision.resumable
    assert decision.start_iteration == 2
    assert decision.consecutive_failures == 2  # two consecutive failures restored

    r2 = run_loop(spec, tmp_path, max_iterations=4, resume=decision)
    assert r2.run_id == r1.run_id  # same logical run continues
    assert r2.iterations == 4  # continued at 3, 4
    iters = [
        r for r in Ledger(r2.ledger_path).records()
        if r.get("event") == "iteration" and r.get("run_id") == r1.run_id
    ]
    assert max(r["iteration"] for r in iters) == 4
    # Exactly 4 iteration records (1,2 from the original; 3,4 from the resume) —
    # NOT 6: resume must continue from iteration 3, not re-run 1 and 2.
    assert len(iters) == 4
    assert sorted(r["iteration"] for r in iters) == [1, 2, 3, 4]

    records = Ledger(r2.ledger_path).records()
    run_starts = [r for r in records if r.get("event") == "run_start" and r.get("run_id") == r1.run_id]
    assert len(run_starts) == 1  # resume must NOT write a second run_start
    resume_loaded = [r for r in records if r.get("event") == "resume_loaded" and r.get("run_id") == r1.run_id]
    assert len(resume_loaded) == 1
    assert resume_loaded[0]["start_iteration"] == 2
    assert resume_loaded[0]["consecutive_failures"] == 2


def test_resume_restored_failures_feed_circuit_breaker(tmp_path):
    # Restored consecutive_failures (2) + 3 more failures -> trips a breaker of 5 at iter 5.
    spec = _spec([PY, "-c", "import sys; sys.exit(1)"], max_iterations=2, max_consecutive_failures=5)
    run_loop(spec, tmp_path)  # exhausted at 2, consecutive_failures = 2
    decision = resolve_resume(_ledger(tmp_path), fingerprint(spec))
    assert decision.consecutive_failures == 2

    r2 = run_loop(spec, tmp_path, max_iterations=9, resume=decision)
    assert r2.status == "blocked"
    assert r2.iterations == 5  # 2 restored + iters 3,4,5 -> 5 consecutive -> blocked


def test_resume_refused_on_fingerprint_mismatch_and_force_overrides(tmp_path):
    spec = _spec([PY, "-c", "import sys; sys.exit(1)"], max_iterations=2, max_consecutive_failures=9)
    run_loop(spec, tmp_path)  # exhausted (resumable but for the fingerprint)
    refused = resolve_resume(_ledger(tmp_path), "0000000000000000")  # bogus current fp
    assert not refused.resumable
    assert refused.reason == "fingerprint_mismatch"
    forced = resolve_resume(_ledger(tmp_path), "0000000000000000", force=True)
    assert forced.resumable


def test_resume_refused_on_blocked_unless_force(tmp_path):
    spec = _spec([PY, "-c", "import sys; sys.exit(1)"], max_iterations=9, max_consecutive_failures=2)
    r1 = run_loop(spec, tmp_path)
    assert r1.status == "blocked"
    refused = resolve_resume(_ledger(tmp_path), fingerprint(spec))
    assert not refused.resumable
    assert refused.reason == "blocked_not_resumable"
    forced = resolve_resume(_ledger(tmp_path), fingerprint(spec), force=True)
    assert forced.resumable


def test_cli_resume_refused_no_ledger_exit_6(tmp_path, monkeypatch):
    main(["init", "--path", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / ".loopeng" / "ledger.jsonl").exists()
    assert main(["run", "--spec", "loop.yaml", "--resume"]) == 6


def test_cli_resume_refused_after_success_exit_6(tmp_path, monkeypatch):
    main(["init", "--path", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--spec", "loop.yaml"]) == 0  # sample fail-once-then-pass succeeds
    assert main(["run", "--spec", "loop.yaml", "--resume"]) == 6  # already succeeded
    records = Ledger(tmp_path / ".loopeng" / "ledger.jsonl").records()
    assert records[-1]["event"] == "resume_refused"
    assert records[-1]["reason"] == "already_succeeded"


def test_resume_survives_torn_final_ledger_line(tmp_path):
    # The crash state resume exists to recover from: a truncated final ledger line.
    spec = _spec([PY, "-c", "import sys; sys.exit(1)"], max_iterations=2, max_consecutive_failures=9)
    run_loop(spec, tmp_path)
    with _ledger(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write('{"event":"iteration","run')  # torn line from a crash
    decision = resolve_resume(_ledger(tmp_path), fingerprint(spec))  # must not raise
    assert decision.resumable
    assert decision.start_iteration == 2


def test_latest_run_selected_across_multiple_runs(tmp_path):
    # Run A succeeds; run B (distinct run_id) exhausts in the SAME .loopeng dir.
    run_loop(_spec([PY, "-c", "import sys; sys.exit(0)"]), tmp_path)  # run A: success
    data_b = {
        "objective": "the second run",
        "prompt": "{{feedback}}",
        "agent": {"type": "shell", "command": [PY, "-c", "pass"]},
        "verify": {"command": [PY, "-c", "import sys; sys.exit(1)"]},
        "limits": {"max_iterations": 2, "max_consecutive_failures": 9},
    }
    spec_b = parse_spec(data_b)
    rb = run_loop(spec_b, tmp_path)  # run B: exhausted
    assert rb.status == "exhausted"
    # Must pick the LATEST run (B), not the first (A, which succeeded).
    decision = resolve_resume(_ledger(tmp_path), fingerprint(spec_b))
    assert decision.resumable
    assert decision.run_id == rb.run_id
    assert decision.start_iteration == 2


def test_cli_force_resumes_blocked_run(tmp_path, monkeypatch):
    main(["init", "--path", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    (tmp_path / "loop.yaml").write_text(
        "objective: always fails\n"
        "agent: {type: shell, command: ['sh', '-lc', 'true']}\n"
        "prompt: '{{feedback}}'\n"
        "verify: 'false'\n"
        "limits: {max_iterations: 9, max_consecutive_failures: 2}\n"
    )
    assert main(["run", "--spec", "loop.yaml"]) == 3  # blocked
    assert main(["run", "--spec", "loop.yaml", "--resume"]) == 6  # blocked -> refused
    # --force overrides the blocked refusal; it proceeds (and blocks again -> 3, not 6)
    assert main(["run", "--spec", "loop.yaml", "--resume", "--force"]) == 3


def test_resume_works_with_require_clean_git(git_repo):
    # A fresh run would precondition-fail on a dirty tree, but on resume the dirt is
    # the prior segment's own output, so the precondition must be skipped.
    data = {
        "objective": "o",
        "prompt": "{{feedback}}",
        "agent": {"type": "shell", "command": ["sh", "-lc", "echo step >> progress.txt"]},
        "verify": "test -f ready.flag",
        "limits": {"require_clean_git": True, "max_iterations": 2, "max_consecutive_failures": 9},
    }
    spec = parse_spec(data)
    r1 = run_loop(spec, git_repo)
    assert r1.status == "exhausted"  # ready.flag absent; agent left the tree dirty
    decision = resolve_resume(_ledger(git_repo), fingerprint(spec))
    assert decision.resumable
    (git_repo / "ready.flag").write_text("ok\n")  # resolve the blocker
    r2 = run_loop(spec, git_repo, max_iterations=4, resume=decision)
    assert r2.status == "success"  # NOT precondition_failed


def test_blast_radius_budget_is_per_segment_on_resume(git_repo):
    data = {
        "objective": "o",
        "prompt": "{{feedback}}",
        "agent": {"type": "shell", "command": ["sh", "-lc", "echo x > f$LOOPENG_ITERATION.txt"]},
        "verify": "false",
        "limits": {
            "require_clean_git": True,
            "max_changed_files": 2,
            "max_iterations": 2,
            "max_consecutive_failures": 9,
        },
    }
    spec = parse_spec(data)
    r1 = run_loop(spec, git_repo)  # creates f1.txt, f2.txt (2 <= 2 ok), verify fails -> exhausted
    assert r1.status == "exhausted"
    decision = resolve_resume(_ledger(git_repo), fingerprint(spec))
    r2 = run_loop(spec, git_repo, max_iterations=4, resume=decision)
    records = Ledger(r2.ledger_path).records()
    it3 = [r for r in records if r.get("event") == "iteration" and r.get("iteration") == 3][0]
    # Per-segment: iteration 3 sees only its own f3.txt (re-baselined), not f1/f2/f3.
    assert it3["blast_radius"]["changed_paths"] == ["f3.txt"]
