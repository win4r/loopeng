"""A minimal loopeng adapter plugin.

A plugin is ordinary local Python you choose to load. It exposes a module-level
``register(registry)`` that maps a new ``agent.type`` to a builder
``fn(AgentSpec) -> ShellAdapter``. Load it explicitly:

    loopeng run --plugin ./examples/reverse_echo_plugin.py

or ship it from an installed package via the entry-point group:

    [project.entry-points."loopeng.adapters"]
    reverse-echo = "your_pkg.reverse_echo_plugin:register"

This sample registers a ``reverse-echo`` agent that just reverses each input
line with the standard ``rev`` tool — a stand-in for any custom CLI agent.
"""

from loopeng.adapters import ShellAdapter


def _build(agent):
    # `agent` is the parsed AgentSpec; honor any explicit command override.
    command = agent.command or ["rev"]
    return ShellAdapter(command, env=agent.env, name="reverse-echo")


def register(registry):
    registry["reverse-echo"] = _build
