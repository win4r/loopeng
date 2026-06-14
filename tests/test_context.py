"""Context discipline: per-output truncation (context_max_chars) and cache-once."""

import sys

from loopeng.ledger import Ledger
from loopeng.runner import _gather_context, run_loop
from loopeng.spec import ContextSpec, parse_spec

PY = sys.executable or "python3"

# A command that prints a per-call counter (proves how many times it actually ran).
_COUNTER = [
    PY,
    "-c",
    "import pathlib; p = pathlib.Path('n.txt'); "
    "n = int(p.read_text()) if p.exists() else 0; p.write_text(str(n + 1)); print(n)",
]


# --- truncation ---

def test_context_truncation_exact_suffix(tmp_path):
    ctx = {"big": ContextSpec(command=[PY, "-c", "print('A' * 500)"])}
    values, _ = _gather_context(ctx, tmp_path, 30, max_chars=100)
    assert values["big"] == "A" * 100 + "... [+400 chars truncated]"  # pins marker + count


def test_context_truncation_boundary(tmp_path):
    # exactly max_chars -> verbatim (no marker); max_chars+1 -> truncated.
    at = {"x": ContextSpec(command=[PY, "-c", "print('A' * 100, end='')"])}
    assert _gather_context(at, tmp_path, 30, max_chars=100)[0]["x"] == "A" * 100
    over = {"x": ContextSpec(command=[PY, "-c", "print('A' * 101, end='')"])}
    assert "truncated" in _gather_context(over, tmp_path, 30, max_chars=100)[0]["x"]


def test_context_truncation_unicode_no_corruption(tmp_path):
    ctx = {"u": ContextSpec(command=[PY, "-c", "print('é' * 500, end='')"])}
    value = _gather_context(ctx, tmp_path, 30, max_chars=100)[0]["u"]
    assert value.startswith("é" * 100)  # cut on code-point boundaries, not bytes


def test_context_no_truncation_when_unset(tmp_path):
    ctx = {"big": ContextSpec(command=[PY, "-c", "print('A' * 500)"])}
    values, _ = _gather_context(ctx, tmp_path, 30)
    assert values["big"] == "A" * 500


# --- cache-once ---

def test_context_cache_reuses(tmp_path):
    ctx = {"c": ContextSpec(command=_COUNTER, cache=True)}
    store = {}
    v1, _ = _gather_context(ctx, tmp_path, 30, cache_store=store)
    v2, _ = _gather_context(ctx, tmp_path, 30, cache_store=store)
    assert v1["c"] == v2["c"] == "0"  # second call reused the cache (no re-run)


def test_context_without_cache_reruns(tmp_path):
    ctx = {"c": ContextSpec(command=_COUNTER, cache=False)}
    store = {}
    v1, _ = _gather_context(ctx, tmp_path, 30, cache_store=store)
    v2, _ = _gather_context(ctx, tmp_path, 30, cache_store=store)
    assert v1["c"] == "0" and v2["c"] == "1"  # re-ran each call


def test_context_cache_skips_caching_on_failure(tmp_path):
    ctx = {"c": ContextSpec(command=[PY, "-c", "import sys; print('x'); sys.exit(1)"], cache=True)}
    store = {}
    _, e1 = _gather_context(ctx, tmp_path, 30, cache_store=store)
    _, e2 = _gather_context(ctx, tmp_path, 30, cache_store=store)
    assert e1 and e2  # failed both times -> not cached, so it retried
    assert "c" not in store


def test_context_cache_then_retry_on_failure_caches_only_success(tmp_path):
    # Fails on the first call, succeeds on the second; only the success is cached.
    cmd = [
        PY, "-c",
        "import pathlib, sys; f = pathlib.Path('flag'); first = not f.exists(); f.write_text('x'); "
        "print('FAIL' if first else 'OK'); sys.exit(1 if first else 0)",
    ]
    ctx = {"c": ContextSpec(command=cmd, cache=True)}
    store = {}
    _, e1 = _gather_context(ctx, tmp_path, 30, cache_store=store)
    assert e1 and "c" not in store  # first run failed -> not cached
    v2, e2 = _gather_context(ctx, tmp_path, 30, cache_store=store)
    assert not e2 and store["c"] == "OK"  # second run succeeded -> cached
    v3, _ = _gather_context(ctx, tmp_path, 30, cache_store=store)
    assert v3["c"] == "OK"  # third run reused the cache (no third execution)


