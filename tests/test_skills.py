"""Reusable skills: discovery, parameter rendering, and the two-layer placeholder split."""

import pytest

from loopeng.errors import SkillError
from loopeng.skills import (
    Skill,
    discover_skills,
    load_skill,
    parse_set_args,
    parse_skill,
    render_skill,
    render_to_spec,
)

_TEMPLATE = """\
skill:
  name: demo
  description: A demo skill.
  params:
    target:
      description: thing to act on
      required: true
    mode:
      default: fast
objective: "Process {{target}} in {{mode}} mode"
agent: {type: shell, command: ["sh", "-lc", "echo {{target}}"]}
prompt: "Iteration {{iteration}}. Target {{target}}. Feedback: {{feedback}}"
verify: {command: ["sh", "-lc", "true"]}
limits: {max_iterations: 3}
"""


def test_bundled_skills_are_discoverable(tmp_path):
    skills = discover_skills(tmp_path)
    assert "fix-until-tests-pass" in skills
    assert "shell-converge" in skills
    assert skills["shell-converge"].source == "bundled"
    # bundled skill declares its params
    assert "verify_cmd" in skills["shell-converge"].params


def test_parse_skill_reads_metadata_and_params():
    skill = parse_skill(_TEMPLATE, source="test")
    assert skill.name == "demo"
    assert skill.description == "A demo skill."
    assert skill.params["target"].required is True
    assert skill.params["mode"].required is False
    assert skill.params["mode"].default == "fast"


def test_parse_skill_requires_skill_block():
    with pytest.raises(SkillError, match="skill:"):
        parse_skill("objective: x\nverify: {command: [true]}\n", source="test")


def test_render_substitutes_declared_params_only():
    skill = parse_skill(_TEMPLATE, source="test")
    out = render_skill(skill, {"target": "alpha"})
    assert "Process alpha in fast mode" in out  # required + defaulted param
    # runtime placeholders survive for the loop runner
    assert "{{feedback}}" in out
    assert "{{iteration}}" in out
    # the declared param IS substituted everywhere, including inside the prompt
    assert "Target alpha." in out


def test_render_missing_required_param_errors():
    skill = parse_skill(_TEMPLATE, source="test")
    with pytest.raises(SkillError, match="missing required parameter"):
        render_skill(skill, {})


def test_render_unknown_set_key_errors():
    skill = parse_skill(_TEMPLATE, source="test")
    with pytest.raises(SkillError, match="unknown parameter"):
        render_skill(skill, {"target": "a", "bogus": "b"})


def test_parse_set_args_allows_equals_in_value():
    assert parse_set_args(["k=a=b", "x=1"]) == {"k": "a=b", "x": "1"}
    with pytest.raises(SkillError):
        parse_set_args(["noequals"])


def test_render_to_spec_produces_valid_loopspec():
    skill = parse_skill(_TEMPLATE, source="test")
    spec, rendered = render_to_spec(skill, {"target": "beta", "mode": "slow"})
    assert spec.objective == "Process beta in slow mode"
    assert spec.agent.type == "shell"
    # {{feedback}}/{{iteration}} remain for runtime substitution
    assert "{{feedback}}" in spec.prompt


def test_project_skill_shadows_bundled(tmp_path):
    skills_dir = tmp_path / ".loopeng" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "shell-converge.yaml").write_text(
        "skill: {name: shell-converge, description: overridden}\n"
        "objective: o\nagent: {type: shell, command: [true]}\n"
        "prompt: p\nverify: {command: [true]}\nlimits: {max_iterations: 1}\n",
        encoding="utf-8",
    )
    skills = discover_skills(tmp_path)
    assert skills["shell-converge"].description == "overridden"
    assert skills["shell-converge"].source == "project"


def test_load_unknown_skill_lists_available(tmp_path):
    with pytest.raises(SkillError, match="unknown skill"):
        load_skill("does-not-exist", tmp_path)
