"""Resume resolution: reconstruct the latest run's state from the JSONL ledger.

The ledger is the source of truth. Each run's records carry a ``run_id``; the
latest run is the one whose ``run_started`` appears last. From its iteration
records we recover how far it got (iteration count, consecutive-failure count)
and whether it reached a terminal status, then decide whether resuming is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .ledger import Ledger

# Refusal reason codes (also emitted as the resume_refused event's `reason`).
NO_LEDGER = "no_ledger"
NO_RESUMABLE_RUN = "no_resumable_run"
ALREADY_SUCCEEDED = "already_succeeded"
BLOCKED_NOT_RESUMABLE = "blocked_not_resumable"
NO_PROGRESS_NOT_RESUMABLE = "no_progress_not_resumable"
FINGERPRINT_MISMATCH = "fingerprint_mismatch"
RUN_IN_PROGRESS = "run_in_progress"

REFUSAL_REASONS = {
    NO_LEDGER: "no ledger found to resume from",
    NO_RESUMABLE_RUN: "no resumable run found in the ledger",
    ALREADY_SUCCEEDED: "the latest run already completed successfully",
    BLOCKED_NOT_RESUMABLE: "the latest run ended 'blocked'; pass --force to resume it",
    NO_PROGRESS_NOT_RESUMABLE: "the latest run ended 'no_progress'; pass --force to resume it",
    FINGERPRINT_MISMATCH: "the spec changed since the latest run; pass --force to resume anyway",
    RUN_IN_PROGRESS: "a run appears to be in progress (live heartbeat); pass --force to resume anyway",
}


@dataclass
class ResumeDecision:
    resumable: bool
    reason: str = ""  # refusal code when not resumable
    run_id: str = ""
    start_iteration: int = 0
    consecutive_failures: int = 0
    prior_fingerprint: str = ""
    prior_status: Optional[str] = None
    prior_started_at: str = ""

    @property
    def message(self) -> str:
        return REFUSAL_REASONS.get(self.reason, self.reason)


def _is_run_start(record: dict) -> bool:
    return record.get("type") == "run_started" or record.get("event") == "run_start"


def _is_run_end(record: dict) -> bool:
    return record.get("event") == "run_end" or record.get("type") in (
        "run_completed",
        "run_blocked",
        "run_failed",
    )


def _is_iteration(record: dict) -> bool:
    return record.get("event") == "iteration"


def load_latest_run_state(ledger_path) -> Optional[dict]:
    records = Ledger(ledger_path).records()
    run_id = None
    for record in records:
        if _is_run_start(record) and record.get("run_id"):
            run_id = record.get("run_id")
    if not run_id:
        return None

    mine = [r for r in records if r.get("run_id") == run_id]
    start = next((r for r in mine if _is_run_start(r)), {})
    iterations = [r for r in mine if _is_iteration(r)]
    end = None
    for record in mine:
        if _is_run_end(record):
            end = record

    # Derive BOTH counters from the same (highest-numbered) iteration record, so
    # an out-of-order ledger can't make the count and failure tally disagree.
    last_record = max(iterations, key=lambda r: r.get("iteration", 0)) if iterations else {}
    return {
        "run_id": run_id,
        "last_iteration": last_record.get("iteration", 0),
        "consecutive_failures": last_record.get("consecutive_failures", 0),
        "status": end.get("status") if end else None,
        "fingerprint": start.get("spec_fingerprint", ""),
        "started_at": start.get("ts", ""),
    }


def resolve_resume(ledger_path, current_fingerprint, *, force: bool = False) -> ResumeDecision:
    if not Path(ledger_path).exists():
        return ResumeDecision(False, NO_LEDGER)

    state = load_latest_run_state(ledger_path)
    if not state:
        return ResumeDecision(False, NO_RESUMABLE_RUN)

    status = state["status"]
    common = dict(
        run_id=state["run_id"],
        prior_fingerprint=state["fingerprint"],
        prior_status=status,
    )

    if status == "success":
        return ResumeDecision(False, ALREADY_SUCCEEDED, **common)
    if status in ("blocked", "no_progress") and not force:
        reason = BLOCKED_NOT_RESUMABLE if status == "blocked" else NO_PROGRESS_NOT_RESUMABLE
        return ResumeDecision(False, reason, **common)
    if (
        state["fingerprint"]
        and current_fingerprint
        and state["fingerprint"] != current_fingerprint
        and not force
    ):
        return ResumeDecision(False, FINGERPRINT_MISMATCH, **common)

    return ResumeDecision(
        True,
        "",
        start_iteration=state["last_iteration"],
        consecutive_failures=state["consecutive_failures"],
        prior_started_at=state.get("started_at", ""),
        **common,
    )
