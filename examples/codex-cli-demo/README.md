# Codex CLI demo

Drives the **Codex CLI** (`codex exec`) through loopeng against a **deterministic verifier** —
`agent.type: codex` with `sandbox: workspace-write` (and `approval_mode: never` for a fully
non-interactive run). loopeng owns the loop; the verifier is the gate.

The spec ([`loop.yaml`](loop.yaml)) builds this argv each iteration:

```
codex exec --sandbox workspace-write -c approval_policy=never "<prompt>"
```

The task is trivial and the gate is deterministic: write `DONE` into `output.txt`; [`verify.py`](verify.py)
exits `0` iff `output.txt` contains `DONE`, so loopeng iterates the agent until the gate passes.

## Run it

Requires the [Codex CLI](https://github.com/openai/codex) **installed and logged in**.

```bash
cd examples/codex-cli-demo
loopeng doctor        # preflight: resolves `codex` on PATH (NO login needed) — exit 0 = ready, 7 = missing
loopeng run           # real, billable Codex call; iterates until output.txt contains DONE
cat .loopeng/ledger.jsonl
```

`doctor` works without a Codex login (it only resolves the binary); `loopeng run` performs a real
Codex call, so it needs a working login. `output.txt` and `.loopeng/` are generated (gitignored here).

## Notes

- **`--sandbox workspace-write`** lets `codex exec` edit files in the workspace. loopeng's
  blast-radius write-set gate is **not** a security sandbox — run on a throwaway checkout or
  `--isolate`. See the top-level [README → Safety model](../../README.md#safety-model) and
  [Codex CLI](../../README.md#codex-cli).
- The argv mapping is asserted by `tests/test_codex_example.py` so this example can't drift from
  the adapter — and that test needs **no Codex login**. A real end-to-end run is **opt-in only**:
  `LOOPENG_CODEX_SMOKE=1 pytest tests/test_codex_example.py` (needs `codex` installed + logged in).
