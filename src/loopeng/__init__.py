"""loopeng — an agent-agnostic Loop Engineering runner.

A small, well-tested core that drives any shell-callable coding agent through a
plan -> act -> verify -> feedback loop: a portable loop spec (loop.yaml), a
deterministic verification gate, guardrails (max iterations, consecutive-failure
circuit breaker, per-command timeout), and an append-only JSONL ledger.

Claude Code and Codex are *presets* layered on top of the generic shell adapter;
the core never depends on either agent's internals.
"""

__version__ = "0.2.0"
