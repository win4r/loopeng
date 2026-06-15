---
name: loopeng
description: "Drive a real act → verify → feed-back agent loop with the installed loopeng CLI (the runtime — orchestrate it, don't reimplement it). Create or inspect a loop.yaml, run a coding agent (claude-code / codex / shell) against a DETERMINISTIC verifier (pytest, xcodebuild test, npm test, ruff, ...), and let loopeng own iteration, gating, isolation, the ledger, and stop conditions. Use when the user wants to iterate an agent until a build/test passes, run a self-correcting fix loop, add a small feature behind a test gate, or says 'use loopeng', 'loop until tests pass', 'run a verify loop', or 'fix this failing build/test autonomously'. Prefer --isolate."
user-invocable: true
argument-hint: "<task to loop on, or path to a loop.yaml>"
---

# /loopeng — drive the loopeng CLI as your loop runtime

loopeng is the runtime. **You orchestrate it; you do NOT reimplement it.** Never hand-roll a
bash `while` loop, re-run an agent yourself, or simulate iterations — call `loopeng run`. loopeng
owns the loop: prompt rendering + `{{feedback}}`, the verifier gate, the blast-radius write-set
check, the append-only ledger, worktree isolation, and every stop condition. loopeng's own
`--help` and its README are the source of truth for flags/schema — consult them rather than
guessing; this skill is the workflow + the gotchas.

## 0. Use the installed `loopeng` CLI

```bash
loopeng --version          # expect: loopeng 0.3.3 (or newer)
```

If `loopeng` isn't found, install it (see loopeng's README → *Install*: from a GitHub release or
`pip install .` from source — it is **not on PyPI**). This skill *uses* that CLI; it does not
replace it.

## 1. Workflow (per task)

1. **Inspect or write the spec.** If a `loop.yaml` (or `loop-*.yaml`, or a `.loopeng/skills/`
   template) already fits, reuse/adapt it. Otherwise write a minimal spec (§2). Keep it in the repo.
2. **Preflight:** `loopeng doctor --spec <spec>` — exit 0 = adapter binary resolves & spec parses.
   Fix the spec (exit 2) or install the agent CLI (exit 7) before running.
3. **Run, isolated by default:** `cd <project> && loopeng run --isolate --spec <spec>`.
   `--isolate` runs in a throwaway git worktree off `HEAD`; your main tree is never touched. On
   success the agent's edits land on a `loop/<hex>` branch (diff surfaced; `git merge` to keep);
   on failure everything is discarded. (Requires a git repo with ≥1 commit.)
4. **Read the result** (§4): exit code, the verifier's final output, the ledger, `status`.
5. **Report** (§5): exit code, what the verifier said, the branch/commit, and remaining risks.

Run a **real coding agent only when the user wants autonomous edits** (it's billable and edits
files). For dry mechanics, use the shell agent (§6 smoke run, free).

## 2. Spec essentials (`loop.yaml`)

```yaml
objective: "one line: what done looks like"
workspace: "."
agent:
  type: claude-code                 # shell | mock | claude-code | codex
  command: ["claude", "-p", "--dangerously-skip-permissions"]   # see GOTCHA below
prompt: |
  <task>. Run `pytest -q` to check. Fix the real source.
  Verifier feedback from the previous attempt (empty on iteration 1):
  {{feedback}}
verify:
  command: ["pytest", "-q"]                 # MUST be deterministic; exit 0 = pass
limits:
  max_iterations: 6
  max_consecutive_failures: 4
  timeout_seconds: 900                      # generous: agent step + a cold build
  allowed_paths: ["src/**", "tests/**"]     # blast-radius: see §3
```

- **Real verifiers only.** `pytest -q`, `npm test`, `go test ./...`, `ruff check .`, or a script
  that runs `xcodegen generate && xcodebuild … test`. Optional numeric gate: `verify.baseline`
  (e.g. coverage ≥ 90).
- Full schema (context, baseline, hooks, YAML skills, plan.yaml): loopeng's README.

## 3. Safety & gotchas (load-bearing — get these right)

- **Not a security sandbox; reads are NOT confined.** The blast-radius gate constrains the git
  **write-set** only — it does not stop network, reads, or writes outside the repo. A headless
  `claude -p --dangerously-skip-permissions` agent runs **unsandboxed**. So: always `--isolate`,
  or run on a throwaway branch/checkout. Don't point it at a tree with secrets you can't lose.
- **Letting the agent edit:** the bare `claude-code`/`codex` presets resolve the CLI but usually
  can't modify files. Grant it: claude → `command: ["claude","-p","--dangerously-skip-permissions"]`
  (a hands-off headless loop needs this — the agent also runs its own shell/verifier, which the
  lower-privilege `capabilities: {approval_mode: acceptEdits}` would stall on); codex →
  `capabilities: {sandbox: workspace-write}`.
