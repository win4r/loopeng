"""The ``loopeng`` command-line interface.

Subcommands: ``init``, ``run`` (with ``--skill`` / ``--isolate`` / ``--plugin``),
``status``, ``doctor``, ``skill`` (list/show), ``watch``, ``schedule``,
``orchestrate``, and ``mcp`` (stdio MCP server).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .adapters import _BUILDERS, build_adapter
from .errors import (
    AdapterError,
    LoopengError,
    OrchestrationError,
    PluginError,
    SkillError,
    SpecError,
    WorktreeError,
)
from .events import make_event
from .heartbeat import HEARTBEAT_FILENAME, is_stale, pid_alive, read_heartbeat
from .ledger import Ledger
from .resume import resolve_resume
from .runner import STATE_DIR, run_loop
from .spec import fingerprint, load_spec

_TERMINAL_PHASES = ("completed", "blocked", "failed")

LOOP_YAML = """# loop.yaml — a portable Loop Engineering spec (loopeng).
# The agent acts, a deterministic verifier gates the result, and the verifier's
# feedback flows back into the next prompt until it passes or a guardrail stops it.

objective: "Write DONE into output.txt"
workspace: "."

agent:
  type: shell                       # generic, fully-working adapter.
  command: ["python3", "samples/mock_agent.py"]
  # Swap for a real coding agent by changing `type` to claude-code or codex,
  # e.g.:
  #   type: claude-code             # preset -> `claude -p "<prompt>"`
  #   type: codex                   # preset -> `codex exec "<prompt>"`

# Prompt template. {{feedback}} carries the verifier's output from the previous
# iteration, so the agent can self-correct — the core loop-engineering idea.
prompt: |
  Objective: {{objective}}
  Iteration: {{iteration}}
  Verifier feedback from last attempt: {{feedback}}
  Make the verifier pass.

verify:
  command: ["python3", "samples/verify.py"]

limits:
  max_iterations: 5
  max_consecutive_failures: 2
  timeout_seconds: 60

  # --- Blast-radius controls: a repository write-set gate, NOT a sandbox. ---
  # These only take effect when the workspace is a git repository; otherwise the
  # gate is skipped with a warning. See the README "Safety model" section.
  max_changed_files: 10
  # Deny edits to dangerous paths (enforced when run inside a git repo).
  # Note: `.git/` internals are invisible to a git-status-based gate, so a
  # ".git/**" pattern here would be inert — protect those via a real sandbox.
  forbidden_paths:
    - ".env"
    - ".env.*"
    - "secrets/**"
    - "infra/prod/**"
    - "pyproject.toml"
    - "uv.lock"
  # Set true (inside a git repo) to require a clean tree before the run starts.
  # Left false here so `loopeng init && loopeng run` works in a fresh dir.
  require_clean_git: false
  # Restrict edits to an allowlist (recommended for real code projects). Left
  # commented so the sample's output.txt is not rejected; uncomment to enforce:
  # allowed_paths:
  #   - "src/**"
  #   - "tests/**"
  #   - "README.md"
"""

MOCK_AGENT = '''#!/usr/bin/env python3
"""Mock coding agent for the loopeng sample (stands in for Claude Code / Codex).

It reads the assembled prompt from stdin. On the first attempt there is no
verifier feedback, so it writes an incomplete result (WIP) and the verifier
fails. The failing verifier message is fed back into the prompt next iteration;
seeing that feedback, the agent corrects itself and writes DONE. This is a
deterministic fail -> feedback -> fix -> pass loop with no randomness.
"""
import pathlib
import sys

prompt = sys.stdin.read()
target = pathlib.Path("output.txt")
if "expected" in prompt.lower():        # verifier feedback present -> fix it
    target.write_text("DONE\\n")
    print("mock-agent: saw verifier feedback -> wrote DONE")
else:                                    # first attempt -> intentionally incomplete
    target.write_text("WIP\\n")
    print("mock-agent: first attempt -> wrote WIP")
'''

VERIFY = '''#!/usr/bin/env python3
"""Deterministic verifier — the load-bearing half of the loop.

