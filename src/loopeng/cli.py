"""The ``loopeng`` command-line interface: ``init`` and ``run``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .errors import AdapterError, SpecError
from .runner import run_loop
from .spec import load_spec

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


def cmd_run(args) -> int:
    spec_path = Path(args.spec)
    try:
        spec = load_spec(spec_path)
    except (SpecError, AdapterError) as exc:
        print(f"spec error: {exc}", file=sys.stderr)
        return 2

    project_dir = spec_path.resolve().parent
    try:
        result = run_loop(
            spec, project_dir, max_iterations=args.max_iterations, on_event=print
        )
    except AdapterError as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        return 2

    print(
        f"\nstatus: {result.status} | iterations: {result.iterations} "
        f"| ledger: {result.ledger_path}"
    )
    # Exit codes are CI-friendly: 0 success, non-zero for every other outcome.
    return {"success": 0, "blocked": 3, "exhausted": 4, "precondition_failed": 5}.get(
        result.status, 1
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loopeng", description="Agent-agnostic Loop Engineering runner."
    )
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
    run_parser.set_defaults(func=cmd_run)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
