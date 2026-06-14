"""Multi-stage / multi-agent orchestration: a DAG of loopeng loops.

A *plan* (``plan.yaml``) wires several loops into a dependency graph. Each stage
resolves to a full :class:`~loopeng.spec.LoopSpec` and is executed by
:func:`~loopeng.runner.run_loop` — so a stage is itself a complete act→verify→
feed-back loop, not a single command. Stages with no unmet dependency run
concurrently; a stage runs only after every stage it ``needs`` has succeeded.

Plan schema::

    version: 1
    workspace: shared | worktree      # default: shared
    fail_fast: true | false           # default: true
    stages:
      <name>:
        needs: [<other stage names>]  # optional, default: []
        # exactly ONE source of the loop spec:
        spec: path/to/loop.yaml                      # load_spec
        # --- or ---
        skill: <skill name>                          # load_skill + render_to_spec
        set: {k: v}                                  # optional render params
        # --- or ---
        loop: {objective: ..., agent: ..., prompt: ..., verify: ..., limits: ...}

Execution model:
  * stages are topologically sorted into *levels* (batches with no intra-batch
    edges); a level runs concurrently via a thread pool, levels run in order;
  * a stage is "success" iff its ``LoopResult.passed`` is True, else "failed";
  * a stage whose *any* dependency failed (or was skipped) is "skipped" — never run;
  * with ``fail_fast`` (default), the first failure skips all not-yet-run stages;
  * a level containing any stage that uses the blast-radius gate runs SERIALLY: the
    gate reads tree-wide ``git status`` with no per-process attribution, so concurrent
    stages in a shared work tree would see one another's writes (false violations /
    wrong changed_paths). Serial execution makes each gated stage's baseline capture
    prior writes, so its change-set is attributed correctly. Ungated levels stay parallel.

Every stage start/end is appended to an orchestration ledger at
``<project_dir>/.loopeng/orchestrate-<run_id>.jsonl`` (one JSON object per line).
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional

from .errors import OrchestrationError
from .runner import STATE_DIR, run_loop
from .skills import load_skill, render_to_spec
from .spec import LoopSpec, load_spec, parse_spec


@dataclass
class StageResult:
    """The outcome of a single stage (one full loopeng loop, or a skip)."""

    name: str
    status: str  # "success" | "failed" | "skipped"
    passed: bool = False
    loop_status: str = ""  # the underlying LoopResult.status, "" if not run
    error: Optional[str] = None  # message if the stage raised before/while running


@dataclass
class OrchestrationResult:
    plan_path: str
    stages: List[StageResult] = field(default_factory=list)
    workspace_mode: str = "shared"
    worktree_branch: Optional[str] = None  # set in worktree mode
    worktree_diff: str = ""  # the isolated checkout's diff, surfaced to the user
    worktree_kept: bool = False  # branch preserved (plan succeeded with changes)

    @property
    def exit_code(self) -> int:
        """1 if ANY stage failed, else 0. A skipped stage is not itself a failure."""
        return 1 if any(s.status == "failed" for s in self.stages) else 0


# ---------------------------------------------------------------------------
# Plan parsing + stage spec resolution
# ---------------------------------------------------------------------------


def _load_plan(plan_path) -> dict:
    """Read + minimally validate plan.yaml into a plain dict."""
    path = Path(plan_path)
    if not path.exists():
        raise OrchestrationError(f"plan file not found: {path}")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise OrchestrationError(
            "PyYAML is required to parse plan.yaml — install it with `pip install pyyaml`"
        ) from exc
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OrchestrationError(f"could not read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise OrchestrationError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OrchestrationError(f"{path}: the top level must be a mapping")

    version = data.get("version")
    # Require the integer 1 specifically: bool (True==1), float 1.0, and "1" must all
    # be rejected so a typo'd version can't silently pass.
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise OrchestrationError(f"plan version must be the integer 1, got {version!r}")
    fail_fast = data.get("fail_fast", True)
    if not isinstance(fail_fast, bool):
        raise OrchestrationError(f"plan fail_fast must be true or false, got {fail_fast!r}")
    return data


def _parse_stages(data: dict) -> Dict[str, dict]:
    """Pull the ``stages`` mapping out of a plan, validating its shape.

    Each stage's ``needs`` is normalized to a list[str] and every referenced
    dependency is required to be a declared stage (a dangling edge is an error,
    not a silently-skipped stage).
    """
    stages_raw = data.get("stages")
    if not isinstance(stages_raw, dict) or not stages_raw:
        raise OrchestrationError("plan must define a non-empty `stages` mapping")
    stages: Dict[str, dict] = {}
    for name, body in stages_raw.items():
        key = str(name)
        if not isinstance(body, dict):
            raise OrchestrationError(f"stage {key!r} must be a mapping")
        needs = body.get("needs", []) or []
        if not (isinstance(needs, list) and all(isinstance(n, str) for n in needs)):
            raise OrchestrationError(f"stage {key!r}: `needs` must be a list of stage names")
        # Structural check at PARSE time (not inside the worker), so a malformed plan is
        # a "bad plan" -> exit 2, distinct from a stage whose loop fails to converge
        # (exit 1) or whose spec/skill file is missing at run time (a stage failure).
        sources = [k for k in ("spec", "skill", "loop") if k in body]
        if len(sources) != 1:
            raise OrchestrationError(
                f"stage {key!r} must have exactly one of spec/skill/loop, got {sources or 'none'}"
            )
        stages[key] = {**body, "needs": list(needs)}
    for name, body in stages.items():
        for dep in body["needs"]:
            if dep not in stages:
                raise OrchestrationError(
                    f"stage {name!r} needs unknown stage {dep!r}"
                )
        if name in body["needs"]:
            raise OrchestrationError(f"stage {name!r} cannot depend on itself")
    return stages


def _resolve_stage_spec(name: str, body: dict, project_dir) -> LoopSpec:
    """Resolve a stage body to a validated LoopSpec via exactly one source.

    spec:  -> load_spec(path)
    skill: -> load_skill(name, project_dir) + render_to_spec(skill, set)
    loop:  -> parse_spec(inline_dict)
    """
    sources = [k for k in ("spec", "skill", "loop") if k in body]
    if len(sources) != 1:
        raise OrchestrationError(
            f"stage {name!r} must have exactly one of spec/skill/loop, got {sources or 'none'}"
        )
    source = sources[0]

    if source == "spec":
        spec_path = body["spec"]
        if not isinstance(spec_path, str) or not spec_path.strip():
            raise OrchestrationError(f"stage {name!r}: `spec` must be a path string")
        # Resolve a relative spec path against the project dir, matching `loopeng run`.
        resolved = Path(spec_path)
        if not resolved.is_absolute():
            resolved = Path(project_dir) / resolved
        return load_spec(resolved)

    if source == "skill":
        skill_name = body["skill"]
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise OrchestrationError(f"stage {name!r}: `skill` must be a skill name")
        values_raw = body.get("set", {}) or {}
        if not isinstance(values_raw, dict):
            raise OrchestrationError(f"stage {name!r}: `set` must be a mapping of key -> value")
        # render_to_spec substitutes only declared params; values are stringified
        # so a YAML int/bool under `set:` matches the string-keyed renderer.
        values = {str(k): str(v) for k, v in values_raw.items()}
        skill = load_skill(skill_name, project_dir)
        spec, _rendered = render_to_spec(skill, values, source=f"plan-stage:{name}")
        return spec

    # source == "loop": an inline LoopSpec mapping.
    inline = body["loop"]
    if not isinstance(inline, dict):
        raise OrchestrationError(f"stage {name!r}: `loop` must be a mapping of LoopSpec fields")
    return parse_spec(inline, source=f"plan-stage:{name}")


# ---------------------------------------------------------------------------
# Topological sort into parallel levels (Kahn)
# ---------------------------------------------------------------------------


def build_levels(stages: Dict[str, dict]) -> List[List[str]]:
    """Kahn topological sort into batches that can run concurrently.

    Each returned level is a list of stage names whose dependencies are all
    satisfied by earlier levels; names within a level are sorted for determinism.
    Raises ``OrchestrationError('cycle detected')`` if not every node resolves
    (i.e. the graph has a cycle).
    """
    # indegree = number of unmet dependencies; deps[name] = its prerequisites.
    deps: Dict[str, set] = {name: set(body["needs"]) for name, body in stages.items()}
    indegree: Dict[str, int] = {name: len(d) for name, d in deps.items()}

    levels: List[List[str]] = []
    resolved: set = set()
    remaining = set(stages)
    while remaining:
        ready = sorted(name for name in remaining if indegree[name] == 0)
        if not ready:
            offenders = ", ".join(sorted(remaining))
            raise OrchestrationError(f"cycle detected among stages: {offenders}")
        levels.append(ready)
        for name in ready:
            remaining.discard(name)
            resolved.add(name)
        # Decrement indegree of every still-pending stage that depended on a
        # just-resolved one (recompute against `resolved` so each edge counts once).
        for name in remaining:
            indegree[name] = len(deps[name] - resolved)
    return levels


# ---------------------------------------------------------------------------
# Orchestration ledger
# ---------------------------------------------------------------------------


class _OrchestrationLedger:
    """Thread-safe append-only JSONL ledger for stage start/end records."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, **record) -> None:
        line = {"ts": time.time(), **record}
        text = json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n"
        with self._lock:  # serialize concurrent writers from the thread pool
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text)


