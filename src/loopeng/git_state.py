"""Read-only git inspection used by the blast-radius gate.

All calls shell out to ``git`` (no GitPython dependency). The gate uses these to
establish a clean baseline and to compute the set of repository paths an agent
touched during a run — including untracked, deleted, and renamed files (via
``git status --porcelain -z``, not just ``git diff --name-only``).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional, Set


@dataclass
class ChangeEntry:
    status: str  # two-char porcelain code, e.g. ' M', '??', 'A ', ' D', 'R '
    path: str
    orig_path: Optional[str] = None  # populated for renames/copies

    def paths(self) -> List[str]:
        out = [self.path]
        if self.orig_path:
            out.append(self.orig_path)
        return [p for p in out if p]


def _git(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def is_git_repo(workspace) -> bool:
    result = _git(["rev-parse", "--is-inside-work-tree"], workspace)
    return result.returncode == 0 and result.stdout.strip() == "true"


def workspace_prefix(workspace) -> str:
    """Path of ``workspace`` relative to the git top-level (e.g. ``"sub/"``).

    ``git status`` always reports paths relative to the repository root, so when
    the workspace sits below the root this prefix must be stripped before user
    patterns (authored relative to the workspace) can match. Empty at the root.
    """
    result = _git(["rev-parse", "--show-prefix"], workspace)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def change_entries(workspace) -> List[ChangeEntry]:
    """Parse ``git status --porcelain -z`` into entries.

    The -z format is NUL-terminated (no quoting/escaping headaches). A normal
    entry is ``XY <space> PATH``; a rename/copy entry is followed by a second
    NUL-separated token holding the origin path.
    """
    # --untracked-files=all lists files inside new directories individually
    # (default collapses an untracked dir to just "dir/"), which is what the
    # blast-radius gate needs for accurate path matching and counting.
    result = _git(["status", "--porcelain", "-z", "--untracked-files=all"], workspace)
    if result.returncode != 0:
        return []
    tokens = result.stdout.split("\0")
    entries: List[ChangeEntry] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        status = token[:2]
        path = token[3:]  # token[2] is the separating space
        orig = None
        if status and status[0] in ("R", "C"):
            if i + 1 < len(tokens):
                orig = tokens[i + 1]
                i += 1
        entries.append(ChangeEntry(status=status, path=path, orig_path=orig))
        i += 1
    return entries


def changed_path_set(workspace) -> Set[str]:
    """All repository paths that currently differ from HEAD (incl. untracked)."""
    paths: Set[str] = set()
    for entry in change_entries(workspace):
        paths.update(entry.paths())
    return paths


def is_clean(workspace) -> bool:
    return len(change_entries(workspace)) == 0
