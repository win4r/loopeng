"""Optional metric/baseline verification gate.

On top of the deterministic exit-0 check, a verifier can be required to meet a
numeric threshold: a ``regex`` extracts a metric from the verifier's output and it
is compared against ``value`` in a ``direction``. The iteration passes only when the
verifier exits 0 AND the baseline holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

# direction -> human comparator
DIRECTIONS = {
    "greater": ">",
    "greater_equal": ">=",
    "less": "<",
    "less_equal": "<=",
    "equal": "==",
}


@dataclass
class BaselineSpec:
    regex: str
    direction: str
    value: float
    name: str = "metric"


def _compare(actual: float, direction: str, value: float) -> bool:
    if direction == "greater":
        return actual > value
    if direction == "greater_equal":
        return actual >= value
    if direction == "less":
        return actual < value
    if direction == "less_equal":
        return actual <= value
    if direction == "equal":
        return actual == value
    raise ValueError(f"unknown baseline direction {direction!r}")  # guarded at parse time


def evaluate_baseline(baseline: BaselineSpec, output: str) -> Tuple[bool, Optional[float], str]:
    """Return (ok, actual, reason). A missing/non-numeric metric fails the gate."""
    match = re.search(baseline.regex, output)
    if match is None:
        return False, None, f"metric {baseline.name!r} not found (regex {baseline.regex!r})"
    raw = match.group(1) if match.groups() else match.group(0)
    try:
        actual = float(raw)
    except (TypeError, ValueError):
        return False, None, f"metric {baseline.name!r} value {raw!r} is not numeric"
    if _compare(actual, baseline.direction, baseline.value):
        return True, actual, ""
    comparator = DIRECTIONS.get(baseline.direction, baseline.direction)
    return False, actual, f"{baseline.name}={actual} not {comparator} {baseline.value}"
