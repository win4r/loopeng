# Changelog

All notable changes to loopeng are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may change behavior).

## [0.3.1] - 2026-06-14

Fixes from a real-world dogfood of the v0.3.0 release assets (every platform layer
exercised end-to-end against the installed wheel/sdist by independent agents).

### Fixed
- **skills (critical):** one malformed file in a `.loopeng/skills/` dir no longer breaks
  *all* skill operations (incl. bundled skills). A bad file is now warned-and-skipped, as
  the docstring always promised. `--set` values containing a newline are rejected.
- **plugins:** an explicit `--plugin` whose `register()` raises is now a clean `PluginError`
  (exit 2), not a raw traceback; overriding an already-registered agent type emits a warning.
  `build_adapter` also normalizes a plugin builder that raises at build time or returns a
  non-adapter object into a clean `AdapterError` (was a raw traceback / late `AttributeError`).
- **MCP:** `tools/call`/`initialize` with non-object `params`/`arguments` now returns
  JSON-RPC `-32602 Invalid params` instead of silently dropping the request (which hung a
  synchronous client); `serve()` always replies to a request id even on an internal error.
- **`run --isolate`:** refuses an absolute/escaping `workspace:` (which would silently
  bypass isolation and mutate the real tree); lifecycle hooks now run with `cwd` = the
  loop's workspace (the worktree under `--isolate`), so relative-path hooks resolve correctly.
- **`schedule`:** a malformed `--cron`/`--marker` is a clean error (exit 2), not a traceback;
  empty markers are rejected.
- **`watch`:** `--max-runs 0` fires nothing; a non-positive `--poll-interval` is rejected.
- **orchestrate:** structurally-invalid plans (bad `version`, non-bool `fail_fast`, a stage
  with zero/two sources) fail as a "bad plan" (exit 2), not a stage failure (exit 1).
- **resume/status:** `pid_alive` treats a non-positive pid as dead (a corrupted heartbeat no
  longer blocks resume); the `blocked` message reports the consecutive-failure limit; the
  `require_clean_git` precondition message names the offending paths.
- **`run`:** `--set` without `--skill` is now an error (matching `--help`).
- **docs:** README skills example warns that `fix-until-tests-pass` launches a real billable
  agent (and shows a no-agent `shell-converge` demo); corrected the stale version string,
  the `schedule` dry-run description, and the `examples/plan.yaml` stage-graph comment.

## [0.3.0] - 2026-06-13

Grew the validated single-run core into a Loop Engineering **platform**. Every new
layer composes on the same `run_loop` contract; the runner, safety posture, and
existing `loop.yaml` are unchanged and backward-compatible.

### Added
- **Reusable skills** — parameterized `loop.yaml` templates with a `skill:` block.
  `loopeng skill list` / `skill show`, `loopeng run --skill <name> --set k=v`.
  The renderer substitutes only declared params, leaving `{{feedback}}`/`{{iteration}}`
  for the runner. Bundled: `fix-until-tests-pass`, `shell-converge`. Discovery order:
  project `.loopeng/skills/` > user `~/.loopeng/skills/` > bundled.
- **Worktree isolation** — `loopeng run --isolate` runs the loop in a throwaway git
  worktree off HEAD; your main working tree is never touched. On success the agent's
  edits are committed to a disposable `loop/<hex>` branch, the diff is surfaced, and
  the worktree removed (branch kept for `git merge`); discarded on failure.
- **Triggers / scheduling (daemonless)** — `loopeng watch --pattern <glob>` re-runs the
  loop on file changes (mtime-poll + debounce, self-write/.git/.loopeng ignored, exit
  130 on Ctrl-C); `loopeng schedule --cron <expr> --marker <id>` emits (or `--apply`
  installs) an idempotent crontab line.
- **Multi-agent orchestration** — `loopeng orchestrate plan.yaml` runs a DAG of stages,
  each a full loopeng loop (`spec:` / `skill:` / inline `loop:`), with `needs:`
  dependencies, parallel levels (thread pool), fail-fast, an orchestration ledger, and
  an optional whole-plan `workspace: worktree` isolation. Exit 0 all-passed, 1 any failed.
- **Lifecycle hooks / connectors** — a `hooks:` block (`on_start`/`on_iteration`/
  `on_success`/`on_failure`) shells out on loop events with `LOOPENG_*` env vars; a
  failing hook is isolated, never fatal.
- **Adapter plugins** — third parties register custom `agent.type` adapters via the
  `loopeng.adapters` entry-point group or `loopeng run --plugin <module-or-path>`.
  Entry-point plugins are failure-isolated; explicit `--plugin` is strict.
