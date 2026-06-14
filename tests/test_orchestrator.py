"""Multi-stage DAG orchestration: level ordering, fan-out/in, cycles, fail_fast.

Stages use the shell adapter and the real ``run_loop`` (no Claude/Codex), so the
tests are deterministic and offline. Ordering is proven structurally: a stage's
verifier reads a file an upstream stage wrote, so it can only pass if the
upstream stage ran first.
"""

import json
import sys

import pytest

from loopeng.errors import OrchestrationError
from loopeng.orchestrator import (
    OrchestrationResult,
    StageResult,
    build_levels,
    orchestrate,
)

PY = sys.executable or "python3"


def _write_plan(dir_path, plan_text):
    plan = dir_path / "plan.yaml"
    plan.write_text(plan_text, encoding="utf-8")
    return plan


def _agent_touch(name):
    """A shell agent argv that creates a marker file ``<name>.done`` in the cwd."""
    return f"[{PY!r}, '-c', \"open('{name}.done','w').close()\"]"


def _verify_file_exists(name):
    """A verify argv that exits 0 iff ``<name>.done`` exists in the cwd."""
    return f"[{PY!r}, '-c', \"import os,sys; sys.exit(0 if os.path.exists('{name}.done') else 1)\"]"


# ---------------------------------------------------------------------------
# build_levels: topological batching + cycle detection
# ---------------------------------------------------------------------------


def test_build_levels_groups_independent_stages_into_one_level():
    # A and B are independent (level 0); C and D both need A,B (level 1); E needs C.
    stages = {
        "A": {"needs": []},
        "B": {"needs": []},
        "C": {"needs": ["A", "B"]},
        "D": {"needs": ["A", "B"]},
        "E": {"needs": ["C"]},
    }
    levels = build_levels(stages)
    assert levels[0] == ["A", "B"]  # independent roots share a level, sorted
    assert levels[1] == ["C", "D"]  # both unblocked once level 0 resolves
    assert levels[2] == ["E"]


def test_build_levels_raises_on_cycle():
    stages = {"A": {"needs": ["B"]}, "B": {"needs": ["A"]}}
    with pytest.raises(OrchestrationError, match="cycle detected"):
        build_levels(stages)


# ---------------------------------------------------------------------------
# (1) linear A -> B: B's verify reads a file A wrote, proving A ran first
# ---------------------------------------------------------------------------


def test_linear_dependency_runs_in_order_and_succeeds(tmp_path):
    plan = _write_plan(
        tmp_path,
        f"""
version: 1
stages:
  A:
    loop:
      objective: write marker A
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: {_agent_touch('A')}}}
      verify: {{command: {_verify_file_exists('A')}}}
      limits: {{max_iterations: 2}}
  B:
    needs: [A]
    loop:
      objective: require A's marker
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: [{PY!r}, '-c', 'pass']}}
      verify: {{command: {_verify_file_exists('A')}}}
      limits: {{max_iterations: 1}}
""",
    )
    result = orchestrate(plan, project_dir=tmp_path)
    assert isinstance(result, OrchestrationResult)
    by_name = {s.name: s for s in result.stages}
    assert by_name["A"].status == "success" and by_name["A"].passed is True
    # B can only pass if A wrote A.done first (B's agent writes nothing).
    assert by_name["B"].status == "success"
    assert by_name["B"].loop_status == "success"
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# (2) fan-out A -> B,C and fan-in B,C -> D, all succeed
# ---------------------------------------------------------------------------


def test_fan_out_and_fan_in_all_succeed(tmp_path):
    plan = _write_plan(
        tmp_path,
        f"""
version: 1
workspace: shared
stages:
  A:
    loop:
      objective: root
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: {_agent_touch('A')}}}
      verify: {{command: {_verify_file_exists('A')}}}
      limits: {{max_iterations: 1}}
  B:
    needs: [A]
    loop:
      objective: branch B needs A
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: {_agent_touch('B')}}}
      verify: {{command: {_verify_file_exists('A')}}}
      limits: {{max_iterations: 1}}
  C:
    needs: [A]
    loop:
      objective: branch C needs A
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: {_agent_touch('C')}}}
      verify: {{command: {_verify_file_exists('A')}}}
      limits: {{max_iterations: 1}}
  D:
    needs: [B, C]
    loop:
      objective: join needs B and C
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: [{PY!r}, '-c', 'pass']}}
      verify: {{command: [{PY!r}, '-c', "import os,sys; sys.exit(0 if os.path.exists('B.done') and os.path.exists('C.done') else 1)"]}}
      limits: {{max_iterations: 1}}
""",
    )
    result = orchestrate(plan, project_dir=tmp_path)
    by_name = {s.name: s for s in result.stages}
    assert all(by_name[n].status == "success" for n in ("A", "B", "C", "D"))
    # D's verify required BOTH B.done and C.done -> the fan-in barrier held.
    assert by_name["D"].passed is True
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# (4) fail_fast: A fails -> B (needs A) is skipped, exit_code 1
# ---------------------------------------------------------------------------


