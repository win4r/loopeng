"""Lifecycle hooks / connectors: shell out on loop events.

A loop spec may carry an optional ``hooks:`` block mapping lifecycle moments to
lists of shell commands. loopeng fires them by *observing the runner's event
stream* — so hooks compose on the existing ``on_event`` callback and need no
change to ``run_loop``'s core. A failing or slow hook is isolated (logged to the
event sink, bounded by a timeout); it never changes the loop's outcome.

    hooks:
      on_start:     ["echo started $LOOPENG_RUN_ID"]
      on_iteration: ["./record.sh"]
      on_success:   ["curl -fsS -X POST https://example/done"]
      on_failure:   ["./alert.sh"]

Each hook command runs through ``sh -lc`` with these environment variables set:
``LOOPENG_EVENT`` (event type), ``LOOPENG_STATUS`` (terminal status, if any),
``LOOPENG_RUN_ID``, ``LOOPENG_ITERATION`` (if any), and ``LOOPENG_EVENT_JSON``
(the full event as JSON).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import events as ev

HOOK_KEYS = ("on_start", "on_iteration", "on_success", "on_failure")


@dataclass
class HooksSpec:
    on_start: List[str] = field(default_factory=list)
    on_iteration: List[str] = field(default_factory=list)
    on_success: List[str] = field(default_factory=list)
    on_failure: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.on_start or self.on_iteration or self.on_success or self.on_failure)

    def commands_for(self, key: str) -> List[str]:
        return list(getattr(self, key, []) or [])


def parse_hooks(raw) -> Optional[HooksSpec]:
    """Validate a ``hooks:`` mapping into a HooksSpec (or None if absent)."""
    from .errors import SpecError

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError("hooks must be a mapping of on_start/on_iteration/on_success/on_failure")
    unknown = set(raw) - set(HOOK_KEYS)
    if unknown:
        raise SpecError(
            f"hooks: unknown key(s) {', '.join(sorted(unknown))}; allowed: {', '.join(HOOK_KEYS)}"
        )
    kwargs: Dict[str, List[str]] = {}
    for key in HOOK_KEYS:
        value = raw.get(key)
        if value is None:
            kwargs[key] = []
            continue
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
            raise SpecError(f"hooks.{key} must be a string or a list of shell-command strings")
        kwargs[key] = list(value)
    spec = HooksSpec(**kwargs)
    return None if spec.is_empty() else spec


# Which hook key(s) a given event triggers. Terminal events carry a `status`.
_TERMINAL = {ev.RUN_COMPLETED, ev.RUN_BLOCKED, ev.RUN_FAILED}


def hook_key_for_event(event: dict) -> Optional[str]:
    etype = event.get("type")
    if etype == ev.RUN_STARTED:
        return "on_start"
    if etype == ev.ITERATION_STARTED:
        return "on_iteration"
    if etype in _TERMINAL:
        return "on_success" if event.get("status", "success") == "success" else "on_failure"
    return None


class HookSink:
    """An ``on_event`` callable that fires the matching hook commands.

    Designed to be composed alongside the normal printer (see ``compose_sinks``).
    A failing/timed-out hook is reported back through ``report`` (defaulting to no-op)
    as a synthetic ``hook_failed`` / ``hook_timed_out`` / ``hook_error`` event — surfaced
    verbatim under ``--json`` and rendered as a ``⚠`` line in text mode — never aborting
    the loop. (The CLI wires ``report`` to the live event printer, not the run ledger.)
    """

    def __init__(
        self,
        hooks: HooksSpec,
        *,
        workspace=".",
        timeout: int = 30,
        report: Optional[Callable[[dict], None]] = None,
        runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
    ):
        self.hooks = hooks
        self.workspace = str(workspace)
        self.timeout = timeout
        self.report = report or (lambda _e: None)
        self.runner = runner

    def __call__(self, event: dict) -> None:
        key = hook_key_for_event(event)
        if key is None:
            return
        commands = self.hooks.commands_for(key)
        if not commands:
            return
        env = dict(os.environ)
        env["LOOPENG_EVENT"] = str(event.get("type", ""))
        env["LOOPENG_RUN_ID"] = str(event.get("run_id", ""))
        env["LOOPENG_EVENT_JSON"] = json.dumps(event, ensure_ascii=False)
        if event.get("status") is not None:
            env["LOOPENG_STATUS"] = str(event["status"])
        if event.get("iteration") is not None:
            env["LOOPENG_ITERATION"] = str(event["iteration"])
        for command in commands:
            self._fire(key, command, env)

    def _fire(self, key: str, command: str, env: Dict[str, str]) -> None:
        try:
            proc = self.runner(
                ["sh", "-lc", command],
                cwd=self.workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            self.report({"type": "hook_timed_out", "hook": key, "command": command})
            return
        except OSError as exc:  # shell missing, etc. — never fatal
            self.report({"type": "hook_error", "hook": key, "command": command, "error": str(exc)})
            return
        if proc.returncode != 0:
            self.report(
                {
                    "type": "hook_failed",
                    "hook": key,
                    "command": command,
                    "exit_code": proc.returncode,
                    "stderr": (proc.stderr or "")[-200:],
                }
            )


def compose_sinks(*sinks: Optional[Callable[[dict], None]]) -> Callable[[dict], None]:
    """Fan one event out to several sinks; a sink that raises never blocks the others."""
    active = [s for s in sinks if s is not None]

    def _emit(event: dict) -> None:
        for sink in active:
            try:
                sink(event)
            except Exception:  # a buggy sink must not abort the loop
                pass

    return _emit
