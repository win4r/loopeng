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


def test_unknown_agent_type_accepted_by_spec_rejected_by_adapter():
    # The adapter registry (built-ins + plugins) is the source of truth: parse_spec
    # accepts any non-empty type (a plugin may register it later); build_adapter
    # rejects an unknown one, listing the available types.
    from loopeng.adapters import build_adapter
    from loopeng.errors import AdapterError

    data = valid_spec_dict()
    data["agent"]["type"] = "wat"
    spec = parse_spec(data)  # no longer raises at parse time
    assert spec.agent.type == "wat"
    with pytest.raises(AdapterError, match="unknown agent type"):
        build_adapter(spec.agent)


def test_empty_agent_type_rejected():
    data = valid_spec_dict()
    data["agent"]["type"] = ""
    with pytest.raises(SpecError, match="non-empty string"):
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


def test_stall_limits_default_none():
    spec = parse_spec(valid_spec_dict())
    assert spec.limits.no_output_timeout is None
    assert spec.limits.no_progress_limit is None


def test_stall_limits_parsed():
    data = valid_spec_dict()
    data["limits"] = {"no_output_timeout": 30, "no_progress_limit": 3}
    spec = parse_spec(data)
    assert spec.limits.no_output_timeout == 30
    assert spec.limits.no_progress_limit == 3


def test_no_output_timeout_must_be_positive():
    data = valid_spec_dict()
    data["limits"] = {"no_output_timeout": 0}
    with pytest.raises(SpecError):
        parse_spec(data)


def test_no_progress_limit_must_be_positive():
    data = valid_spec_dict()
    data["limits"] = {"no_progress_limit": 0}
    with pytest.raises(SpecError):
        parse_spec(data)


def test_fingerprint_ignores_none_optional_fields():
    import json
    from dataclasses import asdict

    from loopeng.spec import _strip_none, fingerprint

    spec = parse_spec(valid_spec_dict())
    # No None-valued optional (e.g. limits.no_output_timeout, verify.baseline) survives
    # into the fingerprint payload, so adding such a field doesn't change the hash.
    assert "null" not in json.dumps(_strip_none(asdict(spec)))
    assert fingerprint(spec) == fingerprint(parse_spec(valid_spec_dict()))


def test_top_level_blast_radius_block_is_rejected_not_silently_ignored():
    # A natural mistake (the README section + LoopSpec field are both "blast_radius"):
    # placing the block at the top level instead of under `limits:` must fail loudly,
    # not be silently ignored (which would leave the gate inactive).
    data = valid_spec_dict()
    data["blast_radius"] = {"forbidden_paths": ["secrets/**"]}
    with pytest.raises(SpecError, match="must be nested under"):
        parse_spec(data)


def test_top_level_blast_radius_subkeys_are_rejected():
    data = valid_spec_dict()
    data["forbidden_paths"] = ["secrets/**"]   # belongs under limits:
    with pytest.raises(SpecError, match="limits"):
        parse_spec(data)


def test_blast_radius_under_limits_still_works():
    data = valid_spec_dict()
    data["limits"] = {"max_iterations": 3, "forbidden_paths": [".env"], "require_clean_git": True}
    spec = parse_spec(data)
    assert spec.blast_radius.forbidden_paths == [".env"]
    assert spec.blast_radius.require_clean_git is True
