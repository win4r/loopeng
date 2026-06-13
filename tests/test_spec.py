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