- **Anti-cheat = mechanical, not a prompt.** A test-running verifier is gameable (the agent can
  "fix" a failing test by editing the test). Use `limits.allowed_paths` (an allowlist is strictly
  stronger than a `forbidden_paths` denylist); the gate runs **before** verify, so an out-of-bounds
  edit fails the run. For a *fix-the-source* loop, exclude the tests (`allowed_paths: ["src/**"]`).
  For an *add-a-feature* loop, allow tests too (the agent should write them).
- **blast-radius keys go UNDER `limits:`** — a *top-level* `blast_radius:`/`allowed_paths:`/
  `forbidden_paths:`/`max_changed_files:`/`require_clean_git:` is rejected with a `SpecError` (since
  0.3.3). (Nesting one under the wrong block — e.g. `verify:` — can still silently no-op, so put them
  directly under `limits:` and confirm with `doctor`.)
- **`--isolate` ledger is ephemeral** (it lives in the worktree and is removed with it). If you
  need to inspect the ledger/feedback afterward, run **in-tree on a throwaway branch**
  (`git checkout -b loop-run && loopeng run --spec ...`, then `git checkout -` + delete the branch).
- **Verifier feedback should lead with the failure.** Tools like `xcodebuild` print failures at the
  END, and loopeng head-truncates ledger feedback (~800 chars); a `tail`-only wrapper buries the
  assertion. Have your verifier script print the failing `error:` / assertion lines FIRST.

## 4. Reading results

- **Exit codes:** `0` success · `2` spec/adapter error · `3` blocked (consecutive failures) ·
  `4` exhausted (max_iterations) · `5` precondition failed (dirty tree + `require_clean_git`) ·
  `6` resume refused · `7` adapter preflight failed · `8` no_progress (identical feedback — only when
  `limits.no_progress_limit` is set). A hung agent/verifier hits `command_timeout` (exit 124 for
  that step) and just fails the iteration.
- **Ledger:** `.loopeng/ledger.jsonl` — one JSON record per iteration (`result`, `verify_exit`,
  `blast_radius.changed_paths`, `feedback`) + a `run_end`. Read the last few lines to see what the
  agent changed and why each iteration passed/failed.
- **Live state:** `loopeng status [--json]` (run_id, phase, iteration, `stale`). Resume an
  interrupted/exhausted run: `loopeng run --resume` (`--force` to override blocked/no_progress
  or a changed spec).

## 5. Report back

State plainly: **status + exit code** (e.g. `success (0)` / `exhausted (4)`), the **verifier's
final output** (the passing line, or the assertion that still fails), the **branch/commit** the
edits landed on (for `--isolate`: the `loop/<hex>` branch; `git diff HEAD..loop/<hex>` to review),
and **remaining risks** (was it `--isolate`? did the allowlist hold? any flaky/timeout? unverified
edge cases?). If it failed, quote the last ledger feedback — don't claim green when it isn't.

## 6. Examples

**Safe smoke run (no API key, nothing billable) — proves the runtime works:**
```bash
cd "$(mktemp -d)" && loopeng init && loopeng run     # scaffolded shell loop: fails once, self-corrects, passes (exit 0)
```
> A bare `mktemp` dir isn't a git repo, so loopeng **skips** the blast-radius gate (it warns; the
> loop still works). `git init -q . && git commit --allow-empty -qm init` first to watch the gate fire.

**Add a small feature via a claude-code agent loop, gated by the test suite:**
```bash
cd <your-project>
cat > loop-feature.yaml <<'YAML'
objective: "Add <feature> with a focused test; keep the suite green"
workspace: "."
agent:
  type: claude-code
  command: ["claude", "-p", "--dangerously-skip-permissions"]   # UNSANDBOXED — run via --isolate
prompt: |
  Implement <feature> in the source, and add a focused test that proves it. Run `pytest -q`
  to verify. Edit only files under src/ and tests/. Make the smallest correct change, then stop.
  Verifier feedback from the previous attempt (empty on iteration 1):
  {{feedback}}
verify:
  command: ["pytest", "-q"]
limits:
  max_iterations: 6
  max_consecutive_failures: 4
  timeout_seconds: 900
  allowed_paths:                 # a feature legitimately touches BOTH source and tests
    - "src/**"
    - "tests/**"
YAML
loopeng doctor --spec loop-feature.yaml          # confirm `claude` resolves
loopeng run --isolate --spec loop-feature.yaml   # runs in a worktree; main untouched
# on success: review `git diff HEAD..loop/<hex>`, then `git merge loop/<hex>` to keep it.
```

**Other stacks:** swap `verify` + `allowed_paths` for the project — e.g. `npm test` / `go test ./...`,
or for iOS a script that runs `xcodegen generate && xcodebuild … test` (printing the failing
`error:`/`XCTAssert` lines FIRST). **Fix-without-cheating:** same shape but exclude the tests from
`allowed_paths` (e.g. `["src/**"]`), so a green run proves a real source fix.

> SECURITY recap: these run a headless coding agent **unsandboxed** in the workspace. Always
> `--isolate` (or a throwaway checkout).
