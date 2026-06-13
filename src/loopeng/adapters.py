"""Agent adapters — every agent is a shell-callable command behind one contract.

Contract (per the project design principle):
  input    -> prompt (on stdin and as $LOOPENG_PROMPT), workspace (cwd), env, spec
  output   -> stdout / stderr / exit_code / artifacts (a ProcResult)
  controls -> timeout, env vars, cwd, max_iterations (enforced by the runner)
  optional -> capabilities: resume, session_id, approval_mode, sandbox

The generic ``ShellAdapter`` is the fully-working core. ``claude-code`` and
``codex`` are thin PRESETS that preconfigure a ShellAdapter's command + flags;
the runner depends only on the ``.run()`` method, never on agent internals.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import List, Optional

from .errors import AdapterError
from .proc import ProcResult, run_proc
from .spec import AgentSpec


@dataclass
class PreflightResult:
    """Outcome of an adapter readiness check, run once before the loop begins."""

    ok: bool
    adapter_type: str
    binary: Optional[str]
    resolved_path: Optional[str]
    reason: str = ""


def normalize_command(value) -> Optional[List[str]]:
    """Turn a str (`sh -lc <str>`) or argv list into an argv list."""
    if value is None:
        return None
    if isinstance(value, str):
        return ["sh", "-lc", value]
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    raise AdapterError(f"command must be a string or list of strings, got {value!r}")


class ShellAdapter:
    """Runs any shell-callable command as the agent. The tested, default path."""

    def __init__(
        self,
        command,
        *,
        args=None,
        env=None,
        capabilities=None,
        prompt_arg: bool = False,
        require_binary: bool = False,
        name: str = "shell",
    ):
        self.command = normalize_command(command)
        if not self.command:
            raise AdapterError("agent.command is required for a shell adapter")
        self.args = list(args or [])
        self.extra_env = {str(k): str(v) for k, v in (env or {}).items()}
        self.capabilities = dict(capabilities or {})
        self.prompt_arg = prompt_arg
        # When True (the named presets), a missing binary fails preflight up front.
        # When False (generic shell), a missing binary is left to surface as a
        # normal exit-127 failure during the loop (the escape-hatch contract).
        self.require_binary = require_binary
        self.name = name

    @property
    def binary(self) -> Optional[str]:
        return self.command[0] if self.command else None

    def build_command(self, prompt: str) -> List[str]:
        command = self.command + self.args
        if self.prompt_arg:
            command = command + [prompt]
        return command

    def preflight(self) -> PreflightResult:
        """Pure readiness check: is this adapter's binary resolvable? No side effects."""
        if not self.command:
            return PreflightResult(False, self.name, None, None, "agent command is empty")
        binary = self.command[0]
        resolved = shutil.which(binary)
        if self.require_binary and resolved is None:
            return PreflightResult(
                False,
                self.name,
                binary,
                None,
                f"{self.name} binary {binary!r} not found on PATH",
            )
        return PreflightResult(True, self.name, binary, resolved)

    def run(self, prompt, *, workspace, timeout, iteration: int = 0, objective: str = "") -> ProcResult:
        env = os.environ.copy()
        env.update(self.extra_env)
        env["LOOPENG_PROMPT"] = prompt
        env["LOOPENG_ITERATION"] = str(iteration)
        env["LOOPENG_OBJECTIVE"] = objective
        env["LOOPENG_WORKSPACE"] = str(workspace)
        # If the prompt is passed as a CLI arg, don't also feed it on stdin
        # (some CLIs block waiting on stdin they don't expect).
        stdin_text = None if self.prompt_arg else prompt
        return run_proc(
            self.build_command(prompt),
            cwd=workspace,
            env=env,
            timeout=timeout,
            stdin_text=stdin_text,
        )


def _build_shell(agent: AgentSpec) -> ShellAdapter:
    return ShellAdapter(
        agent.command,
        args=agent.args,
        env=agent.env,
        capabilities=agent.capabilities,
        prompt_arg=agent.prompt_arg,
        require_binary=False,  # generic escape hatch: missing binary -> exit 127 at runtime
        name=agent.type,
    )


def _build_claude_code(agent: AgentSpec) -> ShellAdapter:
    """PRESET: a CLI wrapper around Claude Code's headless mode (NOT a deep API integration).

    Default invocation: ``claude -p "<prompt>"`` (prompt passed as an argument).
    Set ``command:`` in loop.yaml to pin a custom path or binary; the first element
    is the binary that preflight resolves via PATH before the loop starts.

    NOTE: the flag mapping is best-effort; confirm flags against your installed CLI.
    """
    command = agent.command or ["claude", "-p"]
    args = list(agent.args)
    caps = agent.capabilities
    if caps.get("session_id"):
        if caps.get("resume"):
            args += ["--resume", str(caps["session_id"])]
        else:
            args += ["--session-id", str(caps["session_id"])]
    if caps.get("approval_mode"):
        args += ["--permission-mode", str(caps["approval_mode"])]
    return ShellAdapter(
        command,
        args=args,
        env=agent.env,
        capabilities=caps,
        prompt_arg=True,
        require_binary=True,
        name="claude-code",
    )


def _build_codex(agent: AgentSpec) -> ShellAdapter:
    """PRESET: a CLI wrapper around the Codex CLI non-interactive runner (NOT a deep API integration).

    Default invocation: ``codex exec "<prompt>"``. Set ``command:`` to pin a custom
    path or binary; the first element is the binary preflight resolves via PATH.

    NOTE: the flag mapping is best-effort; confirm flags against your installed CLI.
    """
    command = agent.command or ["codex", "exec"]
    args = list(agent.args)
    caps = agent.capabilities
    if caps.get("sandbox"):
        args += ["--sandbox", str(caps["sandbox"])]
    if caps.get("approval_mode"):
        args += ["--ask-for-approval", str(caps["approval_mode"])]
    return ShellAdapter(
        command,
        args=args,
        env=agent.env,
        capabilities=caps,
        prompt_arg=True,
        require_binary=True,
        name="codex",
    )


_BUILDERS = {
    "shell": _build_shell,
    "mock": _build_shell,
    "claude-code": _build_claude_code,
    "codex": _build_codex,
}


def build_adapter(agent: AgentSpec) -> ShellAdapter:
    builder = _BUILDERS.get(agent.type)
    if builder is None:
        raise AdapterError(
            f"unknown agent type {agent.type!r}; expected one of {sorted(_BUILDERS)}"
        )
    return builder(agent)