Exit 0 (pass) only when output.txt contains DONE; otherwise exit 1 with a
feedback message naming what was expected, so the agent can self-correct.
"""
import pathlib
import sys

path = pathlib.Path("output.txt")
content = path.read_text() if path.exists() else ""
if "DONE" in content:
    print("verify: PASS — output.txt contains DONE")
    sys.exit(0)
print(f"verify: FAIL — expected 'DONE' in output.txt, got {content!r}")
sys.exit(1)
'''


def scaffold(target_dir, force: bool = False) -> List[Path]:
    """Create loop.yaml, the sample scripts, and the .loopeng/ state dir."""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    loop_path = target / "loop.yaml"
    if loop_path.exists() and not force:
        raise FileExistsError(f"{loop_path} already exists (use --force to overwrite)")

    samples = target / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    state = target / ".loopeng"
    state.mkdir(parents=True, exist_ok=True)

    loop_path.write_text(LOOP_YAML, encoding="utf-8")
    (samples / "mock_agent.py").write_text(MOCK_AGENT, encoding="utf-8")
    (samples / "verify.py").write_text(VERIFY, encoding="utf-8")

    return [loop_path, samples / "mock_agent.py", samples / "verify.py", state]


def cmd_init(args) -> int:
    try:
        created = scaffold(args.path, force=args.force)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Initialized loopeng project in {Path(args.path).resolve()}")
    for path in created:
        print(f"  created {path}")
    print("\nRun it with:  loopeng run")
    return 0


def _render_event(event: dict) -> Optional[str]:
    """Render a typed runner event as a human-readable line (or None to suppress)."""
    kind = event.get("type")
    if kind == "run_started":
        suffix = " (resumed)" if event.get("resumed") else ""
        return (
            f"▶ loop start — objective: {event.get('objective')!r} "
            f"(max {event.get('max_iterations')} iterations){suffix}"
        )
    if kind == "resume_loaded":
        return (
            f"↻ resumed run {event.get('run_id')} at iteration {event.get('start_iteration')} "
            f"(consecutive_failures={event.get('consecutive_failures')})"
        )
    if kind == "blast_radius_skipped":
        return (
            "⚠ blast-radius controls are configured but the workspace is not a "
            "git repository — skipping the write-set gate"
        )
    if kind == "verify_passed":
        return f"  [iter {event.get('iteration')}] agent exit={event.get('agent_exit')} | verify PASS"
    if kind == "verify_failed":
        return (
            f"  [iter {event.get('iteration')}] agent exit={event.get('agent_exit')} | "
            f"verify FAIL | {event.get('feedback', '')}"
        )
    if kind == "blast_radius_violation":
        return (
            f"  [iter {event.get('iteration')}] agent exit={event.get('agent_exit')} | "
            f"BLAST-RADIUS VIOLATION | {event.get('reason', '')}"
        )
    if kind == "run_completed":
        return f"✓ success in {event.get('iterations')} iteration(s)"
    if kind == "run_blocked":
        return (
            f"✗ blocked — {event.get('consecutive_failures')} consecutive failures "
            f"(limit {event.get('limit')})"
        )
    if kind == "run_failed":
        if event.get("status") in ("preflight_failed", "no_progress"):
            return None  # already surfaced by adapter_preflight_failed / no_progress_detected
        if event.get("status") == "precondition_failed":
            return f"✗ precondition failed — {event.get('reason', 'working tree not clean at loop start')}"
        return f"✗ exhausted — reached max {event.get('limit')} iterations without passing"
    if kind == "adapter_preflight_failed":
        return f"✗ adapter preflight failed — {event.get('reason')}"
    if kind == "no_progress_detected":
        return f"✗ no progress — {event.get('streak')} consecutive iterations with identical feedback"
    if kind == "prompt_steered":
        return f"↻ prompt steered from loop.yaml (iteration {event.get('iteration')})"
    if kind == "spec_reload_failed":
        return f"⚠ spec reload ignored (invalid mid-edit): {event.get('reason')}"
    if kind == "resume_refused":
        return f"✗ resume refused — {event.get('message') or event.get('reason')}"
    # Hook failures are observational (they never change the loop outcome) but the
    # user needs to see a connector that silently broke. These carry different fields.
    if kind == "hook_failed":
        return f"⚠ hook {event.get('hook')} failed (exit {event.get('exit_code')}): {event.get('command')}"
    if kind == "hook_timed_out":
        return f"⚠ hook {event.get('hook')} timed out: {event.get('command')}"
    if kind == "hook_error":
        return f"⚠ hook {event.get('hook')} error ({event.get('error')}): {event.get('command')}"
    return None


def _printer(event: dict) -> None:
    line = _render_event(event)
    if line is not None:
        print(line)


def _json_printer(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False))


def _refuse_resume(ledger_path, run_id: str, reason: str, message: str, sink) -> int:
    sink(make_event("resume_refused", run_id, reason=reason, message=message))
    if ledger_path.exists():
        Ledger(ledger_path).append(
            {"event": "resume_refused", "type": "resume_refused", "run_id": run_id, "reason": reason}
        )
    return 6  # resume refused


def _safe_segment(value: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in value)


def _load_plugins(args) -> Optional[int]:
    """Register entry-point + explicit ``--plugin`` adapters into the registry.

    Entry-point plugins are failure-isolated (a broken one is a stderr warning);
    an explicit ``--plugin`` that fails to import is a hard error (exit 2) because
    the user asked for it by name. Returns an exit code on a strict failure, else None.
    """
    from .plugins import load_entry_point_plugins, load_explicit_plugin

    for warning in load_entry_point_plugins(_BUILDERS):
        print(f"plugin warning: {warning}", file=sys.stderr)
    for module_spec in (getattr(args, "plugin", None) or []):
        try:
            load_explicit_plugin(module_spec, _BUILDERS)
        except PluginError as exc:
            print(f"plugin error: {exc}", file=sys.stderr)
            return 2
    return None


def _spec_from_skill(args):
    """Render a skill to a (spec, project_dir, spec_path), persisting the render."""
    from .skills import load_skill, parse_set_args, render_to_spec

    project_dir = Path(args.dir).resolve() if getattr(args, "dir", None) else Path.cwd()
    skill = load_skill(args.skill, project_dir)
    values = parse_set_args(getattr(args, "set", None))
    spec, rendered = render_to_spec(skill, values)
    state = project_dir / STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    spec_path = (state / f"skill-{_safe_segment(args.skill)}.rendered.yaml").resolve()
    spec_path.write_text(rendered, encoding="utf-8")
    return spec, project_dir, spec_path


def _finalize_worktree(handle, result, spec, *, json_mode: bool) -> None:
    """Commit the isolated worktree's edits onto its branch, surface the diff, clean up."""
    from .worktree import commit_all, remove_worktree, surface_diff

    root, wt_path, branch = handle
    passed = bool(result is not None and result.passed)
    kept = False
    diff = ""
    try:
        committed = commit_all(wt_path, f"loopeng: {spec.objective[:60]}") if passed else False
        kept = passed and committed
        diff = surface_diff(root, branch) if committed else ""
    except WorktreeError as exc:
        print(f"worktree finalize warning: {exc}", file=sys.stderr)
    if not json_mode:
        if kept and diff:
            print(f"\n--- isolated run changed files (branch {branch}) ---")
            print(diff, end="")
            print(f"merge with:  git merge {branch}")
        elif passed:
            print("\nisolated run passed but changed nothing; worktree discarded.")
        else:
            print("\nisolated run did not pass; worktree discarded.")
    try:
        remove_worktree(root, wt_path, branch, keep_branch=kept)
    except WorktreeError:
        pass


