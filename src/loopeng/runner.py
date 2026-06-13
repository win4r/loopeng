"""The loop engine: act -> verify -> feed back, bounded by guardrails.

Each iteration:
  1. gather optional context commands and render the prompt template
     (``{{objective}}``, ``{{iteration}}``, ``{{feedback}}``, ``{{<context-name>}}``)
  2. run the agent adapter
  3. (if configured) check the blast-radius write-set gate
  4. run the deterministic verifier (exit 0 == pass)
  5. record a ledger entry; on pass -> success, on fail -> feed verifier output back

Every run has a stable ``run_id``. Lifecycle is published two ways: typed events
to ``on_event`` (live), and milestone records to the append-only ledger (durable,
resume-able). A ``.loopeng/heartbeat.json`` is rewritten at each phase for ``status``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Tuple

from . import events as ev
from . import git_state
from .adapters import build_adapter, normalize_command
from .blast_radius import evaluate_changes
from .heartbeat import (
    HEARTBEAT_FILENAME,
    PHASE_BLOCKED,
    PHASE_CHECKING_BLAST_RADIUS,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_GATHERING_CONTEXT,
    PHASE_RUNNING_AGENT,
    PHASE_STARTING,
    PHASE_VERIFYING,
    PHASE_WRITING_LEDGER,
    HeartbeatWriter,
)
from .ledger import LEDGER_SCHEMA_VERSION, Ledger
from .proc import run_proc
from .spec import LoopSpec
from .spec import fingerprint as spec_fingerprint

_PLACEHOLDER = re.compile(r"{{\s*([a-zA-Z0-9_.]+)\s*}}")

# loopeng writes its own ledger/heartbeat here inside the workspace; never count
# it as an agent change (it would otherwise trip require_clean_git / file counts).
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
    status: str  # "success" | "blocked" | "exhausted" | "precondition_failed"
    iterations: int
    passed: bool
    ledger_path: Path
    run_id: str = ""


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
    on_event: Optional[Callable[[dict], None]] = None,
    resume=None,
    run_id: Optional[str] = None,
    spec_path: Optional[str] = None,
) -> LoopResult:
    project_dir = Path(project_dir)
    workspace = (project_dir / spec.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    ledger = Ledger(project_dir / STATE_DIR / "ledger.jsonl")
    adapter = build_adapter(spec.agent)
    verify_command = normalize_command(spec.verify.command)
    limit = max_iterations or spec.limits.max_iterations
    fingerprint = spec_fingerprint(spec)

    resuming = bool(resume and getattr(resume, "resumable", False))
    if resuming:
        run_id = resume.run_id
        start_iteration = resume.start_iteration
        start_failures = resume.consecutive_failures
    else:
        run_id = run_id or ev.new_run_id()
        start_iteration = 0
        start_failures = 0

    # Preserve the logical run's original start time across a resume.
    started_at = (getattr(resume, "prior_started_at", "") if resuming else "") or ev.utcnow_iso()
    heartbeat = HeartbeatWriter(
        project_dir / STATE_DIR / HEARTBEAT_FILENAME,
        run_id=run_id,
        pid=os.getpid(),
        cwd=os.getcwd(),
        spec_path=spec_path or str(project_dir / "loop.yaml"),
        spec_fingerprint=fingerprint,
        max_iterations=limit,
        started_at=started_at,
    )

    st = SimpleNamespace(iteration=start_iteration, consecutive_failures=start_failures, last_event=None)

    def emit(event_type: str, **fields) -> dict:
        event = ev.make_event(event_type, run_id, **fields)
        st.last_event = event_type
        if on_event:
            on_event(event)
        return event

    def beat(phase: str) -> None:
        heartbeat.update(
            phase=phase,
            iteration=st.iteration,
            consecutive_failures=st.consecutive_failures,
            last_event=st.last_event,
        )
        emit(ev.HEARTBEAT_WRITTEN, phase=phase)

    if resuming:
        emit(ev.RESUME_STARTED, prior_status=resume.prior_status)
    else:
        ledger.append(
            {
                "event": "run_start",
                "type": ev.RUN_STARTED,
                "run_id": run_id,
                "ledger_schema_version": LEDGER_SCHEMA_VERSION,
                "spec_fingerprint": fingerprint,
                "objective": spec.objective,
                "agent": spec.agent.type,
                "max_iterations": limit,
                "max_consecutive_failures": spec.limits.max_consecutive_failures,
                "command_timeout": spec.limits.command_timeout,
                "blast_radius_active": spec.blast_radius.active,
            }
        )
    emit(ev.RUN_STARTED, objective=spec.objective, max_iterations=limit, resumed=resuming)
    beat(PHASE_STARTING)
    if resuming:
        forced = bool(
            (resume.prior_fingerprint and resume.prior_fingerprint != fingerprint)
            or resume.prior_status == "blocked"
        )
        ledger.append(
            {
                "event": "resume_loaded",
                "type": ev.RESUME_LOADED,
                "run_id": run_id,
                "start_iteration": start_iteration,
                "consecutive_failures": start_failures,
                "spec_fingerprint": fingerprint,
                "prior_fingerprint": resume.prior_fingerprint,
                "forced": forced,
            }
        )
        emit(
            ev.RESUME_LOADED,
            start_iteration=start_iteration,
            consecutive_failures=start_failures,
            forced=forced,
        )

    # --- adapter preflight (before any agent/verifier/gate work) ---
    preflight = adapter.preflight()
    if not preflight.ok:
        emit(
            ev.ADAPTER_PREFLIGHT_FAILED,
            adapter_type=preflight.adapter_type,
            binary=preflight.binary,
            reason=preflight.reason,
        )
        ledger.append(
            {
                "event": "adapter_preflight_failed",
                "type": ev.ADAPTER_PREFLIGHT_FAILED,
                "run_id": run_id,
                "adapter_type": preflight.adapter_type,
                "binary": preflight.binary,
                "resolved_path": preflight.resolved_path,
                "reason": preflight.reason,
            }
        )
        ledger.append(
            {
                "event": "run_end",
                "type": ev.RUN_FAILED,
                "run_id": run_id,
                "status": "preflight_failed",
                "iterations": st.iteration,
            }
        )
        emit(ev.RUN_FAILED, status="preflight_failed", reason=preflight.reason)
        beat(PHASE_FAILED)
        return LoopResult(
            status="preflight_failed",
            iterations=st.iteration,
            passed=False,
            ledger_path=ledger.path,
            run_id=run_id,
        )
    emit(
        ev.ADAPTER_PREFLIGHT_PASSED,
        adapter_type=preflight.adapter_type,
        binary=preflight.binary,
        resolved_path=preflight.resolved_path,
    )
    ledger.append(
        {
            "event": "adapter_preflight_passed",
            "type": ev.ADAPTER_PREFLIGHT_PASSED,
            "run_id": run_id,
            "adapter_type": preflight.adapter_type,
            "binary": preflight.binary,
            "resolved_path": preflight.resolved_path,
        }
    )

    # --- blast-radius gate setup (a repository write-set gate, NOT a sandbox) ---
    policy = spec.blast_radius
    gate_active = policy.active
    baseline: set = set()
    if gate_active:
        if not git_state.is_git_repo(workspace):
            ledger.append(
                {
                    "event": "blast_radius_skipped",
                    "type": ev.BLAST_RADIUS_SKIPPED,
                    "run_id": run_id,
                    "reason": "workspace is not a git repository",
                }
            )
            emit(ev.BLAST_RADIUS_SKIPPED, reason="workspace is not a git repository")
            gate_active = False
        else:
            # On resume the tree is dirty with the prior segment's own output, so
            # the clean-tree precondition only applies to a fresh run.
            if policy.require_clean_git and not resuming and _dirty_paths(workspace):
                ledger.append(
                    {
                        "event": "blast_radius_precondition_failed",
                        "type": ev.RUN_FAILED,
                        "run_id": run_id,
                        "reason": "working tree not clean at loop start",
                    }
                )
                ledger.append(
                    {
                        "event": "run_end",
                        "type": ev.RUN_FAILED,
                        "run_id": run_id,
                        "status": "precondition_failed",
                        "iterations": st.iteration,
                    }
                )
                emit(
                    ev.RUN_FAILED,
                    status="precondition_failed",
                    reason="working tree not clean at loop start",
                )
                beat(PHASE_FAILED)
                return LoopResult(
                    status="precondition_failed",
                    iterations=st.iteration,
                    passed=False,
                    ledger_path=ledger.path,
                    run_id=run_id,
                )
            baseline = _dirty_paths(workspace)

    feedback = ""
    status = "exhausted"

    while st.iteration < limit:
        st.iteration += 1
        emit(ev.ITERATION_STARTED, iteration=st.iteration)

        beat(PHASE_GATHERING_CONTEXT)
        if spec.context:
            emit(ev.CONTEXT_STARTED, iteration=st.iteration)
        context_values, context_errors = _gather_context(
            spec.context, workspace, spec.limits.command_timeout
        )
        if context_errors:
            emit(ev.CONTEXT_FAILED, iteration=st.iteration, errors=context_errors)
        elif spec.context:
            emit(ev.CONTEXT_COMPLETED, iteration=st.iteration)

        prompt = render_template(
            spec.prompt,
            {
                "objective": spec.objective,
                "feedback": feedback,
                "iteration": str(st.iteration),
                **context_values,
            },
        )

        beat(PHASE_RUNNING_AGENT)
        emit(ev.AGENT_STARTED, iteration=st.iteration)
        agent_result = adapter.run(
            prompt,
            workspace=workspace,
            timeout=spec.limits.command_timeout,
            iteration=st.iteration,
            objective=spec.objective,
        )
        emit(
            ev.AGENT_COMPLETED,
            iteration=st.iteration,
            exit_code=agent_result.exit_code,
            timed_out=agent_result.timed_out,
        )

        # Blast-radius gate: inspect what the agent touched BEFORE verifying.
        agent_changed: Optional[List[str]] = None
        if gate_active:
            beat(PHASE_CHECKING_BLAST_RADIUS)
            emit(ev.BLAST_RADIUS_STARTED, iteration=st.iteration)
            agent_changed = sorted(_dirty_paths(workspace) - baseline)
            blast = evaluate_changes(policy, agent_changed)
            if not blast.ok:
                feedback = "blast_radius_violation: " + blast.reason
                st.consecutive_failures += 1
                emit(
                    ev.BLAST_RADIUS_VIOLATION,
                    iteration=st.iteration,
                    agent_exit=agent_result.exit_code,
                    reason=_truncate(blast.reason, 160),
                    violations=blast.violations,
                    changed_paths=blast.changed_paths,
                )
                emit(ev.ITERATION_FAILED, iteration=st.iteration, reason="blast_radius_violation")
                beat(PHASE_WRITING_LEDGER)
                record = {
                    "event": "iteration",
                    "type": ev.ITERATION_FAILED,
                    "run_id": run_id,
                    "iteration": st.iteration,
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
                    "consecutive_failures": st.consecutive_failures,
                    "feedback": _truncate(feedback),
                }
                if context_errors:
                    record["context_errors"] = context_errors
                ledger.append(record)
                if st.consecutive_failures >= spec.limits.max_consecutive_failures:
                    status = "blocked"
                    break
                continue
            emit(ev.BLAST_RADIUS_PASSED, iteration=st.iteration, changed_paths=agent_changed)

        beat(PHASE_VERIFYING)
        emit(ev.VERIFY_STARTED, iteration=st.iteration)
        verify_result = run_proc(
            verify_command,
            cwd=workspace,
            timeout=spec.limits.command_timeout,
            stdin_text=prompt,
        )
        passed = verify_result.ok
        feedback = verify_result.feedback
        st.consecutive_failures = 0 if passed else st.consecutive_failures + 1

        if passed:
            emit(ev.VERIFY_PASSED, iteration=st.iteration, agent_exit=agent_result.exit_code)
        else:
            emit(
                ev.VERIFY_FAILED,
                iteration=st.iteration,
                agent_exit=agent_result.exit_code,
                feedback=_truncate(feedback, 160),
            )
            emit(ev.ITERATION_FAILED, iteration=st.iteration, reason="verify_failed")

        beat(PHASE_WRITING_LEDGER)
        record = {
            "event": "iteration",
            "type": "iteration_passed" if passed else ev.ITERATION_FAILED,
            "run_id": run_id,
            "iteration": st.iteration,
            "agent_exit": agent_result.exit_code,
            "agent_ms": agent_result.duration_ms,
            "agent_timed_out": agent_result.timed_out,
            "verify_exit": verify_result.exit_code,
            "verify_ms": verify_result.duration_ms,
            "verify_timed_out": verify_result.timed_out,
            "result": "pass" if passed else "fail",
            "consecutive_failures": st.consecutive_failures,
            "feedback": _truncate(feedback),
        }
        if context_errors:
            record["context_errors"] = context_errors
        if gate_active:
            record["blast_radius"] = {"ok": True, "changed_paths": agent_changed}
        ledger.append(record)

        if passed:
            status = "success"
            break
        if st.consecutive_failures >= spec.limits.max_consecutive_failures:
            status = "blocked"
            break
    else:
        status = "exhausted"

    terminal_type = {"success": ev.RUN_COMPLETED, "blocked": ev.RUN_BLOCKED}.get(status, ev.RUN_FAILED)
    terminal_phase = {"success": PHASE_COMPLETED, "blocked": PHASE_BLOCKED}.get(status, PHASE_FAILED)
    ledger.append(
        {
            "event": "run_end",
            "type": terminal_type,
            "run_id": run_id,
            "status": status,
            "iterations": st.iteration,
        }
    )
    emit(
        terminal_type,
        status=status,
        iterations=st.iteration,
        consecutive_failures=st.consecutive_failures,
        limit=limit,
    )
    beat(terminal_phase)
    return LoopResult(
        status=status,
        iterations=st.iteration,
        passed=(status == "success"),
        ledger_path=ledger.path,
        run_id=run_id,
    )
