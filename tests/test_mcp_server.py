"""MCP stdio server: protocol framing + the four loopeng tools.

The E2E test spawns a real subprocess driving ``serve()`` over stdio and feeds it
newline-delimited JSON-RPC, asserting the exact protocol surface (initialize echo,
notification silence, tools/list, a tool call, an error). Direct unit tests cover
``handle_call`` and ``dispatch`` without a process so they stay fast and offline.
"""

import json
import subprocess
import sys
import time

import pytest

from loopeng.mcp_server import (
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    TOOLS,
    dispatch,
    handle_call,
)

_SERVE = "from loopeng.mcp_server import serve; serve()"
_READ_TIMEOUT = 15.0  # generous: covers cold interpreter start on a loaded CI box


def _send(proc, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _read_response_by_id(proc, want_id, *, timeout=_READ_TIMEOUT):
    """Read newline JSON frames until one has ``id == want_id``; fail on timeout.

    Skips any frame whose id doesn't match (there should be none in these scripts,
    but this keeps the assertion robust to interleaving). A notification correctly
    produces no frame, so the loop simply reads the *next* real response.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line == "":
            raise AssertionError(f"stdout closed before a response with id={want_id!r}")
        line = line.strip()
        if not line:
            continue
        frame = json.loads(line)
        if frame.get("id") == want_id:
            return frame
    raise AssertionError(f"timed out waiting for response id={want_id!r}")


def test_stdio_server_end_to_end():
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered stdin from our side
    )
    try:
        # 1) initialize
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
        })
        init = _read_response_by_id(proc, 1)
        assert init["jsonrpc"] == "2.0"
        result = init["result"]
        assert result["protocolVersion"] == "2025-03-26"
        assert result["serverInfo"]["name"] == "loopeng"
        assert result["capabilities"]["tools"]["listChanged"] is False

        # 2) notifications/initialized -> NO response. We prove this by sending it,
        #    then tools/list (id 2): the very next frame we read must be id 2, which
        #    is only possible if the notification produced no frame of its own.
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = _read_response_by_id(proc, 2)
        assert listed["id"] == 2  # the notification did not emit a frame before this
        names = [t["name"] for t in listed["result"]["tools"]]
        assert set(names) == {
            "loopeng_list_skills",
            "loopeng_doctor",
            "loopeng_status",
            "loopeng_run",
        }

        # 3) tools/call loopeng_list_skills -> text content, not an error
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "loopeng_list_skills", "arguments": {}},
        })
        called = _read_response_by_id(proc, 3)
        content = called["result"]["content"]
        assert content[0]["type"] == "text"
        assert called["result"]["isError"] is False
        assert "skill" in content[0]["text"].lower()

        # 4) bogus tool -> isError true OR a -32601 error
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "does_not_exist", "arguments": {}},
        })
        bogus = _read_response_by_id(proc, 4)
        is_tool_error = bogus.get("result", {}).get("isError") is True
        is_rpc_error = bogus.get("error", {}).get("code") == METHOD_NOT_FOUND
        assert is_tool_error or is_rpc_error

        # 5) closing stdin makes the blocking loop hit EOF and the process exit.
        proc.stdin.close()
        assert proc.wait(timeout=_READ_TIMEOUT) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=_READ_TIMEOUT)


def test_malformed_line_yields_parse_error():
    """A non-JSON frame gets a Parse error with id null, and the loop keeps going."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        proc.stdin.write("this is not json\n")
        proc.stdin.flush()
        line = proc.stdout.readline().strip()
        frame = json.loads(line)
        assert frame["id"] is None
        assert frame["error"]["code"] == PARSE_ERROR
        # loop survived the bad line: a normal ping still answers
        _send(proc, {"jsonrpc": "2.0", "id": 7, "method": "ping"})
        pong = _read_response_by_id(proc, 7)
        assert pong["result"] == {}
        proc.stdin.close()
        assert proc.wait(timeout=_READ_TIMEOUT) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=_READ_TIMEOUT)


# --------------------------------------------------------------------------- #
# Direct (in-process) unit tests — fast, no subprocess.
# --------------------------------------------------------------------------- #


def test_handle_call_list_skills_mentions_bundled_skill():
    text, is_error = handle_call("loopeng_list_skills", {})
    assert is_error is False
    # at least one bundled skill is always present
    assert "shell-converge" in text or "fix-until-tests-pass" in text


def test_handle_call_unknown_tool_is_error():
    text, is_error = handle_call("nope", {})
    assert is_error is True
    assert "unknown tool" in text


def test_handle_call_doctor_without_spec_lists_adapter_types(tmp_path):
    text, is_error = handle_call("loopeng_doctor", {}, project_dir=str(tmp_path))
    assert is_error is False  # missing spec is informational, not an error
    assert "shell" in text and "codex" in text


