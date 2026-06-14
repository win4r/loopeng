"""Lifecycle hooks: parsing, event->hook mapping, isolation, and a real-shell fire."""

import pytest

from loopeng import events as ev
from loopeng.errors import SpecError
from loopeng.hooks import (
    HookSink,
    HooksSpec,
    compose_sinks,
    hook_key_for_event,
    parse_hooks,
)


def test_parse_hooks_coerces_and_validates():
    spec = parse_hooks({"on_success": "echo ok", "on_failure": ["a", "b"]})
    assert spec.on_success == ["echo ok"]  # string coerced to single-element list
    assert spec.on_failure == ["a", "b"]
    assert spec.on_start == []


def test_parse_hooks_empty_is_none():
    assert parse_hooks(None) is None
    assert parse_hooks({}) is None
    assert parse_hooks({"on_start": []}) is None


def test_parse_hooks_rejects_unknown_key():
    with pytest.raises(SpecError, match="unknown key"):
        parse_hooks({"on_finish": ["x"]})


def test_parse_hooks_rejects_bad_type():
    with pytest.raises(SpecError, match="string or a list"):
        parse_hooks({"on_success": [1, 2]})


def test_event_to_hook_mapping():
    assert hook_key_for_event({"type": ev.RUN_STARTED}) == "on_start"
    assert hook_key_for_event({"type": ev.ITERATION_STARTED}) == "on_iteration"
    assert hook_key_for_event({"type": ev.RUN_COMPLETED, "status": "success"}) == "on_success"
    assert hook_key_for_event({"type": ev.RUN_BLOCKED, "status": "blocked"}) == "on_failure"
    assert hook_key_for_event({"type": ev.RUN_FAILED, "status": "exhausted"}) == "on_failure"
    assert hook_key_for_event({"type": ev.VERIFY_PASSED}) is None  # not a hook moment


def test_hooksink_fires_matching_command_with_env():
    calls = []

    def fake_runner(argv, **kw):
        calls.append((argv, kw))
        return __import__("subprocess").CompletedProcess(argv, 0, "", "")

    sink = HookSink(
        HooksSpec(on_success=["deploy.sh"]),
        workspace=".",
        runner=fake_runner,
    )
    sink({"type": ev.RUN_COMPLETED, "status": "success", "run_id": "r1", "iteration": 3})
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == ["sh", "-lc", "deploy.sh"]
    assert kw["env"]["LOOPENG_EVENT"] == ev.RUN_COMPLETED
    assert kw["env"]["LOOPENG_STATUS"] == "success"
    assert kw["env"]["LOOPENG_RUN_ID"] == "r1"
    assert kw["env"]["LOOPENG_ITERATION"] == "3"


def test_hooksink_does_not_fire_on_unrelated_event():
    calls = []
    sink = HookSink(HooksSpec(on_success=["x"]), runner=lambda *a, **k: calls.append(1))
    sink({"type": ev.RUN_FAILED, "status": "exhausted"})  # on_failure empty -> nothing
    sink({"type": ev.ITERATION_STARTED})
    assert calls == []


def test_hooksink_isolates_failing_hook():
    reported = []
    import subprocess

    def failing_runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    sink = HookSink(
        HooksSpec(on_failure=["bad"]),
        report=reported.append,
        runner=failing_runner,
    )
    sink({"type": ev.RUN_FAILED, "status": "exhausted"})  # must not raise
    assert reported and reported[0]["type"] == "hook_failed"
    assert reported[0]["exit_code"] == 1


def test_compose_sinks_isolates_raising_sink():
    seen = []

    def good(e):
        seen.append(e["type"])

    def bad(e):
        raise RuntimeError("nope")

    emit = compose_sinks(bad, good, None)
    emit({"type": "x"})  # must not raise despite `bad`
    assert seen == ["x"]


def test_hooksink_real_shell_end_to_end(tmp_path):
    sentinel = tmp_path / "out.txt"
    sink = HookSink(
        HooksSpec(on_success=[f"echo $LOOPENG_STATUS > {sentinel}"]),
        workspace=str(tmp_path),
    )
    sink({"type": ev.RUN_COMPLETED, "status": "success", "run_id": "r9"})
    assert sentinel.read_text().strip() == "success"
