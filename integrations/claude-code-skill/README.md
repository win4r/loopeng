# loopeng — Claude Code skill

A generic [Claude Code](https://claude.com/claude-code) **skill** ([`SKILL.md`](SKILL.md)) that
teaches Claude Code to drive the [`loopeng`](../../README.md) CLI as an `act → verify → feed-back`
agent-loop runtime — *use* loopeng, don't reimplement it. It encodes the per-task workflow
(inspect/create a `loop.yaml` → `doctor` → `run --isolate` → read the ledger/`status` → report),
the load-bearing safety gotchas, and the reporting discipline; it does **not** re-document every
flag (loopeng's `--help` and README are the contract).

## Install

Claude Code loads skills from `~/.claude/skills/<name>/SKILL.md`. Copy this skill's directory in:

```bash
# from a loopeng checkout:
mkdir -p ~/.claude/skills/loopeng
cp integrations/claude-code-skill/SKILL.md ~/.claude/skills/loopeng/SKILL.md

# or symlink it so it tracks the repo:
ln -s "$PWD/integrations/claude-code-skill/SKILL.md" ~/.claude/skills/loopeng/SKILL.md
```

It requires the `loopeng` CLI to be installed (see the top-level [README → Install](../../README.md#install)).
Claude Code discovers the skill automatically (it appears in the available-skills list); invoke it
with `/loopeng <task>` or just describe a loop task ("loop until the tests pass").

## How this relates to loopeng's other integration points

This Claude Code skill is **one of three** ways loopeng meets an agent — it complements, and does
not replace, *loopeng YAML skills* and the *`loopeng mcp`* server. See the top-level
[README → Driving loopeng from an agent](../../README.md#driving-loopeng-from-an-agent-three-layers)
for the full comparison.
