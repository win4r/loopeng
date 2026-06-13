"""Make `import loopeng` work whether or not the package is installed."""

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def git_repo(tmp_path):
    """A clean git repo in tmp_path with one committed file (`seed.txt`)."""

    def git(*args):
        subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "loopeng test")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "seed.txt").write_text("seed\n")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    return tmp_path
