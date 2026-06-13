"""Pure-function tests for the blast-radius policy (no git, no filesystem)."""

from loopeng.blast_radius import BlastRadiusPolicy, evaluate_changes, match_pattern


def test_glob_double_star_matches_nested():
    assert match_pattern("src/loopeng/spec.py", "src/**")
    assert match_pattern("src/a.py", "src/**")
    assert not match_pattern("docs/a.py", "src/**")


def test_glob_single_segment_star_vs_double():
    # "*" stays within a segment; ".env.*" must not match bare ".env".
    assert match_pattern(".env.local", ".env.*")
    assert not match_pattern(".env", ".env.*")
    assert match_pattern(".env", ".env")
    # A single star does not cross a directory boundary.
    assert not match_pattern("a/b/c.py", "a/*.py")
    assert match_pattern("a/c.py", "a/*.py")


def test_exact_file_pattern():
    assert match_pattern("pyproject.toml", "pyproject.toml")
    assert not match_pattern("src/pyproject.toml", "pyproject.toml")


def test_leading_double_star_does_not_overmatch():
    # "**/x" means zero-or-more leading dirs, NOT "any chars then x".
    assert match_pattern(".env", "**/.env")
    assert match_pattern("a/.env", "**/.env")
    assert match_pattern("a/b/.env", "**/.env")
    assert not match_pattern("prod.env", "**/.env")
    assert match_pattern("foo", "**/foo")
    assert match_pattern("a/foo", "**/foo")
    assert not match_pattern("barfoo", "**/foo")


def test_allowed_change_passes():
    policy = BlastRadiusPolicy(allowed_paths=["src/**", "tests/**"], forbidden_paths=[".env"])
    result = evaluate_changes(policy, ["src/loopeng/x.py", "tests/test_x.py"])
    assert result.ok
    assert result.violations == []


def test_forbidden_path_fails():
    policy = BlastRadiusPolicy(forbidden_paths=[".env", "secrets/**"])
    result = evaluate_changes(policy, ["src/x.py", ".env", "secrets/key.pem"])
    assert not result.ok
    assert any(".env" in v for v in result.violations)
    assert any("secrets/key.pem" in v for v in result.violations)


def test_change_outside_allowed_fails():
    policy = BlastRadiusPolicy(allowed_paths=["src/**"])
    result = evaluate_changes(policy, ["docs/readme.md"])
    assert not result.ok
    assert any("outside allowed_paths" in v for v in result.violations)


def test_max_changed_files_exceeded_fails():
    policy = BlastRadiusPolicy(max_changed_files=10)
    result = evaluate_changes(policy, [f"src/f{i}.py" for i in range(11)])
    assert not result.ok
    assert any("max_changed_files" in v for v in result.violations)


def test_max_changed_files_at_limit_passes():
    policy = BlastRadiusPolicy(max_changed_files=10)
    result = evaluate_changes(policy, [f"src/f{i}.py" for i in range(10)])
    assert result.ok


def test_policy_active_flag():
    assert not BlastRadiusPolicy().active
    assert BlastRadiusPolicy(require_clean_git=True).active
    assert BlastRadiusPolicy(forbidden_paths=["x"]).active
    assert BlastRadiusPolicy(max_changed_files=0).active