def test_handle_call_doctor_shell_spec_is_ready(tmp_path):
    # A shell adapter never requires a binary, so preflight is OK.
    # Quote "true" so YAML keeps it a string (bare `true` parses as a boolean,
    # which parse_spec rightly rejects). `true` resolves on PATH on POSIX.
    (tmp_path / "loop.yaml").write_text(
        'objective: o\n'
        'agent: {type: shell, command: ["true"]}\n'
        'prompt: p\n'
        'verify: {command: ["true"]}\n'
        'limits: {max_iterations: 1}\n',
        encoding="utf-8",
    )
    text, is_error = handle_call("loopeng_doctor", {"spec": str(tmp_path / "loop.yaml")})
    assert is_error is False
    assert "OK" in text


def test_handle_call_status_no_run(tmp_path):
    text, is_error = handle_call("loopeng_status", {"project_dir": str(tmp_path)})
    assert is_error is False
    assert "no active run" in text


def test_handle_call_status_reads_heartbeat(tmp_path):
    state = tmp_path / ".loopeng"
    state.mkdir()
    # pid 0 is never a live process for os.kill(.,0) on the caller -> reported STALE.
    (state / "heartbeat.json").write_text(
        json.dumps(
            {
                "run_id": "run-xyz",
                "pid": 2147480000,  # almost certainly not a live pid -> stale
                "phase": "verifying",
                "iteration": 2,
                "max_iterations": 5,
                "consecutive_failures": 1,
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    text, is_error = handle_call("loopeng_status", {"project_dir": str(tmp_path)})
    assert is_error is False
    assert "run-xyz" in text
    assert "phase=verifying" in text
    assert "2/5" in text


def test_handle_call_run_requires_skill_name():
    text, is_error = handle_call("loopeng_run", {})
    assert is_error is True
    assert "skill" in text.lower()


def test_handle_call_run_unknown_skill_is_error(tmp_path):
    text, is_error = handle_call("loopeng_run", {"skill": "no-such-skill"}, project_dir=str(tmp_path))
    assert is_error is True
    assert "SkillError" in text or "unknown skill" in text


def test_dispatch_initialize_echoes_known_version_else_default():
    # known version is echoed back
    ok = dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-03-26"}})
    assert ok["result"]["protocolVersion"] == "2025-03-26"
    # an unknown/absent version falls back to the server's supported version
    older = dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "1999-01-01"}})
    assert older["result"]["protocolVersion"] == "2025-03-26"
    none = dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert none["result"]["protocolVersion"] == "2025-03-26"


def test_dispatch_notification_returns_none():
    # any message without an id is a notification -> no response object
    assert dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert dispatch({"jsonrpc": "2.0", "method": "tools/list"}) is None  # no id == notification


def test_dispatch_ping_and_unknown_method():
    assert dispatch({"jsonrpc": "2.0", "id": 5, "method": "ping"})["result"] == {}
    err = dispatch({"jsonrpc": "2.0", "id": 6, "method": "bogus/method"})
    assert err["error"]["code"] == METHOD_NOT_FOUND


def test_tools_list_payload_shape():
    # every advertised tool has a name, description, and an object inputSchema
    assert {t["name"] for t in TOOLS} == {
        "loopeng_list_skills",
        "loopeng_doctor",
        "loopeng_status",
        "loopeng_run",
    }
    for tool in TOOLS:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_handle_call_run_success_polarity(tmp_path):
    """loopeng_run is the only tool that invokes run_loop. A passing run -> is_error
    False + 'SUCCESS' (pins the `not result.passed` verdict polarity). `set` as an object."""
    text, is_error = handle_call(
        "loopeng_run",
        {"skill": "shell-converge", "set": {
            "agent_cmd": "printf 'x\\n' >> p.txt",
            "verify_cmd": "test -s p.txt",
        }},
        project_dir=str(tmp_path),
    )
    assert is_error is False
    assert "SUCCESS" in text
    assert (tmp_path / "p.txt").exists()


def test_handle_call_run_failure_polarity(tmp_path):
    """A run whose verifier never passes -> is_error True + 'DID NOT PASS' (an inverted
    verdict polarity would flip this and fail the test). `set` delivered as a list."""
    text, is_error = handle_call(
        "loopeng_run",
        {"skill": "shell-converge", "set": ["agent_cmd=true", "verify_cmd=false"]},
        project_dir=str(tmp_path),
    )
    assert is_error is True
    assert "DID NOT PASS" in text


def test_serve_notification_yields_no_response_frame():
    """End-to-end over stdio: a notification (no id) produces NO output frame. Feed
    initialize + notifications/initialized + ping; assert exactly TWO responses come
    back (init id 1, ping id 99) — the notification in between yields none."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}})
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 99, "method": "ping"})
        out, _err = proc.communicate(timeout=_READ_TIMEOUT)  # closes stdin -> EOF -> serve exits
    finally:
        if proc.poll() is None:
            proc.kill()
    frames = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert [f.get("id") for f in frames] == [1, 99]  # notification produced no frame
