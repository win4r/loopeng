"""run_proc turns failure modes into typed data instead of raising."""

import sys

from loopeng.proc import EXIT_NOTEXEC, EXIT_NOTFOUND, EXIT_TIMEOUT, run_proc

PY = sys.executable or "python3"


def test_missing_binary_is_exit_127_not_exception(tmp_path):
    result = run_proc(["loopeng-no-such-binary-xyz"], cwd=tmp_path, timeout=10)
    assert result.exit_code == EXIT_NOTFOUND
    assert not result.ok
    assert "not found" in result.stderr.lower()


def test_timeout_is_exit_124(tmp_path):
    result = run_proc([PY, "-c", "import time; time.sleep(5)"], cwd=tmp_path, timeout=1)
    assert result.exit_code == EXIT_TIMEOUT
    assert result.timed_out is True
    assert not result.ok


def test_nonzero_exit_captured(tmp_path):
    result = run_proc([PY, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], cwd=tmp_path, timeout=10)
    assert result.exit_code == 3
    assert not result.ok
    assert "boom" in result.stderr


def test_non_executable_binary_is_exit_126(tmp_path):
    not_exec = tmp_path / "noexec"
    not_exec.write_text("#!/bin/sh\necho hi\n")  # exists, but NOT chmod +x
    result = run_proc([str(not_exec)], cwd=tmp_path, timeout=10)
    assert result.exit_code == EXIT_NOTEXEC  # 126, not an uncaught PermissionError
    assert not result.ok
    assert "not executable" in result.stderr.lower()


def test_success_exit_zero(tmp_path):
    result = run_proc([PY, "-c", "print('hi')"], cwd=tmp_path, timeout=10)
    assert result.ok
    assert "hi" in result.stdout
