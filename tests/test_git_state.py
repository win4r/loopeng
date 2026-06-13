"""git_state inspection — exercised against real git repos (git_repo fixture)."""

from loopeng import git_state


def test_is_git_repo_false_on_plain_dir(tmp_path):
    assert git_state.is_git_repo(tmp_path) is False


def test_is_git_repo_true_and_clean(git_repo):
    assert git_state.is_git_repo(git_repo) is True
    assert git_state.is_clean(git_repo) is True


def test_is_clean_false_on_dirty_repo(git_repo):
    (git_repo / "new.txt").write_text("x\n")
    assert git_state.is_clean(git_repo) is False


def test_untracked_files_counted(git_repo):
    (git_repo / "untracked.txt").write_text("u\n")
    assert "untracked.txt" in git_state.changed_path_set(git_repo)


def test_deleted_files_counted(git_repo):
    (git_repo / "seed.txt").unlink()
    paths = git_state.changed_path_set(git_repo)
    assert "seed.txt" in paths


def test_modified_files_counted(git_repo):
    (git_repo / "seed.txt").write_text("changed\n")
    assert "seed.txt" in git_state.changed_path_set(git_repo)


def test_nested_untracked_path_uses_repo_relative_path(git_repo):
    (git_repo / "src").mkdir()
    (git_repo / "src" / "a.py").write_text("x\n")
    paths = git_state.changed_path_set(git_repo)
    assert "src/a.py" in paths


def test_renamed_file_reports_both_paths(git_repo):
    import subprocess

    subprocess.run(
        ["git", "-C", str(git_repo), "mv", "seed.txt", "renamed.txt"],
        check=True,
        capture_output=True,
    )
    paths = git_state.changed_path_set(git_repo)
    assert "renamed.txt" in paths  # the new path
    assert "seed.txt" in paths  # the origin path (rename touches both)


def test_workspace_prefix(git_repo):
    (git_repo / "sub").mkdir()
    assert git_state.workspace_prefix(git_repo) == ""
    assert git_state.workspace_prefix(git_repo / "sub") == "sub/"
