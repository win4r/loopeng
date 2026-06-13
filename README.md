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

## Resume & live status

A run's ledger is **resumable state**, not just an audit log. Every run has a stable
`run_id`, and each iteration is recorded with its iteration number and consecutive-
failure count, so an interrupted or exhausted run can be continued.

```bash
loopeng run                      # ... interrupted, or exhausts max_iterations
loopeng run --resume             # continues the latest run from the next iteration
loopeng run --resume --max-iterations 20   # ... with more headroom
loopeng run --resume --force     # override a 'blocked' run or a changed spec
```

**Resume restores** the iteration counter and the consecutive-failure counter (so the
circuit breaker accounts for failures from before the interruption) and continues
under the **same `run_id`**.

**Resume refuses (exit code `6`)** when:

| Condition | Override |
|---|---|
| no ledger exists | — |
| no resumable run in the ledger | — |
| the latest run already **succeeded** | — |
| the latest run ended **`blocked`** | `--force` |
| the spec **fingerprint changed** since that run | `--force` |

The **spec fingerprint** is a hash of the spec's *meaning* — every field of the parsed
`loop.yaml` (objective, prompt, agent, verify, workspace, context, limits, blast-radius)
— not its formatting/comments. Editing any of those between runs trips the mismatch
guard so you don't resume a run against a spec it never saw; `--force` overrides it.
(Changing the world the agent observes — files on disk, environment — does **not**
change the fingerprint, so the normal interrupted→fix→`--resume` flow works.)

**Blast-radius on resume:** the gate re-baselines against the working tree at the start
of *each* invocation, so `max_changed_files` bounds the *resumed segment*, not the whole
logical run cumulatively (this is deliberate — it keeps any manual fixes you make between
runs from being attributed to the agent). `forbidden_paths`/`allowed_paths` are still
enforced per file on every segment. `require_clean_git` applies only to a fresh run; on
`--resume` the dirty tree is the prior segment's own output and is accepted.

### Live status

While (or after) a run, `.loopeng/heartbeat.json` records where it is. Query it:

```bash
loopeng status            # human summary
loopeng status --json     # one stable JSON object (run_id, phase, iteration, stale, ...)
loopeng status --dir path/to/project
```

The heartbeat is rewritten atomically at each phase (`starting`, `gathering_context`,
`running_agent`, `checking_blast_radius`, `verifying`, `writing_ledger`, and a terminal
`completed`/`blocked`/`failed`). Fields: `run_id, pid, cwd, spec_path, spec_fingerprint,
phase, iteration, max_iterations, consecutive_failures, started_at, updated_at,
last_event, heartbeat_schema_version`. `status` reports **`stale: true`** when the
recorded `pid` is no longer alive — a live pid is authoritative, since the heartbeat only
refreshes between phases and a single phase can legitimately run up to `command_timeout`.
When no pid is recorded it falls back to an `updated_at` age threshold (30s). (Caveat: pid
reuse can rarely make a crashed run's recycled pid read as live.)

### Typed events

Internally the runner emits typed event dicts (`run_started`, `iteration_started`,
`agent_started`/`agent_completed`, `blast_radius_started`/`_passed`/`_violation`,
`verify_started`/`_passed`/`_failed`, `iteration_failed`, `run_completed`/`_blocked`/
`_failed`, `resume_started`/`_loaded`/`_refused`, `heartbeat_written`, …). Each carries
`type`, `run_id`, and `ts` and is JSON-serializable, so it is ledger-compatible. The CLI
renders them to the same human-readable output as before.

### Exit codes

`0` success · `2` spec/adapter error · `3` blocked · `4` exhausted ·
`5` precondition failed (dirty tree with `require_clean_git`) · `6` resume refused.

## Not yet built (intentionally out of scope)

Multi-agent orchestration, daemon mode, MCP integration, web UI, publishing.

## License

MIT