def test_fail_fast_skips_downstream_of_failed_stage(tmp_path):
    plan = _write_plan(
        tmp_path,
        f"""
version: 1
fail_fast: true
stages:
  A:
    loop:
      objective: always fails
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: [{PY!r}, '-c', 'pass']}}
      verify: {{command: [{PY!r}, '-c', 'import sys; sys.exit(1)']}}
      limits: {{max_iterations: 1}}
  B:
    needs: [A]
    loop:
      objective: should never run
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: [{PY!r}, '-c', 'pass']}}
      verify: {{command: [{PY!r}, '-c', 'import sys; sys.exit(0)']}}
      limits: {{max_iterations: 1}}
""",
    )
    result = orchestrate(plan, project_dir=tmp_path)
    by_name = {s.name: s for s in result.stages}
    assert by_name["A"].status == "failed"
    assert by_name["A"].loop_status == "exhausted"  # ran to its 1-iteration cap
    assert by_name["B"].status == "skipped"  # dependency failed -> not run
    assert by_name["B"].loop_status == ""  # skipped stages never ran a loop
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# (5) exit_code aggregation: a skipped stage alone is not a failure, but the
#     failed ancestor makes exit_code 1
# ---------------------------------------------------------------------------


def test_skipped_stage_alone_is_not_a_failure_but_failed_ancestor_is(tmp_path):
    # Unit-level proof that exit_code counts only "failed", not "skipped".
    only_skip = OrchestrationResult(
        plan_path="p", stages=[StageResult("X", "skipped")]
    )
    assert only_skip.exit_code == 0  # a lone skip is NOT a failure

    mixed = OrchestrationResult(
        plan_path="p",
        stages=[
            StageResult("A", "failed", loop_status="exhausted"),
            StageResult("B", "skipped"),  # skipped because A failed
        ],
    )
    assert mixed.exit_code == 1  # the failed ancestor drives the exit code


def test_failed_ancestor_makes_exit_code_one_even_with_a_later_success(tmp_path):
    # End-to-end: A fails; B (needs A) is skipped; C is independent and succeeds.
    # fail_fast off so the independent branch still runs; exit_code is still 1.
    plan = _write_plan(
        tmp_path,
        f"""
version: 1
fail_fast: false
stages:
  A:
    loop:
      objective: fails
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: [{PY!r}, '-c', 'pass']}}
      verify: {{command: [{PY!r}, '-c', 'import sys; sys.exit(1)']}}
      limits: {{max_iterations: 1}}
  B:
    needs: [A]
    loop:
      objective: skipped because A failed
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: [{PY!r}, '-c', 'pass']}}
      verify: {{command: [{PY!r}, '-c', 'import sys; sys.exit(0)']}}
      limits: {{max_iterations: 1}}
  C:
    loop:
      objective: independent success
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: [{PY!r}, '-c', 'pass']}}
      verify: {{command: [{PY!r}, '-c', 'import sys; sys.exit(0)']}}
      limits: {{max_iterations: 1}}
""",
    )
    result = orchestrate(plan, project_dir=tmp_path)
    by_name = {s.name: s for s in result.stages}
    assert by_name["A"].status == "failed"
    assert by_name["B"].status == "skipped"
    assert by_name["C"].status == "success"  # independent branch ran (fail_fast off)
    assert result.exit_code == 1  # any failed stage -> 1


# ---------------------------------------------------------------------------
# Plan parsing / version validation / ledger
# ---------------------------------------------------------------------------


def test_version_must_be_one(tmp_path):
    plan = _write_plan(
        tmp_path,
        f"""
version: 2
stages:
  A:
    loop:
      objective: o
      prompt: p
      agent: {{type: shell, command: [{PY!r}, '-c', 'pass']}}
      verify: {{command: [{PY!r}, '-c', 'import sys; sys.exit(0)']}}
""",
    )
    with pytest.raises(OrchestrationError, match="version must be 1"):
        orchestrate(plan, project_dir=tmp_path)


def test_stage_with_two_spec_sources_is_rejected(tmp_path):
    # A stage declaring both `loop:` and `spec:` is ambiguous -> recorded as a
    # stage failure (resolution happens in the worker), so exit_code is 1.
    plan = _write_plan(
        tmp_path,
        f"""
version: 1
stages:
  A:
    spec: nonexistent.yaml
    loop:
      objective: o
      prompt: p
      agent: {{type: shell, command: [{PY!r}, '-c', 'pass']}}
      verify: {{command: [{PY!r}, '-c', 'import sys; sys.exit(0)']}}
""",
    )
    result = orchestrate(plan, project_dir=tmp_path)
    assert result.stages[0].status == "failed"
    assert "exactly one" in (result.stages[0].error or "")
    assert result.exit_code == 1


