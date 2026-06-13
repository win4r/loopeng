"""Subprocess execution with timeouts — the one place we shell out.

Every external command (agent, verifier, context gatherer) goes through
``run_proc`` so that timeouts, missing binaries, and duration accounting are
handled uniformly and turned into data the loop can reason about.
"""

from __future__ import annotations

import os
import select
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
    stalled: bool = False  # killed for producing no output within no_output_timeout
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
    no_output_timeout: Optional[float] = None,
) -> ProcResult:
    """Run ``command`` and always return a ProcResult (never raises on child failure).

    When ``no_output_timeout`` is set, the child is killed if it produces no output
    for that many seconds (a silent-hang guard, distinct from the overall ``timeout``).
    """
    if no_output_timeout:
        return _run_with_inactivity_timeout(
            command, cwd=cwd, env=env, timeout=timeout, stdin_text=stdin_text,
            no_output_timeout=no_output_timeout,
        )
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


def _run_with_inactivity_timeout(
    command, *, cwd, env, timeout, stdin_text, no_output_timeout
) -> ProcResult:
    """run_proc variant that also kills a child that goes silent (POSIX; uses select)."""
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return ProcResult("", f"command not found: {exc}", EXIT_NOTFOUND, int((time.monotonic() - start) * 1000))
    except PermissionError as exc:
        return ProcResult("", f"command not executable: {exc}", EXIT_NOTEXEC, int((time.monotonic() - start) * 1000))

    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    buffers = {proc.stdout.fileno(): [], proc.stderr.fileno(): []}
    streams = [proc.stdout, proc.stderr]
    last_activity = time.monotonic()
    killed = None  # "timeout" | "stall"

    while streams:
        now = time.monotonic()
        waits = [no_output_timeout - (now - last_activity)]
        if timeout is not None:
            waits.append(timeout - (now - start))
        ready, _, _ = select.select(streams, [], [], max(0.0, min(waits)))
        now = time.monotonic()
        if ready:
            for stream in ready:
                chunk = os.read(stream.fileno(), 65536)
                if chunk:
                    buffers[stream.fileno()].append(chunk)
                    last_activity = now
                else:
                    streams.remove(stream)  # EOF
        elif timeout is not None and (now - start) >= timeout:
            killed = "timeout"
            break
        elif (now - last_activity) >= no_output_timeout:
            killed = "stall"
            break

    if killed:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for stream in list(streams):  # best-effort drain without blocking
            if select.select([stream], [], [], 0)[0]:
                try:
                    chunk = os.read(stream.fileno(), 65536)
                    if chunk:
                        buffers[stream.fileno()].append(chunk)
                except OSError:
                    pass
    else:
        proc.wait()

    duration = int((time.monotonic() - start) * 1000)
    stdout = b"".join(buffers[proc.stdout.fileno()]).decode("utf-8", "replace")
    stderr = b"".join(buffers[proc.stderr.fileno()]).decode("utf-8", "replace")
    if killed == "stall":
        note = f"STALLED: no output for {no_output_timeout}s"
        stderr = f"{stderr}\n{note}".strip() if stderr else note
        return ProcResult(stdout, stderr, EXIT_TIMEOUT, duration, timed_out=True, stalled=True)
    if killed == "timeout":
        note = f"TIMEOUT after {timeout}s"
        stderr = f"{stderr}\n{note}".strip() if stderr else note
        return ProcResult(stdout, stderr, EXIT_TIMEOUT, duration, timed_out=True)
    return ProcResult(stdout, stderr, proc.returncode, duration)