def _new_run_id() -> str:
    """A unique-per-process orchestration run id; pid keeps siblings distinct."""
    return f"{int(time.time() * 1000):x}-{os.getpid():x}"


def _safe_name(value: str) -> str:
    """Make a stage/run id safe to use as a single path segment."""
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in value)


# ---------------------------------------------------------------------------
# Orchestrate
# ---------------------------------------------------------------------------


def orchestrate(
    plan_path,
    *,
    project_dir=".",
    fail_fast: Optional[bool] = None,
    workers: int = 4,
    run_id: Optional[str] = None,
) -> OrchestrationResult:
    """Run a plan DAG: resolve each stage to a LoopSpec and run it via run_loop.

    Levels run in order; stages within a level run concurrently in a thread pool.
    A stage whose any dependency failed (or was skipped) is "skipped", not run.
    If ``fail_fast`` (plan default, overridable here) and a stage fails, all
    not-yet-run stages are skipped.
    """
    project_dir = Path(project_dir)
    plan = _load_plan(plan_path)
    stages = _parse_stages(plan)
    levels = build_levels(stages)  # also raises on a cycle

    plan_fail_fast = bool(plan.get("fail_fast", True))
    effective_fail_fast = plan_fail_fast if fail_fast is None else bool(fail_fast)
    workspace_mode = str(plan.get("workspace", "shared"))
    if workspace_mode not in ("shared", "worktree"):
        raise OrchestrationError(
            f"plan workspace must be 'shared' or 'worktree', got {workspace_mode!r}"
        )

    run_id = run_id or _new_run_id()
    ledger = _OrchestrationLedger(project_dir / STATE_DIR / f"orchestrate-{run_id}.jsonl")
    ledger.append(
        stage="<plan>",
        event="orchestration_start",
        status="running",
        run_id=run_id,
        workspace=workspace_mode,
        fail_fast=effective_fail_fast,
        levels=levels,
    )

    # Execution base. In "worktree" mode the WHOLE plan runs inside ONE isolated
    # checkout off HEAD — the user's main tree is never touched, yet stages still
    # share files with one another. In "shared" mode stages run in project_dir.
    # (Spec/skill resolution always reads from project_dir, where the — possibly
    # uncommitted — plan, specs, and skills live; only execution moves.)
    exec_dir = project_dir
    wt_handle = None  # (worktree_module, repo_root, wt_path, branch) when isolating
    if workspace_mode == "worktree":
        from . import worktree as _wt

        repo = _wt.repo_root(project_dir)
        wt_path, wt_branch = _wt.create_isolated_worktree(repo)
        exec_dir = wt_path
        wt_handle = (_wt, repo, wt_path, wt_branch)
        ledger.append(
            stage="<plan>", event="worktree_created", status="running",
            run_id=run_id, worktree=str(wt_path), branch=wt_branch,
        )

    results: Dict[str, StageResult] = {}
    aborted = {"flag": False}  # set once a fail_fast trip happens (skip the rest)

    def _dep_failed(name: str) -> bool:
        """A dependency that did not succeed (failed or skipped) blocks this stage."""
        for dep in stages[name]["needs"]:
            prior = results.get(dep)
            if prior is None or prior.status != "success":
                return True
        return False

    def _run_stage(name: str, spec: "LoopSpec") -> StageResult:
        stage_run_id = f"{run_id}.{name}"
        # Stages within a level run concurrently but agents/verifiers must see one
        # another's files. run_loop derives BOTH its state dir (<base>/.loopeng) and
        # the work tree (<base>/workspace) from its project_dir, and writes a single
        # heartbeat.json via temp+rename — so two concurrent runs sharing one base
        # race on that .tmp file. We give each stage a PRIVATE state dir
        # (<exec_dir>/.loopeng/stages/<id>) while pinning its workspace to the
        # ABSOLUTE shared work tree, so agents/verify run in the common directory but
        # ledgers/heartbeats never collide. exec_dir is the worktree in isolated mode.
        shared_work = (exec_dir / spec.workspace).resolve()
        stage_dir = exec_dir / STATE_DIR / "stages" / _safe_name(stage_run_id)
        stage_dir.mkdir(parents=True, exist_ok=True)
        run_spec = replace(spec, workspace=str(shared_work))

        ledger.append(
            stage=name, event="stage_start", status="running",
            run_id=run_id, workspace=str(shared_work),
        )
        try:
            loop_result = run_loop(run_spec, stage_dir, run_id=stage_run_id)
        except Exception as exc:  # noqa: BLE001 - a runner crash fails only this stage
            ledger.append(
                stage=name, event="stage_end", status="failed", run_id=run_id, error=str(exc)
            )
            return StageResult(name=name, status="failed", error=str(exc))

        # A run SUCCEEDED iff result.passed is True (never inferred otherwise).
        status = "success" if loop_result.passed else "failed"
        ledger.append(
            stage=name, event="stage_end", status=status, run_id=run_id,
            loop_status=loop_result.status, iterations=loop_result.iterations,
            passed=loop_result.passed,
        )
        return StageResult(
            name=name,
            status=status,
            passed=loop_result.passed,
            loop_status=loop_result.status,
        )

    for level in levels:
        # Partition this level: stages to actually run vs. ones forced to skip
        # (a failed/skipped dependency, or a fail_fast abort already tripped).
        # Specs are resolved here (not inside the worker) so we can decide the
        # level's concurrency from the resolved blast-radius policy; a bad spec
        # fails just that stage.
        to_run: List[tuple] = []  # (name, resolved LoopSpec)
        for name in level:
            if aborted["flag"] or _dep_failed(name):
                results[name] = StageResult(name=name, status="skipped")
                ledger.append(stage=name, event="stage_end", status="skipped", run_id=run_id)
                continue
            try:
                spec = _resolve_stage_spec(name, stages[name], project_dir)
            except Exception as exc:  # noqa: BLE001 - a bad stage spec fails only that stage
                ledger.append(
                    stage=name, event="stage_end", status="failed", run_id=run_id, error=str(exc)
                )
                results[name] = StageResult(name=name, status="failed", error=str(exc))
                continue
            to_run.append((name, spec))

        if to_run:
            # The blast-radius gate reads tree-wide `git status` with no per-process
            # attribution, so concurrent stages sharing one work tree would each see
            # the others' writes (false violations + wrong changed_paths). If ANY stage
            # in this level uses the gate, run the level SERIALLY: each stage's baseline
            # then captures prior stages' writes, so its agent_changed delta is correct.
            gated = any(spec.blast_radius.active for _name, spec in to_run)
            max_workers = 1 if gated else max(1, min(workers, len(to_run)))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for stage_result in pool.map(lambda ns: _run_stage(*ns), to_run):
                    results[stage_result.name] = stage_result

            if effective_fail_fast and any(
                results[name].status == "failed" for name, _spec in to_run
            ):
                aborted["flag"] = True  # subsequent levels' stages all skip

    ordered = [results[name] for level in levels for name in level]
    overall = "failed" if any(s.status == "failed" for s in ordered) else "success"

    wt_branch: Optional[str] = None
    wt_diff = ""
    wt_kept = False
    if wt_handle is not None:
        _wt, repo, wt_path, wt_branch = wt_handle
        try:
            # Capture the agents' (usually uncommitted) edits onto the disposable
            # branch so the result is durable and the worktree dir can be removed.
            committed = _wt.commit_all(wt_path, f"loopeng orchestrate {run_id}")
            wt_kept = overall == "success" and bool(committed)
            wt_diff = _wt.surface_diff(repo, wt_branch) if committed else ""
        except Exception as exc:  # noqa: BLE001 - finalize must not mask the result
            ledger.append(
                stage="<plan>", event="worktree_error", status="error",
                run_id=run_id, error=str(exc),
            )
        try:
            _wt.remove_worktree(repo, wt_path, wt_branch, keep_branch=wt_kept)
        except Exception:  # noqa: BLE001
            pass
        ledger.append(
            stage="<plan>", event="worktree_finalized", status="ok",
            run_id=run_id, branch=wt_branch, kept=wt_kept,
        )

    ledger.append(
        stage="<plan>", event="orchestration_end", status=overall, run_id=run_id,
    )
    return OrchestrationResult(
        plan_path=str(plan_path),
        stages=ordered,
        workspace_mode=workspace_mode,
        worktree_branch=wt_branch,
        worktree_diff=wt_diff,
        worktree_kept=wt_kept,
    )
