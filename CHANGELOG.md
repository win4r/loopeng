# Changelog

All notable changes to loopeng are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may change behavior).

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
