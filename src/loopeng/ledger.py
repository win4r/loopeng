"""Append-only JSONL ledger under .loopeng/ledger.jsonl.

One JSON object per line keeps the loop's history auditable and diff-friendly in
git — a run_start record, one record per iteration, and a run_end record.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict) -> None:
        # Every line is timestamped; caller-supplied keys win over nothing here.
        line = {"ts": utcnow_iso(), **record}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

    def records(self) -> List[dict]:
        if not self.path.exists():
            return []
        records = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                records.append(json.loads(raw))
        return records
