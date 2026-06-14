"""A minimal Model Context Protocol (MCP) server over the stdio transport.

So an MCP-capable client (Claude Code, Codex) can drive loopeng as a tool
provider, this exposes a handful of in-process, fast tools — list skills, run
adapter doctor, read run status, and run a skill loop — over newline-delimited
JSON-RPC 2.0 on stdin/stdout. Pure stdlib (the loopeng core already owns YAML).

Transport (stdio):
  * one JSON-RPC message per line on stdin; one JSON response per line on stdout
    (flushed after every write so a synchronous client never blocks);
  * logs go to stderr only — stdout is reserved for protocol frames;
  * a *notification* (a message with no ``id``) is processed but never answered.

The protocol surface is deliberately tiny and matches MCP 2025-03-26: just enough
of ``initialize`` / ``tools/list`` / ``tools/call`` (plus ``ping``) for a client to
discover and invoke the four loopeng tools. Tool execution is delegated to
``handle_call`` so it is unit-testable without spawning a process.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__
from .adapters import _BUILDERS, build_adapter
from .errors import LoopengError
from .heartbeat import HEARTBEAT_FILENAME, is_stale, read_heartbeat
from .runner import STATE_DIR
from .skills import discover_skills, load_skill, parse_set_args, render_to_spec

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "loopeng"

# JSON-RPC error codes we emit (a deliberate subset of the spec's reserved range).
PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601

# The tools/list payload. Schemas are intentionally permissive (all args optional
# except where a tool truly needs one) so a client can call with `{}` and get a
# helpful text answer rather than a schema-validation rejection.
TOOLS: List[dict] = [
    {
        "name": "loopeng_list_skills",
        "description": (
            "List the reusable loopeng skills (parameterized loop templates) "
            "discoverable from the project, with their descriptions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {
                    "type": "string",
                    "description": "Directory to discover skills from (default '.').",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "loopeng_doctor",
        "description": (
            "Check that the agent adapter configured in a loop spec is ready "
            "(its binary resolves). With no spec, lists the available adapter types."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "string",
                    "description": "Path to a loop.yaml (default './loop.yaml' if present).",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "loopeng_status",
        "description": (
            "Report the live run state from <project_dir>/.loopeng/heartbeat.json "
            "(phase, iteration, and whether the run looks stale)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {
                    "type": "string",
                    "description": "Project directory holding .loopeng/ (default '.').",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "loopeng_run",
        "description": (
            "Render a skill into a loop spec and run the loop to completion, "
            "returning the final status and iteration count."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Name of the skill to run."},
                "set": {
                    "type": "object",
                    "description": "Skill parameter values (key -> string value).",
                },
                "project_dir": {
                    "type": "string",
                    "description": "Project directory to run in (default '.').",
                },
            },
            "required": ["skill"],
            "additionalProperties": False,
        },
    },
]


# --------------------------------------------------------------------------- #
# Tool implementations. Each returns (text, is_error); none raises.
# --------------------------------------------------------------------------- #


def _set_values(arguments: dict) -> Dict[str, str]:
    """Coerce a tools/call ``set`` argument (an object, or a list of k=v) into a dict.

    The MCP-native shape is an object (``{"k": "v"}``); we also accept the CLI-style
    ``["k=v", ...]`` list so a client built around ``--set`` strings still works.
    """
    raw = arguments.get("set")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return parse_set_args([str(item) for item in raw])
    raise ValueError("`set` must be an object (key -> value) or a list of 'key=value' strings")


def _tool_list_skills(arguments: dict, project_dir) -> Tuple[str, bool]:
    skills = discover_skills(project_dir)
    if not skills:
        return ("No skills found.", False)
    lines = [f"{len(skills)} skill(s):"]
    for name in sorted(skills):
        skill = skills[name]
        desc = skill.description or "(no description)"
        lines.append(f"- {name} [{skill.source}]: {desc}")
    return ("\n".join(lines), False)


def _tool_doctor(arguments: dict, project_dir) -> Tuple[str, bool]:
    spec_arg = arguments.get("spec")
    if spec_arg:
        spec_path = Path(spec_arg)
    else:
        spec_path = Path(project_dir) / "loop.yaml"
    if not spec_path.exists():
        types = ", ".join(sorted(_BUILDERS))
        return (
            f"No spec at {spec_path}. Available adapter types: {types}.",
            False,
        )
    # load_spec is imported lazily to keep the module import light and to surface
    # any (rare) YAML import error as a tool error rather than a server crash.
    from .spec import load_spec

    spec = load_spec(spec_path)
    adapter = build_adapter(spec.agent)
    workspace = (spec_path.resolve().parent / spec.workspace).resolve()
    pf = adapter.preflight(cwd=workspace)
    if pf.ok:
        return (
            f"adapter {pf.adapter_type!r}: OK — binary={pf.binary!r} "
            f"resolved={pf.resolved_path!r}",
            False,
        )
    return (f"adapter {pf.adapter_type!r}: NOT READY — {pf.reason}", True)


def _tool_status(arguments: dict, project_dir) -> Tuple[str, bool]:
    base = Path(arguments.get("project_dir") or project_dir)
    state_dir = base / STATE_DIR
    heartbeat = read_heartbeat(state_dir / HEARTBEAT_FILENAME)
    if heartbeat is None:
        return (f"no active run (no heartbeat under {state_dir})", False)
    stale = is_stale(heartbeat)
    return (
        "run {run}: phase={phase} iteration={it}/{maxit} failures={fail} "
        "{liveness} (updated {updated})".format(
            run=heartbeat.get("run_id"),
            phase=heartbeat.get("phase"),
            it=heartbeat.get("iteration"),
            maxit=heartbeat.get("max_iterations"),
            fail=heartbeat.get("consecutive_failures"),
            liveness="STALE" if stale else "live",
            updated=heartbeat.get("updated_at"),
        ),
        False,
    )


def _tool_run(arguments: dict, project_dir) -> Tuple[str, bool]:
    name = arguments.get("skill")
    if not name or not isinstance(name, str):
        return ("loopeng_run requires a 'skill' argument (the skill name).", True)
    # Imported lazily: a run is the one heavyweight tool, and only it needs the runner.
    from .runner import run_loop

    values = _set_values(arguments)
    skill = load_skill(name, project_dir)
    spec, _rendered = render_to_spec(skill, values, source=f"mcp:{name}")
    result = run_loop(spec, project_dir, spec_path=None)
    verdict = "SUCCESS" if result.passed else "DID NOT PASS"
    return (
        f"{verdict} — status={result.status} iterations={result.iterations} "
        f"run_id={result.run_id}",
        not result.passed,
    )


_TOOLS = {
    "loopeng_list_skills": _tool_list_skills,
    "loopeng_doctor": _tool_doctor,
    "loopeng_status": _tool_status,
    "loopeng_run": _tool_run,
}


def handle_call(name: str, arguments: dict, *, project_dir=".") -> Tuple[str, bool]:
    """Dispatch a tools/call to its tool. Returns ``(text, is_error)``; never raises.

    Any failure (unknown tool, bad spec, adapter/skill error, or an unexpected
    exception) is reported as ``is_error=True`` with a human-readable message, so a
    misbehaving tool surfaces in the client as a tool error rather than tearing down
    the whole MCP session.
    """
    arguments = arguments or {}
    tool = _TOOLS.get(name)
    if tool is None:
        known = ", ".join(sorted(_TOOLS))
        return (f"unknown tool {name!r}. Available: {known}", True)
    try:
        return tool(arguments, project_dir)
    except LoopengError as exc:
        return (f"{type(exc).__name__}: {exc}", True)
    except Exception as exc:  # defensive: never let one tool crash the server
        return (f"unexpected error in {name}: {type(exc).__name__}: {exc}", True)


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch.
# --------------------------------------------------------------------------- #


def _result(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def dispatch(message: dict, *, project_dir=".") -> Optional[dict]:
    """Handle one parsed JSON-RPC message; return the response, or None for a notification.

    A message with no ``id`` is a notification (e.g. ``notifications/initialized``):
    it is processed for its side effects but never answered — per JSON-RPC, sending a
    reply to a notification is itself a protocol error, so we return None.
    """
    has_id = "id" in message
    req_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    # Notifications (no id) are processed but never answered.
    if not has_id:
        return None

    if method == "initialize":
        client_version = params.get("protocolVersion")
        version = client_version if client_version == PROTOCOL_VERSION else PROTOCOL_VERSION
        return _result(
            req_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            },
        )

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        call_params = params or {}
        name = call_params.get("name")
        arguments = call_params.get("arguments") or {}
        proj = arguments.get("project_dir") or project_dir
        text, is_error = handle_call(str(name), arguments, project_dir=proj)
        return _result(
            req_id,
            {"content": [{"type": "text", "text": text}], "isError": is_error},
        )

    return _error(req_id, METHOD_NOT_FOUND, "Method not found")


# --------------------------------------------------------------------------- #
# The blocking stdio loop.
# --------------------------------------------------------------------------- #


def serve(stdin=None, stdout=None, stderr=None, *, project_dir=".") -> None:
    """Run the MCP stdio server: read JSON-RPC lines, dispatch, write responses.

    Blocks reading ``stdin`` line by line until EOF (the client closing the pipe),
    then returns. Each response is written as one JSON line and flushed immediately.
    Streams default to ``sys.stdin/out/err`` but are injectable for testing.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    for line in stdin:
        line = line.strip()
        if not line:
            continue  # tolerate blank keep-alive lines
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # A malformed frame: respond with a Parse error (id null). We cannot
            # know whether the sender meant a notification, so we always reply.
            _write(stdout, _error(None, PARSE_ERROR, "Parse error"))
            continue
        if not isinstance(message, dict):
            _write(stdout, _error(None, PARSE_ERROR, "Parse error"))
            continue
        try:
            response = dispatch(message, project_dir=project_dir)
        except Exception as exc:  # never let one bad message kill the loop
            print(f"loopeng-mcp: dispatch error: {exc}", file=stderr, flush=True)
            continue
        if response is not None:
            _write(stdout, response)


def _write(stdout, payload: dict) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout.flush()