- **MCP server** — `loopeng mcp` speaks newline-delimited JSON-RPC 2.0 over stdio
  (MCP `2025-03-26`), exposing `loopeng_list_skills`, `loopeng_doctor`, `loopeng_status`,
  and `loopeng_run` so Claude Code / Codex can drive loopeng as an MCP server.

### Changed
- `agent.type` is now validated against the live adapter registry (built-ins + plugins)
  at `build_adapter` time instead of a frozen tuple at parse time, so plugin types are
  accepted. Unknown types still fail with a clear "unknown agent type" error.

### Hardened (pre-release adversarial review)
- **Orchestration + blast-radius**: a level containing any blast-radius-gated stage now
  runs serially. The gate reads tree-wide `git status`; concurrent stages in a shared
  work tree would otherwise see each other's writes (false violations / wrong attribution).
  Ungated levels still run in parallel.
- **`run --isolate` / orchestrate worktree mode**: `commit_all` no longer stages loopeng's
  own `.loopeng/` state, so the surfaced diff and the kept branch you merge contain only
  the agent's work — not run bookkeeping (pid/cwd/fingerprint).
- **`schedule`**: `--marker`/`--workdir` reject newlines and `--cron` must be exactly five
  fields, so a malformed value can't inject a second crontab line or break marker idempotency.
- **`--isolate` + `--resume`** is now rejected up front (the isolated ledger is ephemeral).
- **Hooks**: failed/timed-out hooks now surface as a `⚠` line in text mode (were silent).

## [0.2.0] - 2026-06-13

Hardened the supervised MVP core into a safer, observable, steerable runner.

### Added
- **Blast-radius controls** — a repository write-set gate (`require_clean_git`,
  `allowed_paths`, `forbidden_paths`, `max_changed_files`) checked from `git status`
  after each agent step. Not a sandbox.
- **CI** — GitHub Actions running the test suite on Python 3.9, 3.12, and 3.13.
- **Resume** — `loopeng run --resume` reconstructs the latest run from the ledger and
  continues (restores the iteration and consecutive-failure counters); refuses (exit 6)
  on a succeeded run (no override), or a blocked / no-progress / in-progress run or a
  changed spec fingerprint (those overridable with `--force`).
- **Heartbeat + status** — `.loopeng/heartbeat.json` per phase; `loopeng status [--json]`
  reports run state and staleness (pid-authoritative).
- **Typed events + `run --json`** — the runner emits typed event dicts; `--json` streams
  them as one JSON object per line.
- **Adapter preflight + `loopeng doctor`** — `claude-code`/`codex` resolve their binary
  (workspace-aware) before the loop; a missing binary fails fast (exit 7).
- **Stall / no-progress detection** — `no_output_timeout` kills a silently-hung agent;
  `no_progress_limit` stops on consecutive identical-feedback failures (exit 8).
- **Metric/baseline verification gate** — `verify.baseline {metric, regex, direction, value}`
  requires the verifier to also clear a numeric threshold.
- **Context discipline** — `limits.context_max_chars` caps each context output;
  per-context `cache: true` runs a command once and reuses its output.
- **Mid-run steering** — `loopeng run --reload-spec` hot-reloads the prompt from `loop.yaml`
  each iteration.

### Changed
- Exit codes: `0` success, `2` spec/adapter error, `3` blocked, `4` exhausted,
  `5` precondition failed, `6` resume refused, `7` adapter preflight failed, `8` no progress.
- `run_proc` turns timeouts, missing binaries, and non-executable binaries into typed
  exit codes (124/127/126) instead of exceptions; the spec fingerprint ignores unset
  optional fields so adding one doesn't invalidate resume.

### Fixed
- **`codex` preset `approval_mode`** — the Codex CLI removed `exec --ask-for-approval`,
  so `capabilities: {approval_mode: …}` made the agent exit 2 (`unexpected argument`)
  before doing any work. The approval policy is now set via the stable
  `-c approval_policy=<value>` config override, so the preset works on current Codex CLI
  (validated end-to-end against codex-cli 0.137.0).

## [0.1.0] - 2026-06-13

### Added
- Initial agent-agnostic Loop Engineering runner: a portable `loop.yaml` spec, the generic
  shell/mock adapter (with thin `claude-code`/`codex` presets), a deterministic verification
  gate, three bounded stop conditions (max-iterations, consecutive-failure circuit breaker,
  per-command timeout), and an append-only JSONL ledger. `loopeng init` / `loopeng run`.
