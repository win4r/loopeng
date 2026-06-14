"""The ``loopeng`` command-line interface: ``init`` and ``run``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .adapters import build_adapter
from .errors import AdapterError, SpecError
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


def cmd_run(args) -> int:
    spec_path = Path(args.spec)
    try:
        spec = load_spec(spec_path)
    except (SpecError, AdapterError) as exc:
        print(f"spec error: {exc}", file=sys.stderr)
        return 2

    project_dir = spec_path.resolve().parent
    ledger_path = project_dir / STATE_DIR / "ledger.jsonl"
    sink = _json_printer if args.json else _printer

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

    try:
        result = run_loop(
            spec,
            project_dir,
            max_iterations=args.max_iterations,
            on_event=sink,
            resume=resume_decision,
            spec_path=str(spec_path.resolve()),
            reload_spec_path=str(spec_path.resolve()) if args.reload_spec else None,
        )
    except AdapterError as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        return 2

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
    doctor_parser.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
