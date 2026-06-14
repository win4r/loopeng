"""Typed loop events.

The runner emits these as plain dicts to its ``on_event`` callback. They are
JSON-serializable and ledger-compatible (same ``run_id`` + ``ts`` shape as ledger
records), so a subscriber can render them live, persist them, or both.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

# Lifecycle event types (the vocabulary the runner emits).
RUN_STARTED = "run_started"
CONTEXT_STARTED = "context_started"
CONTEXT_COMPLETED = "context_completed"
CONTEXT_FAILED = "context_failed"
ITERATION_STARTED = "iteration_started"
AGENT_STARTED = "agent_started"
AGENT_COMPLETED = "agent_completed"
BLAST_RADIUS_STARTED = "blast_radius_started"
BLAST_RADIUS_PASSED = "blast_radius_passed"
BLAST_RADIUS_VIOLATION = "blast_radius_violation"
VERIFY_STARTED = "verify_started"
VERIFY_PASSED = "verify_passed"
VERIFY_FAILED = "verify_failed"
ITERATION_FAILED = "iteration_failed"
RUN_COMPLETED = "run_completed"
RUN_BLOCKED = "run_blocked"
RUN_FAILED = "run_failed"
HEARTBEAT_WRITTEN = "heartbeat_written"
RESUME_STARTED = "resume_started"
RESUME_REFUSED = "resume_refused"
RESUME_LOADED = "resume_loaded"
BLAST_RADIUS_SKIPPED = "blast_radius_skipped"
ADAPTER_PREFLIGHT_PASSED = "adapter_preflight_passed"
ADAPTER_PREFLIGHT_FAILED = "adapter_preflight_failed"
NO_PROGRESS_DETECTED = "no_progress_detected"
PROMPT_STEERED = "prompt_steered"
SPEC_RELOAD_FAILED = "spec_reload_failed"

EVENT_TYPES = frozenset(
    {
        RUN_STARTED,
        CONTEXT_STARTED,
        CONTEXT_COMPLETED,
        CONTEXT_FAILED,
        ITERATION_STARTED,
        AGENT_STARTED,
        AGENT_COMPLETED,
        BLAST_RADIUS_STARTED,
        BLAST_RADIUS_PASSED,
        BLAST_RADIUS_VIOLATION,
        VERIFY_STARTED,
        VERIFY_PASSED,
        VERIFY_FAILED,
        ITERATION_FAILED,
        RUN_COMPLETED,
        RUN_BLOCKED,
        RUN_FAILED,
        HEARTBEAT_WRITTEN,
        RESUME_STARTED,
        RESUME_REFUSED,
        RESUME_LOADED,
        BLAST_RADIUS_SKIPPED,
        ADAPTER_PREFLIGHT_PASSED,
        ADAPTER_PREFLIGHT_FAILED,
        NO_PROGRESS_DETECTED,
        PROMPT_STEERED,
        SPEC_RELOAD_FAILED,
    }
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    """A sortable, human-debuggable, unique run id: <utc-compact>-<6 hex>."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)


def make_event(event_type: str, run_id: str, **fields) -> dict:
    return {"type": event_type, "run_id": run_id, "ts": utcnow_iso(), **fields}
