"""Git worktree isolation so a loop runs in a throwaway checkout.

The user's MAIN working tree is never touched: the loop's agent edits and
commits inside a sibling temp checkout on a disposable ``loop/<hex>`` branch.
The orchestrator surfaces the resulting diff back to the user and then removes
the worktree (and, by default, its branch).

All git access shells out to the real ``git`` binary via ``subprocess`` (no
GitPython dependency). Every call checks the return code and raises
:class:`~loopeng.errors.WorktreeError` on failure. We always derive the
worktree path from our own ``mkdtemp`` — never by parsing git's stdout — and
redirect git's stdout/stderr into the captured buffers so nothing leaks to the
console.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from .errors import WorktreeError


def _git(args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True)


def _check(result: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WorktreeError(f"{what}: {detail}" if detail else what)
    return result


def repo_root(cwd) -> Path:
    """Resolve the git top-level for ``cwd``.

    Raises :class:`WorktreeError` if ``cwd`` is not inside a git repository.
    The returned path is already canonical (git emits the realpath), so the
    ``wt_path != root`` guard in :func:`remove_worktree` is reliable.
    """
    result = _git(["rev-parse", "--show-toplevel"], cwd=Path(cwd))
    if result.returncode != 0:
        raise WorktreeError("not a git repository")
    top = result.stdout.strip()
    if not top:
        raise WorktreeError("not a git repository")
    return Path(top)


def assert_head_born(root) -> None:
    """Raise unless ``HEAD`` resolves to a commit.

    ``git worktree add ... HEAD`` cannot work on an unborn/empty repository
    (zero commits), so callers gate on this first to get a clear error.
    """
    result = _git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=Path(root))
    if result.returncode != 0:
        raise WorktreeError("repository has no commits (unborn HEAD)")


def create_isolated_worktree(root, branch: Optional[str] = None) -> Tuple[Path, str]:
    """Add a throwaway worktree as a SIBLING of ``root`` checked out at HEAD.

    Prunes stale worktree registrations first, then makes an empty temp dir
    next to the repo (``tempfile.mkdtemp(prefix="loopeng-wt-", dir=root.parent)``)
    and runs ``git worktree add -b <branch> <wt_path> HEAD``. The branch defaults
    to ``loop/<uuid4 hex[:8]>``. Returns ``(wt_path, branch)`` where ``wt_path``
    is derived from our own mkdtemp (never from git stdout).
    """
    root = Path(root)
    assert_head_born(root)
    # Prune stale registrations so a leftover entry can't block the add.
    _check(_git(["worktree", "prune"], cwd=root), "git worktree prune failed")
    if branch is None:
        branch = "loop/" + uuid.uuid4().hex[:8]
    # Sibling temp dir: created empty, which `git worktree add` accepts.
    wt_path = Path(tempfile.mkdtemp(prefix="loopeng-wt-", dir=str(root.parent))).resolve()
    result = _git(["worktree", "add", "-b", branch, str(wt_path), "HEAD"], cwd=root)
    if result.returncode != 0:
        # Best-effort cleanup of the empty dir we created before re-raising.
        try:
            wt_path.rmdir()
        except OSError:
            pass
        detail = (result.stderr or result.stdout or "").strip()
        raise WorktreeError(
            f"git worktree add failed: {detail}" if detail else "git worktree add failed"
        )
    return wt_path, branch


def surface_diff(root, branch: str) -> str:
    """Return the diff + commit log of ``branch`` relative to HEAD of ``root``.

    Empty string means the branch introduced no change. The result concatenates
    ``git diff HEAD..<branch>`` with ``git log --oneline HEAD..<branch>`` so the
    caller sees both the textual diff and the list of new commits.
    """
    root = Path(root)
    diff = _check(
        _git(["diff", "HEAD.." + branch], cwd=root), "git diff failed"
    ).stdout
    log = _check(
        _git(["log", "--oneline", "HEAD.." + branch], cwd=root), "git log failed"
    ).stdout
    if not diff.strip() and not log.strip():
        return ""
    parts = []
    if diff:
        parts.append(diff.rstrip("\n"))
    if log.strip():
        parts.append("commits:\n" + log.rstrip("\n"))
    return "\n".join(parts) + "\n"


def _changed_paths_excluding_state(wt_path) -> List[str]:
    """Worktree porcelain change paths to stage, dropping anything under ``.loopeng/``.

    Both sides of a rename ("old -> new") are returned so a staged commit is complete.
    """
    result = _check(_git(["status", "--porcelain"], cwd=Path(wt_path)), "git status failed")
    paths: List[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        body = line[3:]
        # "XY path", or "XY old -> new" for renames/copies: stage both sides.
        sides = body.split(" -> ") if " -> " in body else [body]
        for side in sides:
            path = side.strip().strip('"')
            if not path or ".loopeng" in Path(path).parts:
                continue
            paths.append(path)
    return paths


def has_uncommitted(wt_path) -> bool:
    """True if the worktree has real changes — loopeng's own ``.loopeng/`` is ignored."""
    return bool(_changed_paths_excluding_state(wt_path))


def commit_all(wt_path, message: str) -> bool:
    """Stage and commit the agent's work in the worktree; ``True`` if a commit was made.

    Agents edit files but rarely commit, so this captures their work onto the
    disposable ``loop/<hex>`` branch — making the change durable (surfaced by
    :func:`surface_diff` and mergeable by the user) so the worktree dir can be
    removed without losing it. loopeng's own ``.loopeng/`` state (ledger, heartbeat)
    is excluded so merging the branch never pulls run bookkeeping into the user's
    tree. Returns ``False`` when there is no real change. Git identity is supplied
    inline so the commit never blocks on unset config.
    """
    wt_path = Path(wt_path)
    paths = _changed_paths_excluding_state(wt_path)
    if not paths:
        return False
    # Stage exactly the agent's changed paths — NOT a "." pathspec. A "." add trips git's
    # "paths ignored / Use -f" error (rc=1, which `_check` would raise) when the workspace
    # .gitignore ignores `.loopeng/` as a directory, and that previously discarded the agent's
    # commit. These paths already exclude `.loopeng/`, so loopeng's own state is never committed.
    _check(_git(["add", "-A", "--", *paths], cwd=wt_path), "git add failed")
    result = _git(
        [
            "-c", "user.name=loopeng",
            "-c", "user.email=loopeng@localhost",
            "commit", "-m", message,
        ],
        cwd=wt_path,
    )
    _check(result, "git commit failed")
    return True


def remove_worktree(root, wt_path, branch: str, *, keep_branch: bool = False) -> None:
    """Force-remove the worktree, prune, and (by default) delete its branch.

    Guards that ``wt_path`` is not the repo root before any destructive call so
    a mis-wired caller can never blow away the user's main checkout.
    """
    root = Path(root)
    wt_path = Path(wt_path).resolve()
    assert wt_path != root.resolve(), "refusing to remove the repo root as a worktree"
    _check(
        _git(["worktree", "remove", "--force", str(wt_path)], cwd=root),
        "git worktree remove failed",
    )
    _check(_git(["worktree", "prune"], cwd=root), "git worktree prune failed")
    if not keep_branch:
        _check(_git(["branch", "-D", branch], cwd=root), "git branch -D failed")
