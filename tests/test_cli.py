"""CLI: init scaffolding + the end-to-end fail-once-then-pass loop."""

from loopeng.cli import main
from loopeng.ledger import Ledger


def test_init_creates_expected_files(tmp_path):
    assert main(["init", "--path", str(tmp_path)]) == 0
    assert (tmp_path / "loop.yaml").exists()
    assert (tmp_path / "samples" / "mock_agent.py").exists()
    assert (tmp_path / "samples" / "verify.py").exists()
    assert (tmp_path / ".loopeng").is_dir()


def test_init_refuses_overwrite_without_force(tmp_path):
    assert main(["init", "--path", str(tmp_path)]) == 0
    assert main(["init", "--path", str(tmp_path)]) == 2  # already exists
    assert main(["init", "--path", str(tmp_path), "--force"]) == 0  # explicit overwrite


def test_run_end_to_end_fail_once_then_pass(tmp_path, monkeypatch):
    main(["init", "--path", str(tmp_path)])
    monkeypatch.chdir(tmp_path)

    exit_code = main(["run", "--spec", "loop.yaml"])

    assert exit_code == 0
    assert (tmp_path / "output.txt").read_text().strip() == "DONE"

    records = Ledger(tmp_path / ".loopeng" / "ledger.jsonl").records()
    iterations = [r for r in records if r["event"] == "iteration"]
    assert len(iterations) == 2
    assert iterations[0]["result"] == "fail"  # first attempt writes WIP -> verifier fails
    assert iterations[1]["result"] == "pass"  # feedback drives the fix -> DONE
    assert records[-1]["event"] == "run_end"
    assert records[-1]["status"] == "success"
