"""The loop engine: act -> verify -> feed back, bounded by guardrails.

Each iteration:
  1. gather optional context commands and render the prompt template
     (``{{objective}}``, ``{{iteration}}``, ``{{feedback}}``, ``{{<context-name>}}``)
  2. run the agent adapter
  3. run the deterministic verifier (exit 0 == pass)  -- the gate
  4. record a ledger entry
  5. on pass -> success; on fail -> feed the verifier output back as feedback

Termination is bounded three ways: success, the consecutive-failure circuit
breaker (status ``blocked``), and the max-iterations cap (status ``exhausted``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from .adapters import build_adapter, normalize_command
from .ledger import Ledger
from .proc import run_proc
from .spec import LoopSpec

_PLACEHOLDER = re.compile(r"{{\s*([a-zA-Z0-9_.]+)\s*}}")


def render_template(template: str, mapping: Dict[str, object]) -> str:
    """Substitute ``{{key}}`` placeholders; unknown keys are left intact."""

    def replace(match: "re.Match") -> str:
        key = match.group(1)
        return str(mapping[key]) if key in mapping else match.group(0)

    return _PLACEHOLDER.sub(replace, template)


def _truncate(text: str, limit: int = 800) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [+{len(text) - limit} chars]"


@dataclass
class LoopResult:
    status: str  # "success" | "blocked" | "exhausted"
    iterations: int
    passed: bool
    ledger_path: Path


def _gather_context(context, workspace, timeout) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for name, command in context.items():
        result = run_proc(normalize_command(command), cwd=workspace, timeout=timeout)
        values[name] = result.stdout.strip()
    return values


def run_loop(
    spec: LoopSpec,
    project_dir,
    *,
    max_iterations: Optional[int] = None,
    on_event: Optional[Callable[[str], None]] = None,
) -> LoopResult:
    project_dir = Path(project_dir)
    workspace = (project_dir / spec.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    ledger = Ledger(project_dir / ".loopeng" / "ledger.jsonl")
    adapter = build_adapter(spec.agent)
    verify_command = normalize_command(spec.verify.command)
    limit = max_iterations or spec.limits.max_iterations

    def emit(message: str) -> None:
        if on_event:
            on_event(message)

    ledger.append(
        {
            "event": "run_start",
            "objective": spec.objective,
            "agent": spec.agent.type,
            "max_iterations": limit,
            "max_consecutive_failures": spec.limits.max_consecutive_failures,
            "command_timeout": spec.limits.command_timeout,
        }
    )
    emit(f"▶ loop start — objective: {spec.objective!r} (max {limit} iterations)")

    feedback = ""
    consecutive_failures = 0
    status = "exhausted"
    iteration = 0

    while iteration < limit:
        iteration += 1
        context_values = _gather_context(spec.context, workspace, spec.limits.command_timeout)
        prompt = render_template(
            spec.prompt,
            {
                "objective": spec.objective,
                "feedback": feedback,
                "iteration": str(iteration),
                **context_values,
            },
        )

        agent_result = adapter.run(
            prompt,
            workspace=workspace,
            timeout=spec.limits.command_timeout,
            iteration=iteration,
            objective=spec.objective,
        )
        verify_result = run_proc(
            verify_command,
            cwd=workspace,
            timeout=spec.limits.command_timeout,
            stdin_text=prompt,
        )

        passed = verify_result.ok
        feedback = verify_result.feedback
        consecutive_failures = 0 if passed else consecutive_failures + 1

        ledger.append(
            {
                "event": "iteration",
                "iteration": iteration,
                "agent_exit": agent_result.exit_code,
                "agent_ms": agent_result.duration_ms,
                "agent_timed_out": agent_result.timed_out,
                "verify_exit": verify_result.exit_code,
                "verify_ms": verify_result.duration_ms,
                "verify_timed_out": verify_result.timed_out,
                "result": "pass" if passed else "fail",
                "consecutive_failures": consecutive_failures,
                "feedback": _truncate(feedback),
            }
        )
        emit(
            f"  [iter {iteration}] agent exit={agent_result.exit_code} | "
            f"verify {'PASS' if passed else 'FAIL'}"
            + ("" if passed else f" | {_truncate(feedback, 160)}")
        )

        if passed:
            status = "success"
            break
        if consecutive_failures >= spec.limits.max_consecutive_failures:
            status = "blocked"
            emit(
                f"✗ blocked — {consecutive_failures} consecutive failures "
                f"(limit {spec.limits.max_consecutive_failures})"
            )
            break
    else:
        status = "exhausted"
        emit(f"✗ exhausted — reached max {limit} iterations without passing")

    if status == "success":
        emit(f"✓ success in {iteration} iteration(s)")

    ledger.append({"event": "run_end", "status": status, "iterations": iteration})
    return LoopResult(
        status=status,
        iterations=iteration,
        passed=(status == "success"),
        ledger_path=ledger.path,
    )
