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
from typing import List, Optional

from .errors import AdapterError
from .proc import ProcResult, run_proc
from .spec import AgentSpec


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
        name: str = "shell",
    ):
        self.command = normalize_command(command)
        if not self.command:
            raise AdapterError("agent.command is required for a shell adapter")
        self.args = list(args or [])
        self.extra_env = {str(k): str(v) for k, v in (env or {}).items()}
        self.capabilities = dict(capabilities or {})
        self.prompt_arg = prompt_arg
        self.name = name

    def build_command(self, prompt: str) -> List[str]:
        command = self.command + self.args
        if self.prompt_arg:
            command = command + [prompt]
        return command

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
        name=agent.type,
    )


def _build_claude_code(agent: AgentSpec) -> ShellAdapter:
    """PRESET (thin): map the contract onto the Claude Code CLI headless mode.

    Default invocation: ``claude -p "<prompt>"`` (prompt passed as an argument).
    Override ``command:`` in loop.yaml to pin a path or flags.

    NOTE: these are best-effort defaults and are NOT validated against a live
    ``claude`` binary in the test suite. Confirm flags against your installed CLI.
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
        command, args=args, env=agent.env, capabilities=caps, prompt_arg=True, name="claude-code"
    )


def _build_codex(agent: AgentSpec) -> ShellAdapter:
    """PRESET (thin): map the contract onto the Codex CLI non-interactive runner.

    Default invocation: ``codex exec "<prompt>"``.

    NOTE: best-effort defaults, NOT validated against a live ``codex`` binary.
    Confirm flags against your installed CLI version.
    """
    command = agent.command or ["codex", "exec"]
    args = list(agent.args)
    caps = agent.capabilities
    if caps.get("sandbox"):
        args += ["--sandbox", str(caps["sandbox"])]
    if caps.get("approval_mode"):
        args += ["--ask-for-approval", str(caps["approval_mode"])]
    return ShellAdapter(
        command, args=args, env=agent.env, capabilities=caps, prompt_arg=True, name="codex"
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
