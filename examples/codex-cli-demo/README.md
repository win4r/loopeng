# Codex CLI dogfood

Drives the **Codex CLI** (`codex exec`) through loopeng with loopeng's full safety posture:
**`--isolate`** (a throwaway git worktree off `HEAD`), a **blast-radius allow-list**, and a
**real, deterministic verifier**. The task is a genuine fix-the-code loop: `greeting()` in
[`greeting.py`](greeting.py) is wrong, and [`verify.py`](verify.py) imports it, runs it, and
exits `0` only when it returns `Hello, loopeng!`.

The spec ([`loop.yaml`](loop.yaml)) builds this argv each iteration:

```
codex exec --sandbox workspace-write -c approval_policy=never "<prompt>"
```

`limits.allowed_paths: ["greeting.py"]` confines the agent to the source file — it may **not** edit
`verify.py` (the gate) or anything else; the write-set gate runs **before** the verifier, so an
out-of-bounds edit fails the iteration with `blast_radius_violation`. A green run is therefore a
**real** source fix, not a gamed verifier.

Requires the [Codex CLI](https://github.com/openai/codex) **installed and logged in**. `loopeng
doctor` resolves `codex` on PATH with **no login** (`0` ready · `7` missing); `loopeng run` makes a
real, billable Codex call.

## Run it (isolated — recommended)

`--isolate` needs a git repo, so use a throwaway copy (keeps your checkouts untouched and makes the
agent's commit easy to inspect):

```bash
cp -r examples/codex-cli-demo /tmp/codex-demo && cd /tmp/codex-demo
git init -q && git add -A && git commit -qm init
loopeng doctor --spec loop.yaml                 # preflight (no login): exit 0 = codex resolves
loopeng run --isolate --spec loop.yaml          # runs in a worktree off HEAD; your tree is untouched
```

On success loopeng prints `status: success | iterations: N | run: <id>` and surfaces the agent's
edit as a `loop/<hex>` branch. **Evidence of what changed:**

```bash
git log --oneline -1 loop/<hex>                 # the agent's commit (hex from the run output)
git diff HEAD..loop/<hex>                        # exactly the greeting.py fix — nothing else (allowed_paths held)
git merge loop/<hex>                             # keep it, if you want
```

## Ledger & status evidence

`--isolate` runs in a worktree that is removed on success, so its `.loopeng/ledger.jsonl` is
ephemeral. To inspect the **persistent ledger + live status**, run in-tree on a throwaway branch:

```bash
cd /tmp/codex-demo && git checkout -q -b try
loopeng run --spec loop.yaml
cat .loopeng/ledger.jsonl                        # one JSON record per iteration: result, verify_exit,
                                                 # blast_radius.changed_paths (== ["greeting.py"]), feedback
loopeng status                                   # run_id, phase, iteration, stale
git checkout -q - && git branch -D try           # restore
```

Each ledger iteration shows `blast_radius.changed_paths` (the allow-list held), `verify_exit`
(`1` while `greeting()` is wrong, `0` once fixed), and the verifier `feedback` fed to the next turn.

## Notes

- **`--sandbox workspace-write`** lets `codex exec` edit files; loopeng's blast-radius gate is a
  *write-set* gate, **not** a security sandbox (it does not confine reads or network). Always
  `--isolate` or a throwaway checkout — see the top-level [README → Safety model](../../README.md#safety-model)
  and [Codex CLI](../../README.md#codex-cli).
- The example→argv mapping and the `allowed_paths`/verifier shape are covered by
  `tests/test_codex_example.py` (**no Codex login**); a real end-to-end run is **opt-in**
  (`LOOPENG_CODEX_SMOKE=1 pytest tests/test_codex_example.py`, needs `codex` logged in).
- loopeng accepts any deterministic verifier — swap `verify.py` for `pytest -q`, `npm test`, etc.
