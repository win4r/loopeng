#!/usr/bin/env python3
"""Deterministic verifier for the Codex CLI demo: exit 0 iff output.txt contains DONE."""
import pathlib
import sys

out = pathlib.Path("output.txt")
if out.exists() and "DONE" in out.read_text():
    print("verify: PASS — output.txt contains DONE")
    sys.exit(0)
got = repr(out.read_text()) if out.exists() else "<missing>"
print(f"verify: FAIL — expected 'DONE' in output.txt, got {got}")
sys.exit(1)
