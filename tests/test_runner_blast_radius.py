"""Runner + git integration: the blast-radius gate in a real loop."""

import sys

from loopeng.ledger import Ledger
from loopeng.runner import run_loop
from loopeng.spec import parse_spec

PY = sys.executable or "python3"


def _spec(blast, agent_cmd, verify_cmd, **limits):
    data = {
        "objective": "obj",
        "prompt": "do it {{feedback}}",
        "agent": {"type": "shell", "command": agent_cmd},
        "verify": {"command": verify_cmd},
        "limits": {**limits, **blast},
    }
    return parse_spec(data)


def _iterations(result):
    return [r for r in Ledger(result.ledger_path).records() if r.get("event") == "iteration"]


def test_require_clean_git_passes_on_clean_repo(git_repo):
    spec = _spec(
        {"require_clean_git": True},
        agent_cmd=[PY, "-c", "open('out.txt', 'w').write('DONE')"],
        verify_cmd=[PY, "-c", "import pathlib, sys; sys.exit(0 if pathlib.Path('out.txt').exists() else 1)"],
        max_iterations=3,
        max_consecutive_failures=2,
    )
    result = run_loop(spec, git_repo)
    assert result.status == "success"
    assert result.iterations == 1


def test_require_clean_git_fails_on_dirty_repo(git_repo):
    (git_repo / "dirty.txt").write_text("dirty\n")  # dirty BEFORE the run
    spec = _spec(
        {"require_clean_git": True},
        agent_cmd=[PY, "-c", "print('noop')"],
        verify_cmd=[PY, "-c", "import sys; sys.exit(0)"],
    )
    result = run_loop(spec, git_repo)
    assert result.status == "precondition_failed"
    assert result.iterations == 0
    records = Ledger(result.ledger_path).records()
    assert any(r.get("event") == "blast_radius_precondition_failed" for r in records)


def test_forbidden_path_violation_recorded_and_blocks(git_repo):
    spec = _spec(
        {"require_clean_git": True, "forbidden_paths": [".env", "secrets/**"]},
        agent_cmd=[PY, "-c", "open('.env', 'w').write('SECRET=1')"],
        verify_cmd=[PY, "-c", "import sys; sys.exit(0)"],
        max_iterations=5,
        max_consecutive_failures=2,
    )
    result = run_loop(spec, git_repo)
    assert result.status == "blocked"
    iterations = _iterations(result)
    assert iterations and all(r["reason"] == "blast_radius_violation" for r in iterations)
    assert any(".env" in v for v in iterations[0]["blast_radius"]["violations"])
    # The verifier must NOT run when the blast-radius gate fails.
    assert "verify_exit" not in iterations[0]


def test_change_outside_allowed_paths_blocks(git_repo):
    spec = _spec(
        {"require_clean_git": True, "allowed_paths": ["src/**"]},
        agent_cmd=[PY, "-c", "open('hack.txt', 'w').write('x')"],
        verify_cmd=[PY, "-c", "import sys; sys.exit(0)"],
        max_iterations=3,
        max_consecutive_failures=1,
    )
    result = run_loop(spec, git_repo)
    assert result.status == "blocked"
    iterations = _iterations(result)
    assert any("outside allowed_paths" in v for v in iterations[0]["blast_radius"]["violations"])


def test_too_many_changed_files_blocks(git_repo):
    agent = "import pathlib\n" + "\n".join(f"pathlib.Path('f{i}.txt').write_text('x')" for i in range(12))
    spec = _spec(
        {"require_clean_git": True, "max_changed_files": 10},
        agent_cmd=[PY, "-c", agent],
        verify_cmd=[PY, "-c", "import sys; sys.exit(0)"],
        max_iterations=2,
        max_consecutive_failures=1,
    )
    result = run_loop(spec, git_repo)
    assert result.status == "blocked"
    assert any("max_changed_files" in v for v in _iterations(result)[0]["blast_radius"]["violations"])


