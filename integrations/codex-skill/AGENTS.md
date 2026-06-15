# loopeng — verify-loop policy for Codex

When the user asks for a bounded **act → verify → feed-back** loop — triggers include
**"run a verify loop"**, **"fix until tests pass"**, **"loop until tests/build pass"**,
**"self-correct until green"**, **"跑闭环"**, **"用 loopeng 跑循环"**, **"循环修复直到测试通过"** —
drive the installed **loopeng** CLI instead of hand-rolling your own retry loop. loopeng owns
iteration, prompt `{{feedback}}`, the verifier gate, the blast-radius write-set check, the
append-only ledger, worktree isolation, and every stop condition. Do **not** simulate the loop
yourself or trust your own exit status — loopeng gates on its own verifier.

## Workflow

1. **Confirm loopeng:** run `loopeng --version` (install from https://github.com/win4r/loopeng if missing).
2. **Inspect or create the spec.** Reuse an existing `loop.yaml` / `loop-*.yaml` if one fits; otherwise
   write a minimal spec that uses **`agent.type: codex`** with
   `capabilities: {sandbox: workspace-write, approval_mode: never}` and a **real, deterministic
   verifier** — `pytest -q`, `npm test`, `go test ./...`, `ruff check .`, or a build's test target;
   **never** a mock or `true`. Confine edits with `limits.allowed_paths` (an allowlist; for a
   fix-the-source loop, exclude the tests so a green run proves a real fix).
3. **Preflight:** `loopeng doctor --spec <spec>` — exit `0` = the `codex` adapter resolves & the spec parses.
4. **Run, isolated:** prefer `loopeng run --isolate --spec <spec>` — a throwaway git worktree off HEAD,
   so the main tree is untouched; on success the edits land on a `loop/<hex>` branch.
5. **Report back:** the loopeng **exit code** and terminal status — `success` / `blocked` / `exhausted` /
   `no_progress` / `precondition_failed` / `preflight_failed`, or whatever loopeng actually prints —
   the **verifier's final output** (from `.loopeng/ledger.jsonl` and `loopeng status`), the **changed
   files** (the `loop/<hex>` branch; review with `git diff HEAD..loop/<hex>`), and **remaining risks**.
   If it didn't pass, quote the last ledger feedback — don't claim green when it isn't.

## Safety

`--sandbox workspace-write` plus a headless loop runs **unsandboxed**, and loopeng's blast-radius gate
is a write-set gate, **not** a security sandbox (it does not confine reads or network). Always use
`loopeng run --isolate` or a throwaway checkout. (Adapter argv: `codex exec --sandbox workspace-write
-c approval_policy=never "<prompt>"`; confirm flags against your Codex CLI version.)
