"""loopeng — an agent-agnostic Loop Engineering runner.

A small, well-tested core that drives any shell-callable coding agent through a
plan -> act -> verify -> feedback loop: a portable loop spec (loop.yaml), a
deterministic verification gate, guardrails (max iterations, consecutive-failure
circuit breaker, per-command timeout), and an append-only JSONL ledger.

Claude Code and Codex are *presets* layered on top of the generic shell adapter;
the core never depends on either agent's internals.

v0.3.0 adds platform layers that all compose on the same ``run_loop`` core:
reusable skills, git-worktree isolation, daemonless triggers (watch/schedule),
multi-stage DAG orchestration, lifecycle hooks, adapter plugins, and an MCP server.
"""

__version__ = "0.3.1"
