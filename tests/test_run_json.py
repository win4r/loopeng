"""`loopeng run --json` emits a pure machine-readable JSONL event stream."""

import json
import sys

from loopeng.cli import main

PY = sys.executable or "python3"


def test_run_json_emits_pure_event_stream(tmp_path, monkeypatch, capsys):
    main(["init", "--path", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()  # discard init output
    rc = main(["run", "--spec", "loop.yaml", "--json"])
    assert rc == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]  # EVERY line must be valid JSON
    assert events
    for event in events:
        assert "type" in event and "run_id" in event and "ts" in event
    types = {e["type"] for e in events}
    assert "run_started" in types
    assert "run_completed" in types


def test_run_without_json_prints_human_summary(tmp_path, monkeypatch, capsys):
    main(["init", "--path", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    main(["run", "--spec", "loop.yaml"])
    out = capsys.readouterr().out
    assert "status:" in out  # the human summary line is present only in non-JSON mode


def test_run_json_on_failing_run_stays_pure(tmp_path, monkeypatch, capsys):
    (tmp_path / "loop.yaml").write_text(
        "objective: x\n"
        "agent: {type: shell, command: ['sh', '-lc', 'true']}\n"
        "prompt: '{{feedback}}'\n"
        "verify: 'false'\n"
        "limits: {max_iterations: 5, max_consecutive_failures: 9, no_progress_limit: 2}\n"
    )
    monkeypatch.chdir(tmp_path)
    rc = main(["run", "--spec", "loop.yaml", "--json"])
    assert rc == 8  # no_progress
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]  # every line valid JSON -> no human leak
    types = {e["type"] for e in events}
    assert "no_progress_detected" in types
    assert "run_failed" in types