def test_orchestration_ledger_is_written(tmp_path):
    plan = _write_plan(
        tmp_path,
        f"""
version: 1
stages:
  A:
    loop:
      objective: o
      prompt: "go {{{{feedback}}}}"
      agent: {{type: shell, command: [{PY!r}, '-c', 'pass']}}
      verify: {{command: [{PY!r}, '-c', 'import sys; sys.exit(0)']}}
      limits: {{max_iterations: 1}}
""",
    )
    orchestrate(plan, project_dir=tmp_path, run_id="testrun")
    ledger_path = tmp_path / ".loopeng" / "orchestrate-testrun.jsonl"
    assert ledger_path.exists()
    records = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
    events = [r["event"] for r in records]
    assert events[0] == "orchestration_start"
    assert "stage_start" in events
    assert "stage_end" in events
    assert events[-1] == "orchestration_end"
    # every record carries a ts and a stage label
    assert all("ts" in r and "stage" in r for r in records)


import shutil  # noqa: E402 - grouped with the git-gated test below


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_workspace_worktree_isolates_main_tree(tmp_path):
    """workspace: worktree runs the whole plan in a throwaway checkout off HEAD.

    Stages still share files with one another (s2 reads what s1 wrote), but the
    user's main working tree is never modified.
    """
    import subprocess

    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "v@x")
    git("config", "user.name", "v")
    (tmp_path / "base.txt").write_text("base\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    plan = _write_plan(
        tmp_path,
        """
version: 1
workspace: worktree
stages:
  s1:
    loop:
      objective: write
      agent: {type: shell, command: ["sh", "-lc", "echo W > w.txt"]}
      prompt: "{{feedback}}"
      verify: {command: ["test", "-f", "w.txt"]}
      limits: {max_iterations: 2}
  s2:
    needs: [s1]
    loop:
      objective: read
      agent: {type: shell, command: ["sh", "-lc", "cp w.txt w2.txt"]}
      prompt: "{{feedback}}"
      verify: {command: ["sh", "-lc", "grep -q W w2.txt"]}
      limits: {max_iterations: 2}
""",
    )
    result = orchestrate(plan, project_dir=tmp_path, run_id="wt")
    assert result.exit_code == 0
    assert result.workspace_mode == "worktree"
    assert result.worktree_branch and result.worktree_branch.startswith("loop/")
    assert result.worktree_kept is True  # branch preserved on success
    # The agents' files must NOT leak into the user's main working tree.
    assert not (tmp_path / "w.txt").exists()
    assert not (tmp_path / "w2.txt").exists()
    # base.txt (the only tracked file) is untouched.
    assert (tmp_path / "base.txt").read_text() == "base\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_gated_stages_in_one_level_run_serially_with_correct_attribution(tmp_path):
    """Two parallel blast-radius-gated stages, each writing ONE file with
    max_changed_files: 1. Concurrently each would see the other's write (2 files) and
    BOTH would fail; serial execution (forced because the level is gated) attributes
    each stage's single write correctly, so both pass."""
    import subprocess

    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "v@x")
    git("config", "user.name", "v")
    (tmp_path / "base.txt").write_text("base\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    plan = _write_plan(
        tmp_path,
        """
version: 1
stages:
  alpha:
    loop:
      objective: write alpha
      agent: {type: shell, command: ["sh", "-lc", "echo A > alpha.txt"]}
      prompt: "go"
      verify: {command: ["test", "-f", "alpha.txt"]}
      limits: {max_iterations: 2, max_changed_files: 1}
  beta:
    loop:
      objective: write beta
      agent: {type: shell, command: ["sh", "-lc", "echo B > beta.txt"]}
      prompt: "go"
      verify: {command: ["test", "-f", "beta.txt"]}
      limits: {max_iterations: 2, max_changed_files: 1}
""",
    )
    result = orchestrate(plan, project_dir=tmp_path, run_id="gated")
    assert result.exit_code == 0
    assert all(s.status == "success" for s in result.stages)


def test_stage_resolved_from_skill_with_non_string_set_value(tmp_path):
    """Exercises the skill: stage-resolution branch and the str(v) coercion of `set:`
    values (a YAML int under set: must be stringified before template substitution)."""
    plan = _write_plan(
        tmp_path,
        """
version: 1
stages:
  s:
    skill: shell-converge
    set:
      agent_cmd: "echo hi > p.txt"
      verify_cmd: "test -f p.txt"
      objective: 7
""",
    )
    result = orchestrate(plan, project_dir=tmp_path, run_id="skill")
    assert result.exit_code == 0
    assert result.stages[0].loop_status == "success"


def test_stage_resolved_from_relative_spec_file(tmp_path):
    """Exercises the spec: stage-resolution branch with a relative path joined to project_dir."""
    (tmp_path / "child.yaml").write_text(
        "objective: child\nagent: {type: shell, command: ['true']}\n"
        "prompt: go\nverify: {command: ['true']}\nlimits: {max_iterations: 1}\n",
        encoding="utf-8",
    )
    plan = _write_plan(
        tmp_path,
        """
version: 1
stages:
  child:
    spec: child.yaml
""",
    )
    result = orchestrate(plan, project_dir=tmp_path, run_id="spec")
    assert result.exit_code == 0
    assert result.stages[0].loop_status == "success"
