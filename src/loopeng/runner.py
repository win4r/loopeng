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
from typing import Callable, Dict, List, Optional, Tuple

from . import git_state
from .adapters import build_adapter, normalize_command
from .blast_radius import evaluate_changes
from .ledger import Ledger
from .proc import run_proc
from .spec import LoopSpec

_PLACEHOLDER = re.compile(r"{{\s*([a-zA-Z0-9_.]+)\s*}}")

# loopeng writes its own ledger here inside the workspace; never count it as an
# agent change (it would otherwise trip require_clean_git and the file count).
STATE_DIR = ".loopeng"


def _to_workspace_relative(path: str, prefix: str) -> Optional[str]:
    """Map a repo-root-relative git path to a workspace-relative one.

    At the repo root (``prefix == ""``) paths are already workspace-relative.
    A path outside the workspace subtree returns ``None`` — the caller keeps the
    repo-relative path so an allowlist still flags an escape rather than missing it.
    """
    if not prefix:
        return path
    if path == prefix.rstrip("/"):
        return ""  # the workspace directory entry itself
    if path.startswith(prefix):
        return path[len(prefix):]
    return None


def _dirty_paths(workspace) -> set:
    """Changed paths, workspace-relative, excluding loopeng's own state dir.

    ``git status`` reports repo-root-relative paths; user patterns are authored
    relative to the workspace, so the workspace prefix is stripped here.
    """
    prefix = git_state.workspace_prefix(workspace)
    out = set()
    for path in git_state.changed_path_set(workspace):
        if STATE_DIR in path.split("/"):
            continue  # loopeng's own ledger dir, wherever it sits in the repo
        relative = _to_workspace_relative(path, prefix)
        if relative is None:
            out.add(path)  # outside the workspace: keep so an allowlist flags it
        elif relative:
            out.add(relative)
    return out


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


def _gather_context(context, workspace, timeout) -> Tuple[Dict[str, str], List[dict]]:
    values: Dict[str, str] = {}
    errors: List[dict] = []
    for name, command in context.items():
        result = run_proc(normalize_command(command), cwd=workspace, timeout=timeout)
        values[name] = result.stdout.strip()
        if not result.ok:
            errors.append(
                {
                    "name": name,
                    "exit": result.exit_code,
                    "timed_out": result.timed_out,
                    "stderr": _truncate(result.stderr, 200),
                }
            )
    return values, errors


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
            "blast_radius_active": spec.blast_radius.active,
        }
    )
    emit(f"▶ loop start — objective: {spec.objective!r} (max {limit} iterations)")

    # --- blast-radius gate setup (a repository write-set gate, NOT a sandbox) ---
    policy = spec.blast_radius
    gate_active = policy.active
    baseline: set = set()
    if gate_active:
        if not git_state.is_git_repo(workspace):
            emit(
                "⚠ blast-radius controls are configured but the workspace is not a "
                "git repository — skipping the write-set gate"
            )
            ledger.append(
                {"event": "blast_radius_skipped", "reason": "workspace is not a git repository"}
            )
            gate_active = False
        else:
            if policy.require_clean_git and _dirty_paths(workspace):
                emit(
                    "✗ precondition failed — require_clean_git is set but the working "
                    "tree is not clean at loop start"
                )
                ledger.append(
                    {
                        "event": "blast_radius_precondition_failed",
                        "reason": "working tree not clean at loop start",
                    }
                )
                ledger.append(
                    {"event": "run_end", "status": "precondition_failed", "iterations": 0}
                )
                return LoopResult(
                    status="precondition_failed",
                    iterations=0,
                    passed=False,
                    ledger_path=ledger.path,
                )
            # Baseline = anything already dirty before the loop, so the gate only
            # judges what the agent itself changes (empty when require_clean_git).
            baseline = _dirty_paths(workspace)

    feedback = ""
    consecutive_failures = 0
    status = "exhausted"
    iteration = 0

    while iteration < limit:
        iteration += 1
        context_values, context_errors = _gather_context(
            spec.context, workspace, spec.limits.command_timeout
        )
        if context_errors:
            emit(
                f"  [iter {iteration}] context command(s) failed: "
                + ", ".join(error["name"] for error in context_errors)
            )
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

        # Blast-radius gate: inspect what the agent touched BEFORE verifying.
        agent_changed: Optional[List[str]] = None
        if gate_active:
            agent_changed = sorted(_dirty_paths(workspace) - baseline)
            blast = evaluate_changes(policy, agent_changed)
            if not blast.ok:
                feedback = "blast_radius_violation: " + blast.reason
                consecutive_failures += 1
                record = {
                    "event": "iteration",
                    "iteration": iteration,
                    "agent_exit": agent_result.exit_code,
                    "agent_ms": agent_result.duration_ms,
                    "agent_timed_out": agent_result.timed_out,
                    "result": "fail",
                    "reason": "blast_radius_violation",
                    "blast_radius": {
                        "ok": False,
                        "violations": blast.violations,
                        "changed_paths": blast.changed_paths,
                    },
                    "consecutive_failures": consecutive_failures,
                    "feedback": _truncate(feedback),
                }
                if context_errors:
                    record["context_errors"] = context_errors
                ledger.append(record)
                emit(
                    f"  [iter {iteration}] agent exit={agent_result.exit_code} | "
                    f"BLAST-RADIUS VIOLATION | {_truncate(blast.reason, 160)}"
                )
                if consecutive_failures >= spec.limits.max_consecutive_failures:
                    status = "blocked"
                    emit(
                        f"✗ blocked — {consecutive_failures} consecutive failures "
                        f"(limit {spec.limits.max_consecutive_failures})"
                    )
                    break
                continue

        verify_result = run_proc(
            verify_command,
            cwd=workspace,
            timeout=spec.limits.command_timeout,
            stdin_text=prompt,
        )

        passed = verify_result.ok
        feedback = verify_result.feedback
        consecutive_failures = 0 if passed else consecutive_failures + 1

        record = {
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
        if context_errors:
            record["context_errors"] = context_errors
        if gate_active:
            record["blast_radius"] = {"ok": True, "changed_paths": agent_changed}
        ledger.append(record)
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
