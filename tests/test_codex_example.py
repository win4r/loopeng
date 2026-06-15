"""The committed Codex CLI example (examples/codex-cli-demo/) — spec + argv path.

These tests are pure and OFFLINE: they load the example spec and assert the argv the codex
adapter builds, plus a pure preflight. They need NO Codex binary and NO Codex login, so they
run in CI. A real end-to-end Codex run is OPT-IN only (set LOOPENG_CODEX_SMOKE=1).
"""
import os
import pathlib
import shutil
import subprocess

import pytest

from loopeng.adapters import build_adapter
from loopeng.spec import load_spec

DEMO = pathlib.Path(__file__).resolve().parents[1] / "examples" / "codex-cli-demo"
SPEC = DEMO / "loop.yaml"


def test_example_files_exist():
    assert SPEC.is_file()
    assert (DEMO / "verify.py").is_file()
    assert (DEMO / "greeting.py").is_file()
    assert (DEMO / "README.md").is_file()


def test_example_is_a_codex_workspace_write_loop():
    spec = load_spec(SPEC)
    assert spec.agent.type == "codex"
    assert spec.agent.capabilities.get("sandbox") == "workspace-write"
    assert spec.agent.capabilities.get("approval_mode") == "never"
    # the verifier is a fixed, deterministic command (the gate), not the agent
    assert spec.verify.command == ["python3", "verify.py"]


def test_example_confines_writes_with_blast_radius():
    """The hardening: the agent is confined to the source by an allow-list (anti-cheat)."""
    spec = load_spec(SPEC)
    assert spec.blast_radius.allowed_paths == ["greeting.py"]   # agent may edit ONLY the source
    assert spec.blast_radius.max_changed_files == 2
    assert spec.blast_radius.active  # the gate is actually engaged


def test_example_builds_expected_codex_argv():
    """Ties the committed example to the adapter so the docs/example can't drift from code."""
    spec = load_spec(SPEC)
    argv = build_adapter(spec.agent).build_command("PROMPT")
    assert argv[:2] == ["codex", "exec"]
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[-1] == "PROMPT"  # prompt is the final CLI arg (prompt_arg)
    assert "--ask-for-approval" not in argv  # the removed flag must never reappear
    # if the example sets approval_mode, it must use the stable `-c approval_policy=` override
    mode = spec.agent.capabilities.get("approval_mode")
    if mode:
        i = argv.index("-c")
        assert argv[i + 1] == f"approval_policy={mode}"


def test_example_preflight_is_pure_and_login_free():
    """Preflight only resolves the `codex` binary — it never logs in or makes a Codex call."""
    pf = build_adapter(load_spec(SPEC).agent).preflight()
    assert pf.adapter_type == "codex" and pf.binary == "codex"
    assert pf.ok == (pf.resolved_path is not None)  # ok iff `codex` is resolvable on PATH


@pytest.mark.skipif(
    not os.environ.get("LOOPENG_CODEX_SMOKE"),
    reason="opt-in real Codex smoke test; set LOOPENG_CODEX_SMOKE=1 (needs `codex` installed + logged in)",
)
def test_codex_smoke_end_to_end(tmp_path, monkeypatch):
    """OPT-IN ONLY: drive `loopeng run` on the demo with a real, logged-in Codex CLI.

    Sets up a throwaway git repo (so the blast-radius allow-list is active) and runs in-tree,
    then asserts the agent fixed greeting() — i.e. the loop converged through the real verifier.
    """
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not on PATH")
    from loopeng.cli import main

    for name in ("loop.yaml", "verify.py", "greeting.py"):
        shutil.copy(DEMO / name, tmp_path / name)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path, check=True,
    )
    monkeypatch.chdir(tmp_path)
    assert main(["run"]) == 0, "codex did not converge the demo loop"
    assert "Hello, loopeng!" in (tmp_path / "greeting.py").read_text()
