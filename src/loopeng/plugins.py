"""Third-party adapter plugins: register custom agent types without forking loopeng.

A *plugin* is a Python module exposing a module-level ``register(registry)`` that
mutates the adapter registry — ``registry["<type>"] = builder`` — where ``builder``
is a ``function(AgentSpec) -> ShellAdapter`` (the same shape as the built-in
``_BUILDERS`` values in :mod:`loopeng.adapters`). After registration, a loop.yaml
``agent.type: <type>`` resolves through ``build_adapter`` to that builder.

Two discovery paths, with opposite failure policies:

  * **Entry points** (``[project.entry-points."loopeng.adapters"]``) are loaded
    automatically and are FAILURE-ISOLATED: one broken third-party package must
    never stop loopeng from starting, so each failure is captured as a warning
    string and the remaining plugins still load.
  * **``--plugin <module-or-path>``** is loaded on EXPLICIT request and is STRICT:
    the user named it, so a missing module/file or a missing ``register`` is a
    hard :class:`PluginError`.

Overwriting an already-registered type (built-in or earlier plugin) is allowed but
returns a warning, so a silent shadow of ``claude-code``/``codex`` is at least visible.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from typing import Callable, Dict, Iterable, List, Optional

from .errors import PluginError

ENTRY_POINT_GROUP = "loopeng.adapters"

# A registry maps an agent.type string to a builder(AgentSpec) -> ShellAdapter.
Registry = Dict[str, Callable]


def _iter_entry_points(group: str) -> Iterable:
    """Yield the entry points advertised under ``group``, across Python versions.

    Python 3.9's ``importlib.metadata.entry_points()`` returns a dict keyed by
    group (``.get(group, [])``); 3.10+ deprecated that and added the
    ``entry_points(group=...)`` selectable form. Guard on the interpreter version
    so neither path triggers the other's DeprecationWarning.
    """
    from importlib.metadata import entry_points

    if sys.version_info >= (3, 10):
        return entry_points(group=group)
    return entry_points().get(group, [])  # pragma: no cover - exercised on py3.9 CI


def _rebound_keys(before: Dict[str, Callable], after: Registry) -> List[str]:
    """Keys whose builder a plugin *changed* (rebound), not merely left in place.

    A pre-existing type the plugin never touched (same object) is not a rebind, so
    adding a brand-new type to a registry that already holds ``shell``/``codex``
    raises no spurious overwrite warning.
    """
    return sorted(
        key
        for key in after
        if key in before and after[key] is not before[key]
    )


def load_entry_point_plugins(registry: Registry) -> List[str]:
    """Discover and load every ``loopeng.adapters`` entry point. FAILURE-ISOLATED.

    Each entry point is expected to ``ep.load()`` to a callable taking the
    registry (the package's ``register`` function). A broken entry point — one
    that fails to import, doesn't load to a callable, or raises while registering —
    is caught and appended as a warning string; it never propagates, and the
    remaining entry points still load. Returns the list of warning strings (empty
    when everything loaded cleanly).
    """
    warnings: List[str] = []
    for ep in _iter_entry_points(ENTRY_POINT_GROUP):
        name = getattr(ep, "name", "<unknown>")
        try:
            register = ep.load()
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point
            warnings.append(f"entry point {name!r} failed to load: {exc}")
            continue
        if not callable(register):
            warnings.append(
                f"entry point {name!r} did not resolve to a callable register(registry)"
            )
            continue
        try:
            before = dict(registry)
            register(registry)
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop the run
            warnings.append(f"entry point {name!r} raised while registering: {exc}")
            continue
        for key in _rebound_keys(before, registry):
            warnings.append(
                f"entry point {name!r}: overwrote already-registered agent type {key!r}"
            )
    return warnings


def _load_module_from_path(module_spec: str):
    """Import a plugin module from a filesystem path under a unique synthetic name.

    A unique module name (derived from the path) keeps two ``--plugin`` files from
    colliding in ``sys.modules`` and lets the same path be (re)loaded deterministically
    in tests without a stale cache entry shadowing a fresh load.
    """
    unique_name = f"_loopeng_plugin_{abs(hash(module_spec))}"
    spec = importlib.util.spec_from_file_location(unique_name, module_spec)
    if spec is None or spec.loader is None:
        raise PluginError(f"could not load plugin from path {module_spec!r}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so a plugin that imports itself by name resolves.
    sys.modules[unique_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - re-wrapped as the typed error below
        sys.modules.pop(unique_name, None)
        raise PluginError(f"plugin at {module_spec!r} failed to import: {exc}") from exc
    return module


def load_explicit_plugin(module_spec: str, registry: Registry) -> None:
    """Load a single user-named plugin and run its ``register(registry)``. STRICT.

    ``module_spec`` is either a filesystem path (contains ``os.sep`` or ends in
    ``.py``) loaded via ``importlib.util.spec_from_file_location``, or an importable
    dotted module name loaded via ``importlib.import_module``. Because the user
    explicitly asked for this plugin, anything wrong with it is a hard error:

      * the module can't be imported  -> :class:`PluginError`
      * the module has no callable ``register`` -> :class:`PluginError`

    Mutates ``registry`` in place. A plugin that rebinds an existing agent type is
    allowed (no error here); use :func:`load_entry_point_plugins` semantics or
    inspect the registry yourself if you need to detect that for explicit loads.
    """
    is_path = (os.sep in module_spec) or (
        os.altsep is not None and os.altsep in module_spec
    ) or module_spec.endswith(".py")

    if is_path:
        module = _load_module_from_path(module_spec)
    else:
        try:
            module = importlib.import_module(module_spec)
        except Exception as exc:  # noqa: BLE001 - re-wrapped as the typed error
            raise PluginError(
                f"could not import plugin module {module_spec!r}: {exc}"
            ) from exc

    register = getattr(module, "register", None)
    if not callable(register):
        raise PluginError(
            f"plugin {module_spec!r} has no callable module-level register(registry)"
        )
    register(registry)


def load_plugins(
    registry: Registry,
    *,
    explicit: Optional[Iterable[str]] = None,
    use_entry_points: bool = True,
) -> List[str]:
    """Convenience: load entry-point plugins (isolated) then explicit ones (strict).

    Returns the accumulated warnings from the entry-point pass. Explicit plugins
    are loaded after, so a ``--plugin`` can deliberately override an entry point.
    A failing explicit plugin raises :class:`PluginError` (it is not isolated).
    """
    warnings: List[str] = []
    if use_entry_points:
        warnings.extend(load_entry_point_plugins(registry))
    for module_spec in explicit or []:
        load_explicit_plugin(module_spec, registry)
    return warnings
