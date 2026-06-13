"""Adapter argv construction (the shell/mock path and the thin presets)."""

import pytest

from loopeng.adapters import ShellAdapter, build_adapter
from loopeng.errors import AdapterError
from loopeng.spec import AgentSpec


def test_shell_adapter_argv_list():
    adapter = ShellAdapter(["python3", "agent.py"], args=["--flag"])
    assert adapter.build_command("PROMPT") == ["python3", "agent.py", "--flag"]


def test_shell_adapter_string_command_normalized():
    adapter = ShellAdapter("echo hi")
    assert adapter.build_command("PROMPT") == ["sh", "-lc", "echo hi"]


def test_shell_adapter_prompt_arg_appends_prompt():
    adapter = ShellAdapter(["agent"], prompt_arg=True)
    assert adapter.build_command("PROMPT") == ["agent", "PROMPT"]


def test_mock_is_shell_alias():
    adapter = build_adapter(AgentSpec(type="mock", command=["true"]))
    assert adapter.build_command("P") == ["true"]


def test_claude_preset_argv():
    adapter = build_adapter(AgentSpec(type="claude-code"))
    assert adapter.name == "claude-code"
    assert adapter.build_command("PROMPT") == ["claude", "-p", "PROMPT"]


def test_codex_preset_argv():
    adapter = build_adapter(AgentSpec(type="codex"))
    assert adapter.name == "codex"
    assert adapter.build_command("PROMPT") == ["codex", "exec", "PROMPT"]


def test_unknown_agent_type_raises():
    with pytest.raises(AdapterError):
        build_adapter(AgentSpec(type="nope", command=["true"]))
