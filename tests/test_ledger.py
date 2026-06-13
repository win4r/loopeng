"""Ledger writing + reading."""

from loopeng.ledger import Ledger


def test_append_creates_parent_and_reads_back(tmp_path):
    ledger = Ledger(tmp_path / ".loopeng" / "ledger.jsonl")
    assert (tmp_path / ".loopeng").is_dir()

    ledger.append({"event": "a", "n": 1})
    ledger.append({"event": "b", "n": 2})

    records = ledger.records()
    assert [r["event"] for r in records] == ["a", "b"]
    assert records[1]["n"] == 2
    assert all("ts" in r for r in records)  # every line is timestamped


def test_records_empty_when_no_file(tmp_path):
    ledger = Ledger(tmp_path / ".loopeng" / "ledger.jsonl")
    assert ledger.records() == []


def test_append_is_jsonl(tmp_path):
    path = tmp_path / ".loopeng" / "ledger.jsonl"
    ledger = Ledger(path)
    ledger.append({"event": "x"})
    ledger.append({"event": "y"})
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 2  # one JSON object per line


def test_records_skips_torn_trailing_line(tmp_path):
    # A crash mid-append can leave a truncated final line; reading must not throw.
    path = tmp_path / ".loopeng" / "ledger.jsonl"
    ledger = Ledger(path)
    ledger.append({"event": "run_start", "run_id": "r1"})
    ledger.append({"event": "iteration", "run_id": "r1", "iteration": 1})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "iteration", "run_id": "r1", "iter')  # torn JSON, no newline
    records = ledger.records()
    assert len(records) == 2  # good lines survive, the torn one is skipped
    assert records[-1]["iteration"] == 1
