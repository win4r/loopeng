"""Typed errors so callers (and tests) can distinguish failure classes."""


class LoopengError(Exception):
    """Base class for all loopeng errors."""


class SpecError(LoopengError):
    """The loop spec (loop.yaml) is missing, malformed, or invalid."""


class AdapterError(LoopengError):
    """An agent adapter could not be built or configured."""


class SkillError(LoopengError):
    """A reusable skill template is missing, malformed, or has unmet parameters."""


class OrchestrationError(LoopengError):
    """An orchestration plan is missing, malformed, or has an invalid stage graph."""


class PluginError(LoopengError):
    """A plugin module could not be imported or failed to register cleanly."""


class WorktreeError(LoopengError):
    """A git worktree could not be created, surfaced, or removed safely."""
