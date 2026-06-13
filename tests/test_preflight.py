"""Adapter preflight: binary resolution, run-loop gating, doctor, status surfacing."""

import json
import os
import sys

import pytest

from loopeng.adapters import build_adapter
from loopeng.cli import main
from loopeng.errors import AdapterError
from loopeng.heartbeat import HEARTBEAT_FILENAME, read_heartbeat
from loopeng.ledger import Ledger
from loopeng.resume import resolve_resume
from loopeng.runner import run_loop
from loopeng.spec import AgentSpec, fingerprint, parse_spec

PY = sys.executable or "python3"


def _fake_binary(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


# --- adapter-level preflight ---

def test_shell_command_cannot_be_empty():
    with pytest.raises(AdapterError):
        build_adapter(AgentSpec(type="shell", command=[]))


def test_shell_preflight_ok_regardless_of_binary(monkeypatch, tmp_path):
    # The shell adapter is the escape hatch: a missing binary is NOT a preflight
    # failure (it surfaces as exit 127 at runtime instead).
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing resolvable here
    pf = build_adapter(AgentSpec(type="shell", command=["totally-missing-xyz"])).preflight()
    assert pf.ok is True
    assert pf.adapter_type == "shell"
    assert pf.binary == "totally-missing-xyz"
    assert pf.resolved_path is None


def test_claude_missing_binary_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude on PATH
    pf = build_adapter(AgentSpec(type="claude-code")).preflight()
    assert pf.ok is False
    assert pf.adapter_type == "claude-code"
    assert pf.binary == "claude"
    assert "not found" in pf.reason


def test_codex_missing_binary_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    pf = build_adapter(AgentSpec(type="codex")).preflight()
    assert pf.ok is False
    assert pf.adapter_type == "codex"
    assert pf.binary == "codex"


def test_fake_claude_binary_passes(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    fake = _fake_binary(bindir, "claude")
    monkeypatch.setenv("PATH", str(bindir))
    pf = build_adapter(AgentSpec(type="claude-code")).preflight()
    assert pf.ok is True
    assert pf.binary == "claude"
    assert pf.resolved_path == str(fake)


def test_fake_codex_binary_passes(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    fake = _fake_binary(bindir, "codex")
    monkeypatch.setenv("PATH", str(bindir))
    pf = build_adapter(AgentSpec(type="codex")).preflight()
    assert pf.ok is True
    assert pf.resolved_path == str(fake)


def test_custom_binary_path_respected(tmp_path):
    fake = _fake_binary(tmp_path, "my-claude")
    adapter = build_adapter(AgentSpec(type="claude-code", command=[str(fake), "-p"]))
    pf = adapter.preflight()
    assert pf.ok is True
    assert pf.binary == str(fake)
    assert pf.resolved_path == str(fake)
    assert adapter.build_command("PROMPT") == [str(fake), "-p", "PROMPT"]


def test_relative_binary_resolved_against_cwd_not_process_cwd(tmp_path):
    # A relative-with-separator binary must resolve against the workspace (cwd),
    # the way subprocess.run(cwd=...) launches it — not the process CWD.
    (tmp_path / "tools").mkdir()
    _fake_binary(tmp_path / "tools", "agent")
    adapter = build_adapter(AgentSpec(type="claude-code", command=["tools/agent", "-p"]))
    assert adapter.preflight(cwd=tmp_path).ok is True
    assert adapter.preflight(cwd=tmp_path / "elsewhere").ok is False  # matches a failing exec


def test_preflight_resolves_relative_binary_when_workspace_below_project(tmp_path):
    # workspace is a subdir; binary is workspace-relative; process cwd is elsewhere.
    ws = tmp_path / "ws"
    (ws / "tools").mkdir(parents=True)
    _fake_binary(ws / "tools", "agent")
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "{{feedback}}",
            "workspace": "ws",
            "agent": {"type": "claude-code", "command": ["tools/agent", "-p"]},
            "verify": {"command": [PY, "-c", "import sys; sys.exit(0)"]},
        }
    )
    result = run_loop(spec, tmp_path)  # would be preflight_failed without workspace-aware resolution
    assert result.status == "success"


def test_non_executable_custom_binary_reason(tmp_path):
    not_exec = tmp_path / "claude"
    not_exec.write_text("#!/bin/sh\n")  # exists but NOT chmod +x
    pf = build_adapter(AgentSpec(type="claude-code", command=[str(not_exec), "-p"])).preflight()
    assert pf.ok is False
    assert "not executable" in pf.reason


def test_preset_rejects_string_command():
    with pytest.raises(AdapterError):
        build_adapter(AgentSpec(type="claude-code", command="claude -p"))
    with pytest.raises(AdapterError):
        build_adapter(AgentSpec(type="codex", command="codex exec"))


def test_resume_with_missing_binary_fails_preflight(tmp_path):
    fake = _fake_binary(tmp_path / "bin", "claude")
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "{{feedback}}",
            "agent": {"type": "claude-code", "command": [str(fake), "-p"]},
            "verify": {"command": [PY, "-c", "import sys; sys.exit(1)"]},
            "limits": {"max_iterations": 2, "max_consecutive_failures": 9},
        }
    )
    r1 = run_loop(spec, tmp_path)
    assert r1.status == "exhausted"

    (tmp_path / "bin" / "claude").unlink()  # the binary disappears before the resume
    decision = resolve_resume(tmp_path / ".loopeng" / "ledger.jsonl", fingerprint(spec))
    assert decision.resumable
    r2 = run_loop(spec, tmp_path, max_iterations=4, resume=decision)
    assert r2.status == "preflight_failed"
    assert r2.iterations == 2  # the resumed (cumulative) position, not 0

    records = Ledger(r2.ledger_path).records()
    idx_resume = max(i for i, r in enumerate(records) if r.get("event") == "resume_loaded")
    idx_preflight = max(i for i, r in enumerate(records) if r.get("event") == "adapter_preflight_failed")
    assert idx_preflight > idx_resume


