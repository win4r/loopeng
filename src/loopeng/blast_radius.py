"""Blast-radius policy — a repository write-set gate (NOT a security sandbox).

Given the set of repository paths an agent touched, decide whether the change is
within bounds: nothing in ``forbidden_paths``, everything within ``allowed_paths``
(when an allowlist is configured), and no more than ``max_changed_files`` paths.

Path matching is gitignore-lite: ``**`` matches across directory separators,
``*`` matches within a single path segment, ``?`` matches one non-separator char.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional


@dataclass
class BlastRadiusPolicy:
    require_clean_git: bool = False
    max_changed_files: Optional[int] = None
    allowed_paths: List[str] = field(default_factory=list)
    forbidden_paths: List[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(
            self.require_clean_git
            or self.max_changed_files is not None
            or self.allowed_paths
            or self.forbidden_paths
        )


@dataclass
class BlastRadiusResult:
    ok: bool
    violations: List[str]
    changed_paths: List[str]

    @property
    def reason(self) -> str:
        return "; ".join(self.violations)


@lru_cache(maxsize=512)
def _compile(pattern: str):
    out = ["(?s:^"]
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern[i + 1 : i + 2] == "*":
                i += 2
                if pattern[i : i + 1] == "/":
                    # "**/" : zero or more leading directories (so "**/foo"
                    # matches "foo" and "a/foo" but NOT "barfoo").
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")  # trailing ** : anything, including deeper dirs
            else:
                out.append("[^/]*")  # * : within a single segment
                i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    out.append("$)")
    return re.compile("".join(out))


def match_pattern(path: str, pattern: str) -> bool:
    return _compile(pattern).match(path) is not None


def first_match(path: str, patterns) -> Optional[str]:
    for pattern in patterns:
        if match_pattern(path, pattern):
            return pattern
    return None


def evaluate_changes(policy: BlastRadiusPolicy, changed_paths) -> BlastRadiusResult:
    paths = sorted(set(changed_paths))
    violations: List[str] = []

    for path in paths:
        hit = first_match(path, policy.forbidden_paths)
        if hit is not None:
            violations.append(f"{path} matches forbidden pattern '{hit}'")

    if policy.allowed_paths:
        for path in paths:
            if first_match(path, policy.allowed_paths) is None:
                violations.append(f"{path} is outside allowed_paths")

    if policy.max_changed_files is not None and len(paths) > policy.max_changed_files:
        violations.append(
            f"{len(paths)} changed files exceeds max_changed_files={policy.max_changed_files}"
        )

    return BlastRadiusResult(ok=not violations, violations=violations, changed_paths=paths)
