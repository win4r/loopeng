"""Subprocess execution with timeouts — the one place we shell out.

Every external command (agent, verifier, context gatherer) goes through
``run_proc`` so that timeouts, missing binaries, and duration accounting are
handled uniformly and turned into data the loop can reason about.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

# Conventional exit codes for failures we synthesize (the child never returned them).
EXIT_TIMEOUT = 124  # matches coreutils `timeout`
EXIT_NOTEXEC = 126  # matches shell "command found but not executable"
EXIT_NOTFOUND = 127  # matches shell "command not found"


@dataclass
class ProcResult:
    """The agent-contract output shape: stdout / stderr / exit code / artifacts."""

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool = False
    # Reserved for a future iteration (e.g. files changed). Intentionally unused
    # in the MVP, but part of the documented adapter contract.
    artifacts: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def feedback(self) -> str:
        """Combined output, used as the verifier feedback fed back into the loop."""
        parts = [self.stdout.strip()]
        if self.stderr.strip():
            parts.append(self.stderr.strip())
        return "\n".join(p for p in parts if p)


def _coerce(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def run_proc(
    command: Sequence[str],
    *,
    cwd,
    env: Optional[dict] = None,
    timeout: Optional[float] = None,
    stdin_text: Optional[str] = None,
) -> ProcResult:
    """Run ``command`` and always return a ProcResult (never raises on child failure)."""
    start = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            env=env,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = int((time.monotonic() - start) * 1000)
        return ProcResult(
            stdout=_coerce(completed.stdout),
            stderr=_coerce(completed.stderr),
            exit_code=completed.returncode,
            duration_ms=duration,
        )
    except subprocess.TimeoutExpired as exc:
        duration = int((time.monotonic() - start) * 1000)
        stderr = _coerce(exc.stderr)
        note = f"TIMEOUT after {timeout}s"
        stderr = f"{stderr}\n{note}".strip() if stderr else note
        return ProcResult(
            stdout=_coerce(exc.stdout),
            stderr=stderr,
            exit_code=EXIT_TIMEOUT,
            duration_ms=duration,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        duration = int((time.monotonic() - start) * 1000)
        return ProcResult(
            stdout="",
            stderr=f"command not found: {exc}",
            exit_code=EXIT_NOTFOUND,
            duration_ms=duration,
        )
    except PermissionError as exc:
        duration = int((time.monotonic() - start) * 1000)
        return ProcResult(
            stdout="",
            stderr=f"command not executable: {exc}",
            exit_code=EXIT_NOTEXEC,
            duration_ms=duration,
        )