def test_doctor_spec_error_exit_2(tmp_path, monkeypatch, capsys):
    (tmp_path / "loop.yaml").write_text("objective: x\n")  # missing prompt/agent/verify -> SpecError
    monkeypatch.chdir(tmp_path)
    assert main(["doctor", "--json"]) == 2
    assert capsys.readouterr().out.strip() == ""  # no malformed JSON on stdout


# --- run-loop gating ---

def _gate_active_claude_spec(verify_cmd):
    return parse_spec(
        {
            "objective": "o",
            "prompt": "{{feedback}}",
            "agent": {"type": "claude-code"},
            "verify": {"command": verify_cmd},
            "limits": {"require_clean_git": False, "forbidden_paths": [".env"]},  # gate ACTIVE
        }
    )


def test_preflight_failure_skips_agent_verify_and_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    sentinel = tmp_path / "verify_ran.txt"
    result = run_loop(_gate_active_claude_spec(["sh", "-lc", f"touch {sentinel}"]), tmp_path)

    assert result.status == "preflight_failed"
    assert result.iterations == 0
    assert not sentinel.exists()  # the verifier never ran (so the agent didn't either)

    records = Ledger(result.ledger_path).records()
    events = [r.get("event") for r in records]
    assert "adapter_preflight_failed" in events  # ledger written
    assert "iteration" not in events  # no agent/verify iteration
    assert "blast_radius_skipped" not in events  # the gate was never reached
    assert records[-1]["event"] == "run_end"
    assert records[-1]["status"] == "preflight_failed"


def test_preflight_failure_updates_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    run_loop(_gate_active_claude_spec(["true"]), tmp_path)
    hb = read_heartbeat(tmp_path / ".loopeng" / HEARTBEAT_FILENAME)
    assert hb["phase"] == "failed"  # terminal phase reflects the failed run
    assert hb["last_event"] == "run_failed"  # the specific reason is in the ledger + status.adapter_preflight


def test_preflight_pass_proceeds_with_custom_binary(tmp_path):
    fake = _fake_binary(tmp_path, "fakeclaude")  # exits 0, ignores args
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "{{feedback}}",
            "agent": {"type": "claude-code", "command": [str(fake), "-p"]},
            "verify": {"command": [PY, "-c", "import sys; sys.exit(0)"]},
        }
    )
    result = run_loop(spec, tmp_path)
    assert result.status == "success"
    records = Ledger(result.ledger_path).records()
    pf = [r for r in records if r.get("event") == "adapter_preflight_passed"][0]
    assert pf["adapter_type"] == "claude-code"
    assert pf["resolved_path"] == str(fake)


# --- doctor ---

def test_doctor_missing_binary_exit_7(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    (tmp_path / "loop.yaml").write_text(
        "objective: o\nagent: {type: codex}\nprompt: '{{feedback}}'\nverify: 'true'\n"
    )
    monkeypatch.chdir(tmp_path)
    assert main(["doctor", "--json"]) == 7
    report = json.loads(capsys.readouterr().out.strip())
    assert report["adapter_type"] == "codex"
    assert report["ok"] is False


def test_doctor_shell_ok_exit_0(tmp_path, monkeypatch, capsys):
    (tmp_path / "loop.yaml").write_text(
        "objective: o\nagent: {type: shell, command: ['true']}\nprompt: '{{feedback}}'\nverify: 'true'\n"
    )
    monkeypatch.chdir(tmp_path)
    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out.strip())
    assert report["ok"] is True
    assert report["adapter_type"] == "shell"
