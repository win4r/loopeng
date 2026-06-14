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

def test_context_truncation(tmp_path):
    ctx = {"big": ContextSpec(command=[PY, "-c", "print('A' * 500)"])}
    values, _ = _gather_context(ctx, tmp_path, 30, max_chars=100)
    assert values["big"].startswith("A" * 100)
    assert "truncated" in values["big"]
    assert len(values["big"]) < 200


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


def test_runner_caches_context_across_iterations(tmp_path):
    spec = parse_spec(
        {
            "objective": "o",
            "prompt": "ctx={{c}} {{feedback}}",
            "agent": {"type": "shell", "command": [PY, "-c", "pass"]},
            "verify": {"command": [PY, "-c", "import sys; sys.exit(1)"]},  # always fail -> 3 iterations
            "context": {"c": {"command": _COUNTER, "cache": True}},
            "limits": {"max_iterations": 3, "max_consecutive_failures": 9},
        }
    )
    result = run_loop(spec, tmp_path)
    assert result.status == "exhausted" and result.iterations == 3
    assert (tmp_path / "n.txt").read_text() == "1"  # cached context ran exactly once


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
