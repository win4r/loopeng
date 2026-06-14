"""worktree isolation — exercised against real git repos (tempfile + git init).

These tests prove the core safety property: a loop running inside the throwaway
worktree NEVER mutates the user's MAIN working tree. Each test builds its own
repo from scratch (some need 0 commits, all need sibling-dir worktrees), so they
do not reuse the shared ``git_repo`` fixture. Skipped entirely when ``git`` is
not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from loopeng import worktree
from loopeng.errors import WorktreeError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _init_repo(path: Path, *, commit: bool = True) -> Path:
    """Init a git repo at ``path``; optionally seed one commit (``seed.txt``)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "loopeng test")
    _git(path, "config", "commit.gpgsign", "false")
    if commit:
        (path / "seed.txt").write_text("seed\n")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "seed")
    return path


def _commit_in(wt: Path, name: str, body: str, msg: str) -> None:
    (wt / name).write_text(body)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", msg)


def _main_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout


# --- (1) happy path ---------------------------------------------------------


def test_happy_path_isolates_main_tree(tmp_path):
    root = _init_repo(tmp_path / "repo")

    wt_path, branch = worktree.create_isolated_worktree(root)
    assert wt_path.exists()
    assert wt_path != root  # sibling, not the repo itself
    assert wt_path.parent == root.parent  # SIBLING of the repo root
    assert branch.startswith("loop/")

    _commit_in(wt_path, "feature.txt", "hello from worktree\n", "add feature")

    # The user's MAIN working tree was never touched.
    assert _main_status(root) == ""

    diff = worktree.surface_diff(root, branch)
    assert diff != ""
    assert "feature.txt" in diff  # names the file the loop produced

    worktree.remove_worktree(root, wt_path, branch)


# --- (2) repo_root on a non-repo dir ---------------------------------------


def test_repo_root_raises_on_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not a git repository"):
        worktree.repo_root(plain)


# --- (3) assert_head_born on a 0-commit repo -------------------------------


def test_assert_head_born_raises_on_unborn_repo(tmp_path):
    root = _init_repo(tmp_path / "empty", commit=False)
    with pytest.raises(WorktreeError):
        worktree.assert_head_born(root)
    # And the public entrypoint must refuse too (it gates on assert_head_born).
    with pytest.raises(WorktreeError):
        worktree.create_isolated_worktree(root)


# --- (4) cleanup removes the worktree dir + branch -------------------------


def test_remove_worktree_cleans_dir_and_branch(tmp_path):
    root = _init_repo(tmp_path / "repo")
    wt_path, branch = worktree.create_isolated_worktree(root)
    _commit_in(wt_path, "f.txt", "x\n", "work")

    worktree.remove_worktree(root, wt_path, branch)

    assert not wt_path.exists()  # the worktree directory is gone
    # The branch is deleted.
    branches = subprocess.run(
        ["git", "-C", str(root), "branch", "--list", branch],
        capture_output=True,
        text=True,
    ).stdout
    assert branches.strip() == ""
    # Main tree is still clean.
    assert _main_status(root) == ""


def test_remove_worktree_keep_branch(tmp_path):
    root = _init_repo(tmp_path / "repo")
    wt_path, branch = worktree.create_isolated_worktree(root)
    _commit_in(wt_path, "f.txt", "x\n", "work")

    worktree.remove_worktree(root, wt_path, branch, keep_branch=True)

    assert not wt_path.exists()
    branches = subprocess.run(
        ["git", "-C", str(root), "branch", "--list", branch],
        capture_output=True,
        text=True,
    ).stdout
    assert branch in branches  # branch preserved


def test_remove_worktree_guards_against_root(tmp_path):
    """The guard must refuse to remove the repo root as if it were a worktree."""
    root = _init_repo(tmp_path / "repo")
    with pytest.raises(AssertionError):
        worktree.remove_worktree(root, root, "loop/whatever")


# --- (5) MUTATION / isolation: shared file stays byte-identical -------------


def test_overwriting_shared_file_leaves_main_copy_byte_identical(tmp_path):
    root = _init_repo(tmp_path / "repo")
    shared = root / "shared.txt"
    shared.write_bytes(b"ORIGINAL CONTENT\nline2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add shared")

    before = shared.read_bytes()

    wt_path, branch = worktree.create_isolated_worktree(root)
    # Same path, completely different bytes, committed inside the worktree.
    (wt_path / "shared.txt").write_bytes(b"TOTALLY DIFFERENT bytes here\n")
    _git(wt_path, "add", "-A")
    _git(wt_path, "commit", "-q", "-m", "clobber shared inside worktree")

    after = shared.read_bytes()
    assert after == before  # main-tree copy is byte-identical
    assert after == b"ORIGINAL CONTENT\nline2\n"
    # And the worktree really did diverge (mutation is load-bearing, not a no-op).
    assert (wt_path / "shared.txt").read_bytes() == b"TOTALLY DIFFERENT bytes here\n"
    assert "shared.txt" in worktree.surface_diff(root, branch)

    worktree.remove_worktree(root, wt_path, branch)


# --- extra: surface_diff empty when branch == HEAD; has_uncommitted ---------


def test_surface_diff_empty_when_no_change(tmp_path):
    root = _init_repo(tmp_path / "repo")
    wt_path, branch = worktree.create_isolated_worktree(root)
    # No commits made in the worktree -> branch is identical to HEAD.
    assert worktree.surface_diff(root, branch) == ""
    worktree.remove_worktree(root, wt_path, branch)


def test_has_uncommitted_tracks_dirty_worktree(tmp_path):
    root = _init_repo(tmp_path / "repo")
    wt_path, branch = worktree.create_isolated_worktree(root)
    assert worktree.has_uncommitted(wt_path) is False
    (wt_path / "dirty.txt").write_text("uncommitted\n")
    assert worktree.has_uncommitted(wt_path) is True
    worktree.remove_worktree(root, wt_path, branch)


def test_commit_all_excludes_loopeng_state(tmp_path):
    """commit_all captures the agent's work but NEVER loopeng's own .loopeng/
    bookkeeping — the surfaced diff and kept branch are what the user merges."""
    root = _init_repo(tmp_path / "repo")
    wt_path, branch = worktree.create_isolated_worktree(root)
    (wt_path / "real.txt").write_text("agent work\n")
    state = wt_path / ".loopeng"
    state.mkdir()
    (state / "ledger.jsonl").write_text('{"pid": 123, "cwd": "/secret"}\n')
    assert worktree.commit_all(wt_path, "loopeng: test") is True
    diff = worktree.surface_diff(root, branch)
    assert "real.txt" in diff and ".loopeng" not in diff
    tree = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", branch],
        capture_output=True, text=True,
    ).stdout
    assert "real.txt" in tree and ".loopeng" not in tree
    worktree.remove_worktree(root, wt_path, branch)


def test_commit_all_noop_when_only_loopeng_changed(tmp_path):
    """A run that touched only .loopeng/ is 'no real change' -> commit_all returns False."""
    root = _init_repo(tmp_path / "repo")
    wt_path, branch = worktree.create_isolated_worktree(root)
    (wt_path / ".loopeng").mkdir()
    (wt_path / ".loopeng" / "heartbeat.json").write_text("{}\n")
    assert worktree.has_uncommitted(wt_path) is False
    assert worktree.commit_all(wt_path, "loopeng: test") is False
    worktree.remove_worktree(root, wt_path, branch)