def test_context_cache_stores_truncated_value(tmp_path):
    ctx = {"big": ContextSpec(command=[PY, "-c", "print('A' * 500)"], cache=True)}
    store = {}
    v1, _ = _gather_context(ctx, tmp_path, 30, max_chars=100, cache_store=store)
    v2, _ = _gather_context(ctx, tmp_path, 30, max_chars=100, cache_store=store)
    assert v1["big"] == v2["big"]  # reused
    assert v1["big"] == "A" * 100 + "... [+400 chars truncated]"  # the cached value is truncated


def test_runner_caches_context_across_iterations(tmp_path):
    # The agent records the prompt it actually received, so we verify both that the
    # context command ran once AND that every iteration's prompt carried the cached value.
    capture = [
        PY, "-c",
        "import os, pathlib; pathlib.Path('seen.txt').open('a').write(os.environ['LOOPENG_PROMPT'] + chr(10))",
    ]
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "ctx={{c}}",
            "agent": {"type": "shell", "command": capture},
            "verify": {"command": [PY, "-c", "import sys; sys.exit(1)"]},  # always fail -> 3 iterations
            "context": {"c": {"command": _COUNTER, "cache": True}},
            "limits": {"max_iterations": 3, "max_consecutive_failures": 9},
        }
    )
    result = run_loop(spec, tmp_path)
    assert result.status == "exhausted" and result.iterations == 3
    assert (tmp_path / "n.txt").read_text() == "1"  # cached context ran exactly once
    assert (tmp_path / "seen.txt").read_text().count("ctx=0") == 3  # all 3 prompts saw the cached value


def test_fingerprint_collapses_cache_off_context_to_bare_command():
    import hashlib
    import json
    from dataclasses import asdict

    from loopeng.spec import _strip_none, fingerprint

    def _spec(context):
        return parse_spec({
            "objective": "o", "prompt": "{{git}}",
            "agent": {"type": "shell", "command": ["true"]},
            "verify": "true", "context": context,
        })

    spec = _spec({"git": "git status"})
    # Expected = the pre-ContextSpec representation (context as bare commands); a fix that
    # leaves cache:false entries as {command, cache:false} would hash differently.
    payload = asdict(spec)
    payload["context"] = {"git": "git status"}
    expected = hashlib.sha256(
        json.dumps(_strip_none(payload), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    assert fingerprint(spec) == expected

    # cache:true is a genuine semantic change and must change the hash.
    assert fingerprint(spec) != fingerprint(_spec({"git": {"command": "git status", "cache": True}}))


# --- spec parsing ---

def test_context_dict_and_plain_forms_parsed():
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "{{git}}{{date}}",
            "agent": {"type": "shell", "command": ["true"]},
            "verify": "true",
            "context": {"git": {"command": ["git", "status"], "cache": True}, "date": ["date"]},
        }
    )
    assert spec.context["git"].command == ["git", "status"] and spec.context["git"].cache is True
    assert spec.context["date"].command == ["date"] and spec.context["date"].cache is False


def test_context_max_chars_parsed_and_default_none():
    assert parse_spec({
        "objective": "o", "prompt": "p", "agent": {"type": "shell", "command": ["true"]},
        "verify": "true", "limits": {"context_max_chars": 500},
    }).limits.context_max_chars == 500
    assert parse_spec({
        "objective": "o", "prompt": "p", "agent": {"type": "shell", "command": ["true"]},
        "verify": "true",
    }).limits.context_max_chars is None
