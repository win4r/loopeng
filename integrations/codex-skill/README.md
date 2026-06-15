# loopeng — Codex integration (AGENTS.md)

Makes the **Codex CLI** reach for loopeng automatically. When you say *"run a verify loop"*,
*"fix until tests pass"*, or *"跑闭环"*, Codex inspects/creates a `loop.yaml` (`agent.type: codex`),
runs `loopeng doctor`, prefers `loopeng run --isolate` against a **real** verifier, and reports the
ledger/status, exit code, changed files, and remaining risks — instead of hand-rolling its own loop.

The policy lives in [`AGENTS.md`](AGENTS.md) — the instruction file the Codex CLI reads.

## Install

Codex reads `AGENTS.md` from your repo and from `~/.codex/AGENTS.md` (global). Add this policy to
whichever scope you want:

```bash
# per-project — append to (or paste into) your repo's AGENTS.md:
cat integrations/codex-skill/AGENTS.md >> AGENTS.md

# …or globally, for all your projects:
mkdir -p ~/.codex && cat integrations/codex-skill/AGENTS.md >> ~/.codex/AGENTS.md
```

If your `AGENTS.md` already has content, paste the loopeng section in rather than appending blindly.
Requires the `loopeng` CLI installed (top-level [README → Install](../../README.md#install)); your
Codex is already logged in if you're using it.

## Use

Say a trigger phrase to Codex — e.g. *"run a verify loop until pytest passes"*, *"fix until tests
pass"*, or *"跑闭环修复这个测试"* — and Codex drives loopeng per the policy. See the top-level
[README → Codex CLI](../../README.md#codex-cli) for the `agent.type: codex` spec and the runnable
[`examples/codex-cli-demo/`](../../examples/codex-cli-demo/).

> The `AGENTS.md` convention (repo + `~/.codex/AGENTS.md`) is the OpenAI Codex norm; confirm the path
> against your Codex CLI version (written for codex-cli 0.137.0). This is the Codex-side analog of the
> Claude Code skill in [`integrations/claude-code-skill/`](../claude-code-skill/).
