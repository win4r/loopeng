"""CLI wiring for the v0.3.0 platform layers: skill / orchestrate / isolate / hooks / plugin.

These exercise main()'s argument parsing + dispatch + exit codes (the integration
seam), complementing the per-module tests.
"""

import shutil
import subprocess

import pytest

from loopeng import adapters
from loopeng.cli import main

HAS_GIT = shutil.which("git") is not None


@pytest.fixture(autouse=True)
def _restore_builders():
    """`--plugin` mutates the process-global adapter registry; snapshot+restore it so a
    leaked builder can't mask a sibling test's unknown-type assertion."""
    snapshot = dict(adapters._BUILDERS)
    try:
        yield
    finally:
        adapters._BUILDERS.clear()
        adapters._BUILDERS.update(snapshot)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_repo(path):
    _git(["init", "-q"], path)
    _git(["config", "user.email", "v@x"], path)
    _git(["config", "user.name", "v"], path)
    (path / "base.txt").write_text("base\n")
    _git(["add", "-A"], path)
    _git(["commit", "-qm", "init"], path)


# --- skill ---------------------------------------------------------------


def test_cli_skill_list(tmp_path, capsys):
    assert main(["skill", "list", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "shell-converge" in out and "fix-until-tests-pass" in out


def test_cli_run_skill_converges(tmp_path):
    rc = main([
        "run", "--skill", "shell-converge", "--dir", str(tmp_path),
        "--set", "agent_cmd=printf 'x\\n' >> p.txt",
        "--set", "verify_cmd=test $(wc -l < p.txt) -ge 2",
    ])
    assert rc == 0
    assert (tmp_path / "p.txt").read_text().count("x") >= 2
    # the rendered spec is persisted for transparency
    assert (tmp_path / ".loopeng" / "skill-shell-converge.rendered.yaml").exists()


def test_cli_run_skill_missing_param_is_spec_error(tmp_path, capsys):
    rc = main(["run", "--skill", "shell-converge", "--dir", str(tmp_path),
               "--set", "agent_cmd=true"])
    assert rc == 2
    assert "missing required parameter" in capsys.readouterr().err


# --- orchestrate ---------------------------------------------------------


def test_cli_orchestrate_linear(tmp_path, capsys):
    (tmp_path / "plan.yaml").write_text(
        """
version: 1
stages:
  a:
    loop:
      objective: write a
      agent: {type: shell, command: ["sh","-lc","echo A > a.txt"]}
      prompt: "go"
      verify: {command: ["test","-f","a.txt"]}
      limits: {max_iterations: 2}
  b:
    needs: [a]
    loop:
      objective: read a
      agent: {type: shell, command: ["sh","-lc","cp a.txt b.txt"]}
      prompt: "go"
      verify: {command: ["test","-f","b.txt"]}
      limits: {max_iterations: 2}
""",
        encoding="utf-8",
    )
    rc = main(["orchestrate", "--plan", str(tmp_path / "plan.yaml"), "--dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[success] a" in out and "[success] b" in out


def test_cli_orchestrate_failure_exit_1(tmp_path):
    (tmp_path / "plan.yaml").write_text(
        """
version: 1
stages:
  boom:
    loop:
      objective: fail
      agent: {type: shell, command: ['true']}
      prompt: "go"
      verify: {command: ["false"]}
      limits: {max_iterations: 1}
""",
        encoding="utf-8",
    )
    assert main(["orchestrate", "--plan", str(tmp_path / "plan.yaml"), "--dir", str(tmp_path)]) == 1


# --- hooks ---------------------------------------------------------------


def test_cli_run_fires_success_hook(tmp_path):
    (tmp_path / "loop.yaml").write_text(
        """
objective: ok
agent: {type: shell, command: ['true']}
prompt: "go"
verify: {command: ['true']}
limits: {max_iterations: 1}
hooks:
  on_success: ["echo FIRED-$LOOPENG_STATUS > hook.txt"]
""",
        encoding="utf-8",
    )
    assert main(["run", "--spec", str(tmp_path / "loop.yaml")]) == 0
    assert (tmp_path / "hook.txt").read_text().strip() == "FIRED-success"


# --- plugin --------------------------------------------------------------


def test_cli_run_with_plugin_resolves_custom_type(tmp_path):
    (tmp_path / "revplug.py").write_text(
        "from loopeng.adapters import ShellAdapter\n"
        "def register(reg):\n"
        "    reg['reverse-echo'] = lambda agent: ShellAdapter(['true'], name='reverse-echo')\n",
        encoding="utf-8",
    )
    (tmp_path / "loop.yaml").write_text(
        "objective: p\nagent: {type: reverse-echo}\nprompt: go\n"
        "verify: {command: ['true']}\nlimits: {max_iterations: 1}\n",
        encoding="utf-8",
    )
    rc = main(["run", "--plugin", str(tmp_path / "revplug.py"), "--spec", str(tmp_path / "loop.yaml")])
    assert rc == 0


def test_cli_run_unknown_type_without_plugin_errors(tmp_path, capsys):
    (tmp_path / "loop.yaml").write_text(
        "objective: p\nagent: {type: nope}\nprompt: go\n"
        "verify: {command: ['true']}\nlimits: {max_iterations: 1}\n",
        encoding="utf-8",
    )
    assert main(["run", "--spec", str(tmp_path / "loop.yaml")]) == 2
    assert "unknown agent type" in capsys.readouterr().err


# --- isolate (needs git) -------------------------------------------------


@pytest.mark.skipif(not HAS_GIT, reason="git not available")
def test_cli_run_isolate_leaves_main_tree_untouched(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "loop.yaml").write_text(
        "objective: make isolated.txt\n"
        "agent: {type: shell, command: [\"sh\",\"-lc\",\"echo hi > isolated.txt\"]}\n"
        "prompt: go\nverify: {command: [\"test\",\"-f\",\"isolated.txt\"]}\n"
        "limits: {max_iterations: 2}\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "add loop"], tmp_path)
    rc = main(["run", "--isolate", "--spec", str(tmp_path / "loop.yaml")])
    assert rc == 0
    # the agent's file must NOT appear in the user's main working tree
    assert not (tmp_path / "isolated.txt").exists()
    # main tree stays clean
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                            capture_output=True, text=True).stdout
    assert status.strip() == ""


@pytest.mark.skipif(not HAS_GIT, reason="git not available")
def test_cli_run_isolate_outside_repo_errors(tmp_path, capsys):
    (tmp_path / "loop.yaml").write_text(
        "objective: x\nagent: {type: shell, command: ['true']}\nprompt: go\n"
        "verify: {command: ['true']}\nlimits: {max_iterations: 1}\n",
        encoding="utf-8",
    )
    # tmp_path is not a git repo -> WorktreeError -> main() net -> exit 2
    assert main(["run", "--isolate", "--spec", str(tmp_path / "loop.yaml")]) == 2
    assert "not a git repository" in capsys.readouterr().err


@pytest.mark.skipif(not HAS_GIT, reason="git not available")
def test_cli_run_isolate_preserves_work_on_branch(tmp_path, capsys):
    """Positive half of --isolate: a passing run commits the agent's work to a kept
    loop/* branch, surfaces the diff, and prints a merge hint — and the committed
    branch must NOT contain loopeng's own .loopeng/ bookkeeping."""
    _git_repo(tmp_path)
    (tmp_path / "loop.yaml").write_text(
        "objective: make isolated.txt\n"
        "agent: {type: shell, command: [\"sh\",\"-lc\",\"echo hi > isolated.txt\"]}\n"
        "prompt: go\nverify: {command: [\"test\",\"-f\",\"isolated.txt\"]}\n"
        "limits: {max_iterations: 2}\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "add loop"], tmp_path)
    assert main(["run", "--isolate", "--spec", str(tmp_path / "loop.yaml")]) == 0
    out = capsys.readouterr().out
    # exactly one loop/* branch kept
    branches = subprocess.run(["git", "branch", "--list", "loop/*"], cwd=tmp_path,
                              capture_output=True, text=True).stdout.split()
    branches = [b for b in branches if b.startswith("loop/")]
    assert len(branches) == 1
    branch = branches[0]
    # the agent's work is recoverable from the branch...
    show = subprocess.run(["git", "show", f"{branch}:isolated.txt"], cwd=tmp_path,
                          capture_output=True, text=True)
    assert show.returncode == 0 and show.stdout.strip() == "hi"
    # ...but loopeng's own .loopeng/ bookkeeping is NOT on the branch
    lsfiles = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch], cwd=tmp_path,
                             capture_output=True, text=True).stdout
    assert ".loopeng" not in lsfiles
    assert "git merge" in out and branch in out


@pytest.mark.skipif(not HAS_GIT, reason="git not available")
def test_cli_run_isolate_failure_discards_branch(tmp_path):
    """A failing isolated run keeps no branch and leaves the main tree clean."""
    _git_repo(tmp_path)
    (tmp_path / "loop.yaml").write_text(
        "objective: never passes\n"
        "agent: {type: shell, command: [\"sh\",\"-lc\",\"echo x > f.txt\"]}\n"
        "prompt: go\nverify: {command: [\"false\"]}\n"
        "limits: {max_iterations: 1}\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "add loop"], tmp_path)
    assert main(["run", "--isolate", "--spec", str(tmp_path / "loop.yaml")]) == 4  # exhausted
    branches = subprocess.run(["git", "branch", "--list", "loop/*"], cwd=tmp_path,
                              capture_output=True, text=True).stdout.strip()
    assert branches == ""  # discarded on failure
    assert subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip() == ""


def test_cli_run_fires_iteration_hook_each_iteration(tmp_path):
    """on_iteration fires once per iteration with LOOPENG_ITERATION populated, and a
    successful run does not mis-fire on_failure (proves cmd_run composes the live stream)."""
    (tmp_path / "loop.yaml").write_text(
        """
objective: append two bytes
agent: {type: shell, command: ["sh", "-lc", "printf x >> p.txt"]}
prompt: "go {{iteration}}"
verify: {command: ["sh", "-lc", "test $(wc -c < p.txt) -ge 2"]}
limits: {max_iterations: 5, max_consecutive_failures: 5}
hooks:
  on_iteration: ["printf 'I%s\\n' \\"$LOOPENG_ITERATION\\" >> iter.log"]
  on_failure: ["printf F >> fail.log"]
""",
        encoding="utf-8",
    )
    assert main(["run", "--spec", str(tmp_path / "loop.yaml")]) == 0
    assert (tmp_path / "iter.log").read_text() == "I1\nI2\n"
    assert not (tmp_path / "fail.log").exists()


# --- schedule (crontab) — hermetic via monkeypatch, never touches the real crontab ---


def test_cli_schedule_dry_run_does_not_install(tmp_path, monkeypatch, capsys):
    import loopeng.triggers as triggers

    monkeypatch.setattr(triggers, "current_crontab", lambda: "")
    called = {"installed": False}
    monkeypatch.setattr(triggers, "install_crontab", lambda text: called.__setitem__("installed", True))
    (tmp_path / "loop.yaml").write_text("objective: o\nagent: {type: shell, command: ['true']}\n"
                                        "prompt: go\nverify: {command: ['true']}\nlimits: {max_iterations: 1}\n")
    rc = main(["schedule", "--cron", "*/30 * * * *", "--marker", "ci", "--spec", str(tmp_path / "loop.yaml")])
    assert rc == 0
    assert called["installed"] is False  # dry-run NEVER writes the crontab
    assert "# loopeng:ci" in capsys.readouterr().out


def test_cli_schedule_apply_installs_merged(tmp_path, monkeypatch, capsys):
    import loopeng.triggers as triggers

    monkeypatch.setattr(triggers, "current_crontab", lambda: "0 0 * * * other-job\n")
    captured = {}
    monkeypatch.setattr(triggers, "install_crontab", lambda text: captured.__setitem__("text", text))
    (tmp_path / "loop.yaml").write_text("objective: o\nagent: {type: shell, command: ['true']}\n"
                                        "prompt: go\nverify: {command: ['true']}\nlimits: {max_iterations: 1}\n")
    rc = main(["schedule", "--cron", "*/30 * * * *", "--marker", "ci", "--apply", "--spec", str(tmp_path / "loop.yaml")])
    assert rc == 0
    assert "other-job" in captured["text"] and "# loopeng:ci" in captured["text"]


def test_cli_schedule_install_failure_is_exit_2(tmp_path, monkeypatch, capsys):
    import subprocess as _sp

    import loopeng.triggers as triggers

    monkeypatch.setattr(triggers, "current_crontab", lambda: "")

    def _boom(_text):
        raise _sp.CalledProcessError(1, ["crontab", "-"])

    monkeypatch.setattr(triggers, "install_crontab", _boom)
    (tmp_path / "loop.yaml").write_text("objective: o\nagent: {type: shell, command: ['true']}\n"
                                        "prompt: go\nverify: {command: ['true']}\nlimits: {max_iterations: 1}\n")
    rc = main(["schedule", "--cron", "*/30 * * * *", "--marker", "ci", "--apply", "--spec", str(tmp_path / "loop.yaml")])
    assert rc == 2
    assert "schedule error" in capsys.readouterr().err
