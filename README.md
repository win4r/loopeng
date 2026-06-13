# loopeng

An **agent-agnostic Loop Engineering runner**. It drives any shell-callable coding
agent (Claude Code, Codex, or a plain script) through a bounded
`act → verify → feed-back` loop, defined by a portable `loop.yaml` spec.

It's the small, well-tested core that the current crop of loop-engineering tools
each implement a slice of: a **portable loop spec** (ralphify's idea), **agent-neutral
adapters** (loom), **guardrails + auditable stop conditions** (openloop), a
**deterministic verification gate** (the load-bearing half of Spotify's "Honk"
findings), and **git-friendly state** via an append-only ledger.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

loopeng init            # scaffold loop.yaml + samples/ + .loopeng/
loopeng run             # run the sample loop (fails once, self-corrects, passes)
cat .loopeng/ledger.jsonl
```

## How it works

Each iteration:

1. Render the prompt template — `{{objective}}`, `{{iteration}}`, `{{feedback}}`
   (the previous verifier's output), and any `{{<context-command>}}` outputs.
2. Run the **agent** adapter (prompt on stdin + `$LOOPENG_PROMPT`).
3. Run the **verifier** — exit `0` means pass. This is the gate.
4. Append an iteration record to `.loopeng/ledger.jsonl`.
5. On pass → `success`. On fail → feed the verifier output back and continue.

Termination is bounded three ways:

| Outcome | Trigger | Exit code |
|---|---|---|
| `success` | verifier passed | 0 |
| `blocked` | `max_consecutive_failures` consecutive fails (circuit breaker) | 3 |
| `exhausted` | `max_iterations` reached without a pass | 4 |

Per-command `command_timeout` turns a hung agent/verifier into a normal failure
(exit `124`), so the loop can't wedge.

## The loop spec (`loop.yaml`)

```yaml
objective: "Write DONE into output.txt"
workspace: "."
agent:
  type: shell                 # shell | mock | claude-code | codex
  command: ["python3", "samples/mock_agent.py"]
prompt: |
  Objective: {{objective}}
  Verifier feedback from last attempt: {{feedback}}
verify:
  command: ["python3", "samples/verify.py"]
limits:
  max_iterations: 5
  max_consecutive_failures: 3
  command_timeout: 30
```

## Agents

Every agent is a shell-callable command behind one contract
(input: prompt/workspace/env; output: stdout/stderr/exit-code; controls: timeout/env/cwd;
optional capabilities: resume / session_id / approval_mode / sandbox). The
**generic `shell` adapter is the fully-working, tested path.** `claude-code`
(`claude -p`) and `codex` (`codex exec`) are thin **presets** that preconfigure
that adapter — their default flags are best-effort and not yet validated against
a live binary; override `command:` to pin your own.

## Not yet built (intentionally out of scope)

Multi-agent orchestration, daemon mode, MCP integration, web UI, publishing.

## License

MIT
