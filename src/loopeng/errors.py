"""Typed errors so callers (and tests) can distinguish failure classes."""


class LoopengError(Exception):
    """Base class for all loopeng errors."""


class SpecError(LoopengError):
    """The loop spec (loop.yaml) is missing, malformed, or invalid."""


class AdapterError(LoopengError):
    """An agent adapter could not be built or configured."""
