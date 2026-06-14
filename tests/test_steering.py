"""Mid-run steering: re-read loop.yaml each iteration to pick up an edited prompt."""

import json
import sys

from loopeng.cli import main
from loopeng.runner import run_loop
from loopeng.spec import load_spec

PY = sys.executable or "python3"

# An agent that records the exact prompt it received (so we can see what it was steered to).
CAPTURE = [
    PY, "-c",
    "import os, pathlib; pathlib.Path('seen.txt').open('a').write(os.environ['LOOPENG_PROMPT'] + chr(10))",
]


def _yaml(prompt, agent_cmd, verify_cmd):
    return (
        "objective: o\n"
        f"prompt: {json.dumps(prompt)}\n"
        f"agent: {{type: shell, command: {json.dumps(agent_cmd)}}}\n"
        f"verify: {{command: {json.dumps(verify_cmd)}}}\n"
        "limits: {max_iterations: 3, max_consecutive_failures: 9}\n"
    )


def _seen(tmp_path):
    return [line for line in (tmp_path / "seen.txt").read_text().splitlines() if line]


def test_mid_run_prompt_steering(tmp_path):
    # The verifier copies a pre-written steered.yaml over loop.yaml on each iteration (and fails),
    # so iteration 2 should pick up the steered prompt.
    (tmp_path / "steered.yaml").write_text(_yaml("STEERED {{feedback}}", CAPTURE, ["sh", "-lc", "exit 1"]))
    loop = tmp_path / "loop.yaml"
    loop.write_text(_yaml("ORIGINAL {{feedback}}", CAPTURE, ["sh", "-lc", "cp steered.yaml loop.yaml; exit 1"]))

    events = []
    result = run_loop(load_spec(loop), tmp_path, reload_spec_path=str(loop), on_event=events.append)
    assert result.iterations == 3
    seen = _seen(tmp_path)
    assert seen[0].startswith("ORIGINAL")  # iteration 1 used the original prompt
    assert seen[1].startswith("STEERED") and seen[2].startswith("STEERED")  # then steered
    assert any(e["type"] == "prompt_steered" for e in events)


def test_no_steering_without_reload_flag(tmp_path):
    (tmp_path / "steered.yaml").write_text(_yaml("STEERED {{feedback}}", CAPTURE, ["sh", "-lc", "exit 1"]))
    loop = tmp_path / "loop.yaml"
    loop.write_text(_yaml("ORIGINAL {{feedback}}", CAPTURE, ["sh", "-lc", "cp steered.yaml loop.yaml; exit 1"]))

    run_loop(load_spec(loop), tmp_path)  # reload_spec_path NOT passed
    assert all(line.startswith("ORIGINAL") for line in _seen(tmp_path))  # prompt fixed despite edits


def test_invalid_mid_edit_keeps_current_prompt(tmp_path):
    loop = tmp_path / "loop.yaml"
    loop.write_text(_yaml("ORIGINAL {{feedback}}", CAPTURE, ["sh", "-lc", "echo 'bad: [' > loop.yaml; exit 1"]))

    events = []
    run_loop(load_spec(loop), tmp_path, reload_spec_path=str(loop), on_event=events.append)
    assert all(line.startswith("ORIGINAL") for line in _seen(tmp_path))  # invalid reload ignored
    assert any(e["type"] == "spec_reload_failed" for e in events)


def test_cli_run_reload_spec_flag(tmp_path, monkeypatch):
    main(["init", "--path", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--spec", "loop.yaml", "--reload-spec"]) == 0  # sample still succeeds with the flag
