"""Release-polish sanity: version consistency, --version, LICENSE, CHANGELOG."""

import pathlib
import re

import pytest

import loopeng
from loopeng.cli import main

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_version_matches_pyproject():
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version = "([^"]+)"', text)
    assert match is not None
    assert match.group(1) == loopeng.__version__


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "loopeng" in out and loopeng.__version__ in out


def test_license_file_present_and_mit():
    license_text = (_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text


def test_changelog_documents_current_version():
    changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"[{loopeng.__version__}]" in changelog