def cmd_run(args) -> int:
    plugin_rc = _load_plugins(args)
    if plugin_rc is not None:
        return plugin_rc

    using_skill = bool(getattr(args, "skill", None))
    if args.resume and (args.isolate or using_skill):
        # --isolate writes its ledger into a throwaway worktree that is then removed,
        # and --skill renders a fresh spec each time, so resuming either is impossible.
        print(
            "error: --resume cannot be combined with --isolate or --skill "
            "(their ledger/rendered state is ephemeral)",
            file=sys.stderr,
        )
        return 2
    try:
        if using_skill:
            spec, project_dir, spec_path = _spec_from_skill(args)
        else:
            spec_path = Path(args.spec).resolve()
            spec = load_spec(spec_path)
            project_dir = spec_path.parent
    except (SpecError, AdapterError, SkillError) as exc:
        print(f"spec error: {exc}", file=sys.stderr)
        return 2

    ledger_path = project_dir / STATE_DIR / "ledger.jsonl"
    base_sink = _json_printer if args.json else _printer
    sink = base_sink
    if spec.hooks is not None:
        from .hooks import HookSink, compose_sinks

        sink = compose_sinks(
            base_sink, HookSink(spec.hooks, workspace=project_dir, report=base_sink)
        )

    resume_decision = None
    if args.resume:
        # Don't start a second process under a still-live run (same run_id).
        heartbeat = read_heartbeat(project_dir / STATE_DIR / HEARTBEAT_FILENAME)
        if (
            heartbeat
            and not is_stale(heartbeat)
            and heartbeat.get("phase") not in _TERMINAL_PHASES
            and not args.force
        ):
            return _refuse_resume(
                ledger_path,
                heartbeat.get("run_id", ""),
                "run_in_progress",
                "a run appears to be in progress (live heartbeat); pass --force to resume anyway",
                sink,
            )
        decision = resolve_resume(ledger_path, fingerprint(spec), force=args.force)
        if not decision.resumable:
            return _refuse_resume(ledger_path, decision.run_id or "", decision.reason, decision.message, sink)
        resume_decision = decision

    # Optional isolation: run the loop in a throwaway git worktree off HEAD so the
    # user's main working tree is never touched. The diff is surfaced afterward and
    # the worktree removed (its branch kept on success). Requires a git repo.
    worktree_handle = None
    run_project_dir = project_dir
    if args.isolate:
        from .worktree import create_isolated_worktree, repo_root

        root = repo_root(project_dir)
        wt_path, branch = create_isolated_worktree(root)
        worktree_handle = (root, wt_path, branch)
        run_project_dir = wt_path

    result = None
    try:
        result = run_loop(
            spec,
            run_project_dir,
            max_iterations=args.max_iterations,
            on_event=sink,
            resume=resume_decision,
            spec_path=str(spec_path),
            reload_spec_path=str(spec_path) if (args.reload_spec and not using_skill) else None,
        )
    except AdapterError as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        return 2
    finally:
        if worktree_handle is not None:
            _finalize_worktree(worktree_handle, result, spec, json_mode=args.json)

    if not args.json:  # in --json mode stdout is a pure JSONL event stream
        print(
            f"\nstatus: {result.status} | iterations: {result.iterations} "
            f"| run: {result.run_id} | ledger: {result.ledger_path}"
        )
    # Exit codes are CI-friendly: 0 success, non-zero for every other outcome.
    # 2 spec/adapter error, 3 blocked, 4 exhausted, 5 precondition_failed,
    # 6 resume refused, 7 adapter preflight failed, 8 no progress.
    return {
        "success": 0,
        "blocked": 3,
        "exhausted": 4,
        "precondition_failed": 5,
        "preflight_failed": 7,
        "no_progress": 8,
    }.get(result.status, 1)


