"""Reusable skills: named, parameterized loop templates.

A *skill* is a `loop.yaml` template carrying a `skill:` metadata block that
declares parameters. `loopeng run --skill <name> --set k=v` renders the template
(substitutes only the declared `{{param}}` placeholders) and runs the resulting
loop. Crucially, the renderer leaves *undeclared* placeholders — `{{feedback}}`,
`{{iteration}}`, `{{objective}}` — untouched so the runner can still substitute
them per iteration. The two placeholder layers compose: render-time params, then
run-time loop variables.

Skills are discovered from, in precedence order:
  1. the project `.loopeng/skills/`     (overrides everything)
  2. the user `~/.loopeng/skills/`
  3. the bundled library (`loopeng/skills_lib/`)
so a project can shadow a bundled skill by reusing its name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .errors import SkillError

SKILLS_DIRNAME = "skills"
_BUNDLED_PACKAGE = "loopeng.skills_lib"
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass
class SkillParam:
    name: str
    description: str = ""
    required: bool = False
    default: Optional[str] = None


@dataclass
class Skill:
    name: str
    description: str
    params: Dict[str, SkillParam]
    raw_text: str
    source: str  # "bundled" | "user" | "project" | a path, for `skill show`

    def declared(self) -> set:
        return set(self.params)


def _coerce_params(raw, *, name: str) -> Dict[str, SkillParam]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SkillError(f"skill {name!r}: `skill.params` must be a mapping")
    out: Dict[str, SkillParam] = {}
    for key, spec in raw.items():
        if spec is None:
            spec = {}
        if not isinstance(spec, dict):
            raise SkillError(f"skill {name!r}: param {key!r} must be a mapping")
        default = spec.get("default")
        out[str(key)] = SkillParam(
            name=str(key),
            description=str(spec.get("description", "")),
            required=bool(spec.get("required", False)) and default is None,
            default=None if default is None else str(default),
        )
    return out


def parse_skill(text: str, *, source: str) -> Skill:
    """Parse a skill file's text into a Skill (without rendering its body).

    The file must be valid YAML, so any `{{placeholder}}` in scalar positions must
    be quoted (e.g. ``objective: "{{objective}}"``). Only the `skill:` block is read
    here; the rest is kept verbatim as ``raw_text`` for rendering.
    """
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SkillError(f"skill at {source}: not valid YAML — {exc}") from exc
    if not isinstance(data, dict):
        raise SkillError(f"skill at {source}: top level must be a mapping")
    meta = data.get("skill")
    if not isinstance(meta, dict):
        raise SkillError(
            f"skill at {source}: missing a `skill:` metadata block (name/description/params)"
        )
    name = str(meta.get("name") or "").strip()
    if not name:
        raise SkillError(f"skill at {source}: `skill.name` is required")
    return Skill(
        name=name,
        description=str(meta.get("description", "")).strip(),
        params=_coerce_params(meta.get("params"), name=name),
        raw_text=text,
        source=source,
    )


def _iter_dir_skills(directory: Path, source: str) -> Dict[str, Skill]:
    found: Dict[str, Skill] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.yaml")):
        try:
            skill = parse_skill(path.read_text(encoding="utf-8"), source=source)
        except (SkillError, OSError, UnicodeDecodeError) as exc:
            # A malformed skill file shouldn't kill discovery of the good ones.
            raise SkillError(f"could not load skill {path}: {exc}") from exc
        found[skill.name] = skill
    return found


def _bundled_skills() -> Dict[str, Skill]:
    from importlib.resources import files

    found: Dict[str, Skill] = {}
    try:
        root = files(_BUNDLED_PACKAGE)
    except (ModuleNotFoundError, FileNotFoundError):
        return found
    for entry in root.iterdir():
        if entry.name.endswith(".yaml"):
            skill = parse_skill(entry.read_text(encoding="utf-8"), source="bundled")
            found[skill.name] = skill
    return found


def user_skills_dir() -> Path:
    return Path.home() / ".loopeng" / SKILLS_DIRNAME


def project_skills_dir(project_dir) -> Path:
    return Path(project_dir) / ".loopeng" / SKILLS_DIRNAME


def discover_skills(project_dir=".") -> Dict[str, Skill]:
    """All skills by name, project shadowing user shadowing bundled."""
    skills: Dict[str, Skill] = {}
    skills.update(_bundled_skills())
    skills.update(_iter_dir_skills(user_skills_dir(), "user"))
    skills.update(_iter_dir_skills(project_skills_dir(project_dir), "project"))
    return skills


def load_skill(name: str, project_dir=".") -> Skill:
    skills = discover_skills(project_dir)
    if name not in skills:
        available = ", ".join(sorted(skills)) or "(none)"
        raise SkillError(f"unknown skill {name!r}. Available: {available}")
    return skills[name]


def parse_set_args(pairs: Optional[List[str]]) -> Dict[str, str]:
    """Turn ``--set k=v`` repeats into a dict; the value may contain `=`."""
    out: Dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise SkillError(f"--set expects key=value, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SkillError(f"--set expects a non-empty key, got {item!r}")
        out[key] = value
    return out


def render_skill(skill: Skill, values: Dict[str, str]) -> str:
    """Render the template text by substituting ONLY declared params.

    Undeclared placeholders (``{{feedback}}``, ``{{iteration}}``, …) are left
    verbatim for the runner. Unknown `--set` keys and missing required params are
    hard errors so a typo never silently produces the wrong loop.
    """
    unknown = set(values) - skill.declared()
    if unknown:
        raise SkillError(
            f"skill {skill.name!r}: unknown parameter(s): {', '.join(sorted(unknown))}. "
            f"Declared: {', '.join(sorted(skill.declared())) or '(none)'}"
        )

    resolved: Dict[str, str] = {}
    missing: List[str] = []
    for pname, param in skill.params.items():
        if pname in values:
            resolved[pname] = values[pname]
        elif param.default is not None:
            resolved[pname] = param.default
        else:
            missing.append(pname)
    if missing:
        raise SkillError(
            f"skill {skill.name!r}: missing required parameter(s): {', '.join(sorted(missing))}"
        )

    def _replace(match: "re.Match") -> str:
        key = match.group(1)
        if key in resolved:
            return resolved[key]
        return match.group(0)  # leave runtime placeholders for the loop runner

    return _PLACEHOLDER.sub(_replace, skill.raw_text)


def render_to_spec(skill: Skill, values: Dict[str, str], *, source: Optional[str] = None):
    """Render a skill and parse it into a validated LoopSpec."""
    import yaml

    from .spec import parse_spec

    rendered = render_skill(skill, values)
    try:
        data = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        raise SkillError(
            f"skill {skill.name!r}: rendered template is not valid YAML — {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise SkillError(f"skill {skill.name!r}: rendered template is not a mapping")
    data.pop("skill", None)  # strip skill metadata before spec validation
    return parse_spec(data, source=source or f"skill:{skill.name}"), rendered
