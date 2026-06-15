#!/usr/bin/env python3
"""Real, deterministic verifier: import the agent's source, run it, assert its behavior.

Exit 0 iff greeting() returns exactly "Hello, loopeng!". This is the gate loopeng iterates against;
it is NOT a mock or `true`. The agent cannot edit this file (loop.yaml allowed_paths only permits
greeting.py), so a green run proves a real source fix.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
EXPECTED = "Hello, loopeng!"

try:
    from greeting import greeting

    got = greeting()
except Exception as exc:  # noqa: BLE001 - any import/run error is a verify failure
    print(f"verify: FAIL — greeting() raised {exc!r}")
    sys.exit(1)

if got == EXPECTED:
    print(f"verify: PASS — greeting() == {EXPECTED!r}")
    sys.exit(0)
print(f"verify: FAIL — greeting() returned {got!r}, expected {EXPECTED!r}")
sys.exit(1)
