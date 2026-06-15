# loopeng examples

Runnable artifacts for the v0.3.0 platform layers. All are local, explicit, and
safe-by-default — they shell out only to the commands written here.

| File | Layer | Try it |
|---|---|---|
| `plan.yaml` | multi-stage orchestration | `cd examples && loopeng orchestrate --plan plan.yaml` (shell-only, no agent CLI needed) |
| `reverse_echo_plugin.py` | adapter plugin | `loopeng run --plugin ./examples/reverse_echo_plugin.py --spec your_loop.yaml` |
| `.mcp.json` | MCP server | copy to your project root; Claude Code will launch `loopeng mcp` as a local stdio server |
| `codex-cli-demo/` | Codex CLI agent | `cd examples/codex-cli-demo && loopeng doctor` (no login) then `loopeng run` (needs `codex` installed + logged in) |

Other layers need no example file:

```bash
# Reusable skills (bundled):
loopeng skill list
loopeng run --skill shell-converge --set agent_cmd="echo x >> p.txt" --set verify_cmd="test -s p.txt"  # pure shell, no agent
loopeng run --skill fix-until-tests-pass --set test_cmd="pytest -q"   # ⚠ launches a real billable claude agent that edits files

# Worktree isolation (inside a git repo): run in a throwaway checkout, main tree untouched
loopeng run --isolate

# Daemonless triggers:
loopeng watch --pattern "src/**/*.py"
loopeng schedule --cron "*/30 * * * *" --marker nightly        # dry-run; add --apply to install

# Lifecycle hooks: add a `hooks:` block to loop.yaml, e.g.
#   hooks: { on_success: ["echo done $LOOPENG_STATUS"] }
```

See the top-level [README](../README.md#platform-layers-v030) for the full reference
and the safety model.