def test_allowed_change_inside_repo_passes(git_repo):
    (git_repo / "src").mkdir()
    spec = _spec(
        {"require_clean_git": True, "allowed_paths": ["src/**"], "forbidden_paths": [".env"]},
        agent_cmd=[PY, "-c", "open('src/out.py', 'w').write('# ok')"],
        verify_cmd=[PY, "-c", "import pathlib, sys; sys.exit(0 if pathlib.Path('src/out.py').exists() else 1)"],
        max_iterations=3,
        max_consecutive_failures=2,
    )
    result = run_loop(spec, git_repo)
    assert result.status == "success"
    iteration = _iterations(result)[0]
    assert iteration["result"] == "pass"
    assert iteration["blast_radius"]["ok"] is True


def test_gate_skipped_when_workspace_not_git(tmp_path):
    spec = _spec(
        {"forbidden_paths": [".env"]},
        agent_cmd=[PY, "-c", "open('out.txt', 'w').write('DONE')"],
        verify_cmd=[PY, "-c", "import sys; sys.exit(0)"],
    )
    result = run_loop(spec, tmp_path)
    assert result.status == "success"  # gate can't run without git, loop still works
    assert any(r.get("event") == "blast_radius_skipped" for r in Ledger(result.ledger_path).records())


def test_forbidden_pattern_matches_when_workspace_below_git_root(git_repo):
    # Regression: git reports repo-root-relative paths; the gate must normalize to
    # workspace-relative so a workspace-relative forbidden pattern still matches.
    # Here the workspace `work/` lives BELOW the git root (git_repo).
    work = git_repo / "work"
    work.mkdir()
    spec = _spec(
        {"require_clean_git": False, "forbidden_paths": [".env"]},
        agent_cmd=["sh", "-lc", "echo SECRET=1 > .env"],
        verify_cmd=["true"],
        max_iterations=3,
        max_consecutive_failures=1,
    )
    result = run_loop(spec, work)  # project_dir == work, below the git root
    assert result.status == "blocked"  # without normalization this would slip through
    iterations = _iterations(result)
    assert any(".env matches forbidden" in v for v in iterations[0]["blast_radius"]["violations"])
    # The recorded path is workspace-relative, not 'work/.env'.
    assert ".env" in iterations[0]["blast_radius"]["changed_paths"]


def test_allowed_pattern_matches_when_workspace_below_git_root(git_repo):
    work = git_repo / "work"
    work.mkdir()
    spec = _spec(
        {"require_clean_git": False, "allowed_paths": ["src/**"]},
        agent_cmd=["sh", "-lc", "mkdir -p src && echo ok > src/out.py"],
        verify_cmd=["true"],
        max_iterations=2,
        max_consecutive_failures=1,
    )
    result = run_loop(spec, work)
    assert result.status == "success"  # src/out.py is in-bounds once normalized


def test_baseline_excludes_preexisting_dirt_and_state_dir(git_repo):
    # require_clean_git: false, gate still active via max_changed_files.
    (git_repo / "preexisting.txt").write_text("dirty before the run\n")
    spec = _spec(
        {"require_clean_git": False, "max_changed_files": 5},
        agent_cmd=["sh", "-lc", "echo x > allowed_new.txt"],
        verify_cmd=["true"],
        max_iterations=2,
        max_consecutive_failures=1,
    )
    result = run_loop(spec, git_repo)
    assert result.status == "success"
    changed = _iterations(result)[0]["blast_radius"]["changed_paths"]
    assert "allowed_new.txt" in changed  # the agent's change is counted
    assert "preexisting.txt" not in changed  # pre-existing dirt excluded by baseline
    assert not any(".loopeng" in p.split("/") for p in changed)  # state dir excluded


def test_context_errors_recorded_on_blast_radius_violation(git_repo):
    data = {
        "objective": "o",
        "prompt": "ctx={{bad}} {{feedback}}",
        "agent": {"type": "shell", "command": ["sh", "-lc", "echo SECRET=1 > .env"]},
        "verify": "true",
        "context": {"bad": ["sh", "-lc", "echo boom >&2; exit 2"]},
        "limits": {
            "require_clean_git": True,
            "forbidden_paths": [".env"],
            "max_iterations": 2,
            "max_consecutive_failures": 1,
        },
    }
    result = run_loop(parse_spec(data), git_repo)
    assert result.status == "blocked"
    iteration = _iterations(result)[0]
    assert iteration["reason"] == "blast_radius_violation"
    assert iteration["context_errors"][0]["name"] == "bad"
