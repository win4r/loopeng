"""Spec parsing + validation (no YAML or filesystem needed)."""

import pytest

from loopeng.errors import SpecError
from loopeng.spec import Limits, LoopSpec, parse_spec


def valid_spec_dict():
    return {
        "objective": "do x",
        "prompt": "work on it: {{feedback}}",
        "agent": {"type": "shell", "command": ["true"]},
        "verify": {"command": ["true"]},
    }


def test_parse_valid_with_defaults():
    spec = parse_spec(valid_spec_dict())
    assert isinstance(spec, LoopSpec)
    assert spec.objective == "do x"
    assert spec.workspace == "."
    assert spec.limits == Limits()  # default 10 / 3 / 120
    assert spec.agent.type == "shell"
    assert spec.agent.command == ["true"]
    assert spec.verify.command == ["true"]


def test_custom_limits_parsed():
    data = valid_spec_dict()
    data["limits"] = {"max_iterations": 7, "max_consecutive_failures": 2, "command_timeout": 5}
    spec = parse_spec(data)
    assert spec.limits == Limits(7, 2, 5)


def test_missing_objective_raises():
    data = valid_spec_dict()
    del data["objective"]
    with pytest.raises(SpecError):
        parse_spec(data)


def test_blank_objective_raises():
    data = valid_spec_dict()
    data["objective"] = "   "
    with pytest.raises(SpecError):
        parse_spec(data)


def test_missing_prompt_raises():
    data = valid_spec_dict()
    del data["prompt"]
    with pytest.raises(SpecError):
        parse_spec(data)


def test_missing_verify_raises():
    data = valid_spec_dict()
    del data["verify"]
    with pytest.raises(SpecError):
        parse_spec(data)


def test_bad_agent_type_raises():
    data = valid_spec_dict()
    data["agent"]["type"] = "wat"
    with pytest.raises(SpecError):
        parse_spec(data)


def test_shell_requires_command():
    data = valid_spec_dict()
    del data["agent"]["command"]
    with pytest.raises(SpecError):
        parse_spec(data)


def test_preset_agent_command_optional():
    data = valid_spec_dict()
    data["agent"] = {"type": "claude-code"}  # presets supply a default command
    spec = parse_spec(data)
    assert spec.agent.type == "claude-code"
    assert spec.agent.command is None


@pytest.mark.parametrize("bad_limits", [
    {"max_iterations": 0},
    {"max_consecutive_failures": 0},
    {"command_timeout": 0},
])
def test_invalid_limits_raise(bad_limits):
    data = valid_spec_dict()
    data["limits"] = bad_limits
    with pytest.raises(SpecError):
        parse_spec(data)


def test_non_integer_limits_raise():
    data = valid_spec_dict()
    data["limits"] = {"max_iterations": "abc"}
    with pytest.raises(SpecError):
        parse_spec(data)


def test_string_command_is_accepted():
    data = valid_spec_dict()
    data["verify"] = "test -f output.txt"
    spec = parse_spec(data)
    assert spec.verify.command == "test -f output.txt"


def test_empty_command_list_raises():
    data = valid_spec_dict()
    data["agent"]["command"] = []
    with pytest.raises(SpecError):
        parse_spec(data)


def test_timeout_seconds_alias():
    data = valid_spec_dict()
    data["limits"] = {"timeout_seconds": 45}
    assert parse_spec(data).limits.command_timeout == 45


def test_command_timeout_still_supported():
    data = valid_spec_dict()
    data["limits"] = {"command_timeout": 7}
    assert parse_spec(data).limits.command_timeout == 7


def test_blast_radius_defaults_inactive():
    spec = parse_spec(valid_spec_dict())
    assert spec.blast_radius.active is False


def test_blast_radius_parsed_from_limits():
    data = valid_spec_dict()
    data["limits"] = {
        "require_clean_git": True,
        "max_changed_files": 5,
        "allowed_paths": ["src/**"],
        "forbidden_paths": [".env", "secrets/**"],
    }
    spec = parse_spec(data)
    assert spec.blast_radius.require_clean_git is True
    assert spec.blast_radius.max_changed_files == 5
    assert spec.blast_radius.allowed_paths == ["src/**"]
    assert spec.blast_radius.forbidden_paths == [".env", "secrets/**"]
    assert spec.blast_radius.active is True


def test_blast_radius_bad_allowed_paths_type_raises():
    data = valid_spec_dict()
    data["limits"] = {"allowed_paths": "src/**"}  # must be a list, not a string
    with pytest.raises(SpecError):
        parse_spec(data)


def test_blast_radius_bad_max_changed_files_raises():
    data = valid_spec_dict()
    data["limits"] = {"max_changed_files": "ten"}
    with pytest.raises(SpecError):
        parse_spec(data)