def cmd_status(args) -> int:
    state_dir = Path(args.dir) / STATE_DIR
    heartbeat = read_heartbeat(state_dir / HEARTBEAT_FILENAME)
    ledger_path = state_dir / "ledger.jsonl"
    records = Ledger(ledger_path).records() if ledger_path.exists() else []
    last_event = records[-1] if records else None

    adapter_preflight = None
    for record in records:
        if record.get("event") in ("adapter_preflight_passed", "adapter_preflight_failed"):
            adapter_preflight = {
                "ok": record.get("event") == "adapter_preflight_passed",
                "adapter_type": record.get("adapter_type"),
                "binary": record.get("binary"),
                "resolved_path": record.get("resolved_path"),
                "reason": record.get("reason", ""),
                "ts": record.get("ts"),
            }

    stale = is_stale(heartbeat)
    hb = heartbeat or {}
    report = {
        "heartbeat_present": heartbeat is not None,
        "stale": stale,
        "run_id": hb.get("run_id") or (last_event.get("run_id") if last_event else None),
        "phase": hb.get("phase"),
        "pid": hb.get("pid"),
        "pid_alive": pid_alive(hb.get("pid")) if heartbeat else False,
        "iteration": hb.get("iteration"),
        "max_iterations": hb.get("max_iterations"),
        "consecutive_failures": hb.get("consecutive_failures"),
        "started_at": hb.get("started_at"),
        "updated_at": hb.get("updated_at"),
        "spec_path": hb.get("spec_path"),
        "spec_fingerprint": hb.get("spec_fingerprint"),
        "cwd": hb.get("cwd"),
        "adapter_preflight": adapter_preflight,
        "last_event": last_event,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    elif heartbeat is None:
        print(f"no run state found under {state_dir}")
    else:
        print(
            f"run {report['run_id']}: phase={report['phase']} "
            f"iteration={report['iteration']}/{report['max_iterations']} "
            f"failures={report['consecutive_failures']} "
            f"{'STALE' if stale else 'live'} (updated {report['updated_at']})"
        )
    return 0


def cmd_doctor(args) -> int:
    plugin_rc = _load_plugins(args)
    if plugin_rc is not None:
        return plugin_rc
    spec_path = Path(args.spec)
    try:
        spec = load_spec(spec_path)
    except (SpecError, AdapterError) as exc:
        print(f"spec error: {exc}", file=sys.stderr)
        return 2
    try:
        adapter = build_adapter(spec.agent)
    except AdapterError as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        return 2

    workspace = (spec_path.resolve().parent / spec.workspace).resolve()
    pf = adapter.preflight(cwd=workspace)
    report = {
        "adapter_type": pf.adapter_type,
        "binary": pf.binary,
        "resolved_path": pf.resolved_path,
        "ok": pf.ok,
        "reason": pf.reason,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    elif pf.ok:
        print(f"adapter {pf.adapter_type!r}: OK — binary={pf.binary!r} resolved={pf.resolved_path!r}")
    else:
        print(f"adapter {pf.adapter_type!r}: NOT READY — {pf.reason}", file=sys.stderr)
    return 0 if pf.ok else 7


def cmd_skill(args) -> int:
    """List available skills, or show one rendered template + its parameters."""
    from .skills import discover_skills, load_skill

    project_dir = Path(args.dir)
    if args.action == "show":
        if not args.name:
            print("usage: loopeng skill show <name>", file=sys.stderr)
            return 2
        try:
            skill = load_skill(args.name, project_dir)
        except SkillError as exc:
            print(f"skill error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            params = {
                n: {"required": p.required, "default": p.default, "description": p.description}
                for n, p in skill.params.items()
            }
            print(json.dumps({"name": skill.name, "description": skill.description,
                              "source": skill.source, "params": params}, ensure_ascii=False))
        else:
            print(f"{skill.name} [{skill.source}] — {skill.description}")
            if skill.params:
                print("parameters:")
                for name, param in skill.params.items():
                    req = "required" if param.required else f"default={param.default!r}"
                    print(f"  {name} ({req}) — {param.description}")
            print("\ntemplate:\n" + skill.raw_text)
        return 0

    # default: list
    skills = discover_skills(project_dir)
    if args.json:
        print(json.dumps(
            [{"name": s.name, "description": s.description, "source": s.source}
             for s in skills.values()], ensure_ascii=False))
    elif not skills:
        print("no skills found (bundled, ~/.loopeng/skills/, or .loopeng/skills/)")
    else:
        for s in sorted(skills.values(), key=lambda s: s.name):
            print(f"{s.name} [{s.source}] — {s.description}")
    return 0


def cmd_watch(args) -> int:
    """Re-run the loop whenever watched files change (foreground, daemonless)."""
    from .triggers import DEFAULT_IGNORE_DIRS, watch

    run_args = [sys.executable, "-m", "loopeng", "run", "--spec", str(Path(args.spec).resolve())]
    if args.json:
        run_args.append("--json")
    ignore = tuple(DEFAULT_IGNORE_DIRS) + tuple(args.ignore or ())
    if not args.json:
        print(f"watching {args.pattern} -> loopeng run (Ctrl-C to stop)")
    return watch(
        args.pattern,
        run_args,
        poll_interval=args.poll_interval,
        debounce_quiet=args.debounce,
        ignore_dirs=ignore,
        run_on_start=args.run_on_start,
        max_runs=args.max_runs,
    )


def cmd_schedule(args) -> int:
    """Emit (or install with --apply) a crontab line that runs the loop periodically."""
    from .triggers import build_cron_entry, current_crontab, install_crontab, upsert_cron

    workdir = args.workdir or str(Path(args.spec).resolve().parent)
    run_args = [sys.executable, "-m", "loopeng", "run", "--spec", str(Path(args.spec).name)]
    entry = build_cron_entry(args.cron, run_args, marker=args.marker, workdir=workdir)
    merged = upsert_cron(current_crontab(), entry, args.marker)
    if args.apply:
        try:
            install_crontab(merged)
        except (OSError, subprocess.CalledProcessError) as exc:
            # FileNotFoundError (no crontab binary) or a rejected crontab (check=True).
            print(f"schedule error: {exc}", file=sys.stderr)
            return 2
        print(f"installed cron entry (# loopeng:{args.marker})")
    else:
        print(merged, end="" if merged.endswith("\n") else "\n")
        print(f"# dry-run — re-run with --apply to install via `crontab -`", file=sys.stderr)
    return 0


def cmd_orchestrate(args) -> int:
    """Run a multi-stage plan.yaml DAG (each stage is a full loopeng loop)."""
    plugin_rc = _load_plugins(args)
    if plugin_rc is not None:
        return plugin_rc
    from .orchestrator import orchestrate

    try:
        result = orchestrate(
            args.plan, project_dir=args.dir, fail_fast=args.fail_fast, workers=args.workers
        )
    except (OrchestrationError, SpecError, AdapterError, SkillError) as exc:
        print(f"orchestration error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        import dataclasses

        print(json.dumps({
            "plan_path": result.plan_path,
            "exit_code": result.exit_code,
            "workspace_mode": result.workspace_mode,
            "worktree_branch": result.worktree_branch,
            "worktree_kept": result.worktree_kept,
            "stages": [dataclasses.asdict(s) for s in result.stages],
        }, ensure_ascii=False))
    else:
        for stage in result.stages:
            extra = f" ({stage.loop_status})" if stage.loop_status else ""
            extra += f" — {stage.error}" if stage.error else ""
            print(f"  [{stage.status}] {stage.name}{extra}")
        if result.worktree_diff:
            print(f"\n--- isolated plan changed files (branch {result.worktree_branch}) ---")
            print(result.worktree_diff, end="")
            if result.worktree_kept:
                print(f"merge with:  git merge {result.worktree_branch}")
        print(f"\nplan {'succeeded' if result.exit_code == 0 else 'failed'} "
              f"({sum(s.status == 'success' for s in result.stages)}/{len(result.stages)} stages passed)")
    return result.exit_code


def cmd_mcp(args) -> int:
    """Run loopeng as an MCP server over stdio (for Claude Code / Codex)."""
    from .mcp_server import serve

    serve(project_dir=args.dir)  # blocks on stdin until EOF; stdout is the JSON-RPC channel
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loopeng", description="Agent-agnostic Loop Engineering runner."
    )
    parser.add_argument("--version", action="version", version=f"loopeng {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="scaffold a sample loop.yaml + .loopeng/")
    init_parser.add_argument(
        "--path", default=".", help="target directory (default: current directory)"
    )
    init_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing loop.yaml"
    )
    init_parser.set_defaults(func=cmd_init)

    run_parser = sub.add_parser("run", help="run the loop defined in loop.yaml")
    run_parser.add_argument(
        "--spec", default="loop.yaml", help="path to the loop spec (default: loop.yaml)"
    )
    run_parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="override limits.max_iterations for this run",
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="continue the latest unfinished run from the ledger",
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="with --resume: override a blocked run or a changed spec fingerprint",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="emit one typed JSON event per line to stdout (machine-readable stream)",
    )
    run_parser.add_argument(
        "--reload-spec",
        action="store_true",
        help="re-read loop.yaml before each iteration to pick up prompt edits (mid-run steering)",
    )
    run_parser.add_argument(
        "--skill",
        default=None,
        help="run a named reusable skill template instead of --spec (see `loopeng skill list`)",
    )
    run_parser.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        help="set a skill parameter (repeatable); only valid with --skill",
    )
    run_parser.add_argument(
        "--isolate",
        action="store_true",
        help="run in a throwaway git worktree off HEAD; your main tree is never touched",
    )
    run_parser.add_argument(
        "--plugin",
        action="append",
        metavar="MODULE_OR_PATH",
        help="load a custom adapter plugin (dotted module or .py path); repeatable",
    )
    run_parser.add_argument(
        "--dir", default=None, help="project directory for --skill resolution (default: cwd)"
    )
    run_parser.set_defaults(func=cmd_run)

    status_parser = sub.add_parser("status", help="report live run state from the heartbeat")
    status_parser.add_argument(
        "--dir", default=".", help="project directory containing .loopeng/ (default: .)"
    )
    status_parser.add_argument("--json", action="store_true", help="emit a single JSON object")
    status_parser.set_defaults(func=cmd_status)

    doctor_parser = sub.add_parser(
        "doctor", help="check the configured agent adapter's binary is available"
    )
    doctor_parser.add_argument(
        "--spec", default="loop.yaml", help="path to the loop spec (default: loop.yaml)"
    )
    doctor_parser.add_argument("--json", action="store_true", help="emit a single JSON object")
    doctor_parser.add_argument(
        "--plugin", action="append", metavar="MODULE_OR_PATH",
        help="load a custom adapter plugin before preflight (repeatable)",
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    # --- skill: list / show reusable templates (positional action keeps flag order free) ---
    skill_parser = sub.add_parser("skill", help="list or show reusable skill templates")
    skill_parser.add_argument(
        "action", nargs="?", choices=["list", "show"], default="list",
        help="'list' (default) or 'show <name>'",
    )
    skill_parser.add_argument("name", nargs="?", help="skill name (required for 'show')")
    skill_parser.add_argument("--dir", default=".", help="project dir for skill discovery (default: .)")
    skill_parser.add_argument("--json", action="store_true", help="emit JSON")
    skill_parser.set_defaults(func=cmd_skill)

    # --- watch: re-run on file changes (foreground, daemonless) ---
    watch_parser = sub.add_parser("watch", help="re-run the loop when watched files change")
    watch_parser.add_argument("--spec", default="loop.yaml", help="loop spec to run (default: loop.yaml)")
    watch_parser.add_argument(
        "--pattern", action="append", required=True, metavar="GLOB",
        help="glob to watch, e.g. 'src/**/*.py' (repeatable, required)",
    )
    watch_parser.add_argument("--poll-interval", type=float, default=0.5, dest="poll_interval")
    watch_parser.add_argument("--debounce", type=float, default=0.3, help="quiet seconds before firing")
    watch_parser.add_argument("--ignore", action="append", metavar="DIR", help="extra dir name to ignore")
    watch_parser.add_argument("--run-on-start", action="store_true", dest="run_on_start")
    watch_parser.add_argument("--max-runs", type=int, default=None, dest="max_runs")
    watch_parser.add_argument("--json", action="store_true", help="pass --json to each `run`")
    watch_parser.set_defaults(func=cmd_watch)

    # --- schedule: emit/install a crontab line (no daemon) ---
    schedule_parser = sub.add_parser("schedule", help="emit or install a cron line that runs the loop")
    schedule_parser.add_argument("--spec", default="loop.yaml", help="loop spec to run")
    schedule_parser.add_argument("--cron", required=True, help="5-field cron expression, e.g. '*/30 * * * *'")
    schedule_parser.add_argument("--marker", required=True, help="idempotency key (one line per marker)")
    schedule_parser.add_argument("--workdir", default=None, help="cwd for the cron job (default: spec's dir)")
    schedule_parser.add_argument("--apply", action="store_true", help="install via `crontab -` (default: dry-run)")
    schedule_parser.set_defaults(func=cmd_schedule)

    # --- orchestrate: multi-stage plan.yaml DAG ---
    orch_parser = sub.add_parser("orchestrate", help="run a multi-stage plan.yaml DAG")
    orch_parser.add_argument("--plan", default="plan.yaml", help="path to plan.yaml (default: plan.yaml)")
    orch_parser.add_argument("--dir", default=".", help="project directory (default: .)")
    orch_parser.add_argument("--workers", type=int, default=4, help="max concurrent stages per level")
    orch_parser.add_argument("--json", action="store_true", help="emit a JSON summary")
    orch_parser.add_argument("--plugin", action="append", metavar="MODULE_OR_PATH", help="load a custom adapter plugin")
    ff = orch_parser.add_mutually_exclusive_group()
    ff.add_argument("--fail-fast", dest="fail_fast", action="store_true", default=None,
                    help="stop after the first failed stage (overrides the plan)")
    ff.add_argument("--no-fail-fast", dest="fail_fast", action="store_false",
                    help="run independent stages even after a failure")
    orch_parser.set_defaults(func=cmd_orchestrate)

    # --- mcp: stdio MCP server for Claude Code / Codex ---
    mcp_parser = sub.add_parser("mcp", help="run loopeng as an MCP server over stdio")
    mcp_parser.add_argument("--dir", default=".", help="project dir to resolve skills/specs/status against")
    mcp_parser.set_defaults(func=cmd_mcp)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LoopengError as exc:
        # Safety net for typed errors a command didn't handle locally (e.g. a
        # WorktreeError from --isolate). Specific commands still map their own
        # exit codes; this only catches what would otherwise be a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
