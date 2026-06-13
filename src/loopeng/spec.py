"""The portable loop spec: parse + validate loop.yaml into typed dataclasses.

Keeping parsing (``parse_spec`` on a plain dict) separate from loading
(``load_spec`` from a YAML file) means the validation logic is testable without
any YAML dependency or filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Union

from .blast_radius import BlastRadiusPolicy
from .errors import SpecError

ALLOWED_AGENT_TYPES = ("shell", "mock", "claude-code", "codex")

# A command may be given as an argv list (exec'd directly) or a string
# (run via `sh -lc <string>`, normalized later in adapters.normalize_command).
Command = Union[str, List[str]]


@dataclass
class Limits:
    max_iterations: int = 10
    max_consecutive_failures: int = 3
    command_timeout: int = 120


@dataclass
class AgentSpec:
    type: str = "shell"
    command: object = None  # Command | None — required for shell/mock
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    capabilities: Dict[str, object] = field(default_factory=dict)
    prompt_arg: bool = False


@dataclass
class VerifySpec:
    command: object  # Command — the deterministic gate


@dataclass
class LoopSpec:
    objective: str
    prompt: str
    agent: AgentSpec
    verify: VerifySpec
    workspace: str = "."
    context: Dict[str, object] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    blast_radius: BlastRadiusPolicy = field(default_factory=BlastRadiusPolicy)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SpecError(message)


def _parse_blast_radius(limits_raw: dict) -> BlastRadiusPolicy:
    """Blast-radius keys live under ``limits:`` in the spec (see README)."""

    def _str_list(key: str) -> List[str]:
        value = limits_raw.get(key, []) or []
        _require(
            isinstance(value, list) and all(isinstance(item, str) for item in value),
            f"limits.{key} must be a list of strings",
        )
        return list(value)

    max_changed_raw = limits_raw.get("max_changed_files", None)
    if max_changed_raw is None:
        max_changed_files = None
    else:
        try:
            max_changed_files = int(max_changed_raw)
        except (TypeError, ValueError) as exc:
            raise SpecError(f"limits.max_changed_files must be an integer: {exc}") from exc
        _require(max_changed_files >= 0, "limits.max_changed_files must be >= 0")

    return BlastRadiusPolicy(
        require_clean_git=bool(limits_raw.get("require_clean_git", False)),
        max_changed_files=max_changed_files,
        allowed_paths=_str_list("allowed_paths"),
        forbidden_paths=_str_list("forbidden_paths"),
    )


def _as_command(value, field_name: str) -> Command:
    _require(value is not None, f"{field_name} is required")
    if isinstance(value, str):
        _require(value.strip() != "", f"{field_name} must not be an empty string")
        return value
    if isinstance(value, list):
        _require(
            len(value) > 0 and all(isinstance(item, str) for item in value),
            f"{field_name} must be a non-empty list of strings",
        )
        return value
    raise SpecError(f"{field_name} must be a string or a list of strings")


def parse_spec(data, *, source: str = "<dict>") -> LoopSpec:
    """Validate a plain dict (already-parsed YAML) into a LoopSpec."""
    _require(isinstance(data, dict), f"{source}: the top level must be a mapping")

    objective = data.get("objective")
    _require(
        isinstance(objective, str) and objective.strip(),
        "objective is required and must be a non-empty string",
    )

    prompt = data.get("prompt")
    _require(
        isinstance(prompt, str) and prompt.strip(),
        "prompt is required and must be a non-empty string",
    )

    agent_raw = data.get("agent")
    _require(isinstance(agent_raw, dict), "agent is required and must be a mapping")
    agent_type = agent_raw.get("type", "shell")
    _require(
        agent_type in ALLOWED_AGENT_TYPES,
        f"agent.type must be one of {list(ALLOWED_AGENT_TYPES)}, got {agent_type!r}",
    )
    command = agent_raw.get("command")
    if agent_type in ("shell", "mock"):
        _require(
            command is not None,
            "agent.command is required when agent.type is 'shell' or 'mock'",
        )
    if command is not None:
        command = _as_command(command, "agent.command")
    agent = AgentSpec(
        type=agent_type,
        command=command,
        args=list(agent_raw.get("args", []) or []),
        env=dict(agent_raw.get("env", {}) or {}),
        capabilities=dict(agent_raw.get("capabilities", {}) or {}),
        prompt_arg=bool(agent_raw.get("prompt_arg", False)),
    )

    verify_raw = data.get("verify")
    _require(
        verify_raw is not None,
        "verify is required — a deterministic gate is the load-bearing half of the loop",
    )
    if isinstance(verify_raw, dict):
        verify_command = _as_command(verify_raw.get("command"), "verify.command")
    else:
        verify_command = _as_command(verify_raw, "verify")
    verify = VerifySpec(command=verify_command)

    workspace = data.get("workspace", ".")
    _require(
        isinstance(workspace, str) and workspace.strip(),
        "workspace must be a non-empty string",
    )

    context_raw = data.get("context", {}) or {}
    _require(isinstance(context_raw, dict), "context must be a mapping of name -> command")
    context = {str(name): _as_command(cmd, f"context.{name}") for name, cmd in context_raw.items()}

    limits_raw = data.get("limits", {}) or {}
    _require(isinstance(limits_raw, dict), "limits must be a mapping")
    # `timeout_seconds` is the preferred key; `command_timeout` stays as an alias.
    timeout_raw = limits_raw.get("timeout_seconds", limits_raw.get("command_timeout", 120))
    try:
        limits = Limits(
            max_iterations=int(limits_raw.get("max_iterations", 10)),
            max_consecutive_failures=int(limits_raw.get("max_consecutive_failures", 3)),
            command_timeout=int(timeout_raw),
        )
    except (TypeError, ValueError) as exc:
        raise SpecError(f"limits values must be integers: {exc}") from exc
    _require(limits.max_iterations >= 1, "limits.max_iterations must be >= 1")
    _require(limits.max_consecutive_failures >= 1, "limits.max_consecutive_failures must be >= 1")
    _require(limits.command_timeout > 0, "limits.command_timeout (timeout_seconds) must be > 0")

    blast_radius = _parse_blast_radius(limits_raw)

    return LoopSpec(
        objective=objective,
        prompt=prompt,
        agent=agent,
        verify=verify,
        workspace=workspace,
        context=context,
        limits=limits,
        blast_radius=blast_radius,
    )


def load_spec(path) -> LoopSpec:
    """Load and validate a loop spec from a YAML file."""
    spec_path = Path(path)
    _require(spec_path.exists(), f"spec file not found: {spec_path}")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SpecError(
            "PyYAML is required to parse loop.yaml — install it with `pip install pyyaml`"
        ) from exc
    try:
        data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecError(f"invalid YAML in {spec_path}: {exc}") from exc
    return parse_spec(data, source=str(spec_path))
