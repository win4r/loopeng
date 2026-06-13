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

## Safety model

loopeng can apply **blast-radius controls** — a **repository write-set gate**, not a
security sandbox. After the agent runs (and *before* the verifier), loopeng asks git
what changed and rejects the iteration if the change set steps outside the bounds you
declared. Configure it under `limits:`:

```yaml
limits:
  max_iterations: 5
  max_consecutive_failures: 2
  timeout_seconds: 60

  require_clean_git: true        # fail early if the tree is dirty at loop start
  max_changed_files: 10          # cap how many paths one run may touch
  allowed_paths:                 # if set, every changed path must match one of these
    - "src/**"
    - "tests/**"
    - "README.md"
  forbidden_paths:               # any matching changed path fails the iteration
    - ".env"
    - ".env.*"
    - "secrets/**"
    - "infra/prod/**"
    - "pyproject.toml"
    - "uv.lock"
```

Patterns are authored **relative to the workspace**. `allowed_paths`/`forbidden_paths`
glob examples like `src/**` and `.env` match the agent's changes after they are
normalized to workspace-relative paths.

**How it behaves**

- `require_clean_git: true` → if the working tree is dirty at loop start, the run
  **aborts early** (exit code `5`, ledger event `blast_radius_precondition_failed`)
  so pre-existing edits are never attributed to the agent.
- After each agent step, loopeng computes the agent's change set with
  `git status --porcelain -z --untracked-files=all` (so individual files inside new
  directories are listed, and **untracked, deleted, and renamed** files all count —
  not just `git diff`), relative to the loop-start baseline.
- A change that hits `forbidden_paths`, falls outside a non-empty `allowed_paths`,
  or exceeds `max_changed_files` is a **`blast_radius_violation`**: the iteration
  fails, the verifier is skipped, the violation is recorded in the ledger, and it
  **counts toward `max_consecutive_failures`** (so repeated violations trip the
  circuit breaker → `blocked`). The violation detail is fed back into the next
  prompt, so an agent can self-correct by reverting the offending change.

Path matching is gitignore-lite: `**` crosses directories, `*` stays within one
segment, `?` matches a single non-separator character.

**Explicit limitation.** This is **not a security sandbox**. It only observes the
git worktree *after* the agent runs, and only when the workspace is a git repository
(otherwise the gate is skipped with a warning). It does **not** stop network access,
data exfiltration, writes outside the repo, or destructive commands — it constrains
the **repository write-set** only. Specifically: it matches path strings and does
**not resolve symlinks**, so an agent could create a symlink inside an allowed path
and write through it to a location outside the repo without tripping the gate; and
`.git/` internals are invisible to `git status`, so they cannot be constrained by
`forbidden_paths`. For real isolation, run the agent in a container, VM, or a
dedicated sandbox.

## Not yet built (intentionally out of scope)

Multi-agent orchestration, daemon mode, MCP integration, web UI, publishing.

## License

MIT
