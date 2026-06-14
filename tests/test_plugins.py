"""Third-party adapter plugins: entry-point discovery (isolated) + explicit load (strict).

Entry points are faked by patching ``loopeng.plugins._iter_entry_points`` with
lightweight fakes, so no real distribution metadata is required (deterministic,
offline). Module/path loads use ``tmp_path`` modules made importable via
``monkeypatch.syspath_prepend``.
"""

import textwrap
from unittest import mock

import pytest

from loopeng.adapters import ShellAdapter
from loopeng.errors import PluginError
from loopeng.plugins import (
    ENTRY_POINT_GROUP,
    load_entry_point_plugins,
    load_explicit_plugin,
    load_plugins,
)
from loopeng.spec import AgentSpec


# --- fakes -----------------------------------------------------------------


class _FakeEP:
    """Stands in for an importlib.metadata EntryPoint: has .name and .load()."""

    def __init__(self, name, load_result=None, load_error=None):
        self.name = name
        self._load_result = load_result
        self._load_error = load_error

    def load(self):
        if self._load_error is not None:
            raise self._load_error
        return self._load_result


def _reverse_echo_builder(agent):
    """A custom builder: type 'reverse-echo' -> a ShellAdapter echoing reversed text."""
    return ShellAdapter(["sh", "-lc", "rev"], name="reverse-echo")


def _register_reverse_echo(registry):
    registry["reverse-echo"] = _reverse_echo_builder


def _patch_entry_points(eps):
    """Patch the version-guarded entry-point iterator to yield our fakes."""
    return mock.patch("loopeng.plugins._iter_entry_points", return_value=list(eps))


# --- a fresh registry like loopeng.adapters._BUILDERS ----------------------


def _seed_registry():
    """A registry pre-seeded with built-in-like keys, to test overwrite semantics."""
    builtin_shell = lambda agent: ShellAdapter(["true"], name="shell")  # noqa: E731
    builtin_codex = lambda agent: ShellAdapter(["codex", "exec"], name="codex")  # noqa: E731
    return {"shell": builtin_shell, "codex": builtin_codex}


# --- entry-point path ------------------------------------------------------


def test_entry_point_registers_reverse_echo_builder():
    registry = {}
    ep = _FakeEP("reverse-echo", load_result=_register_reverse_echo)
    with _patch_entry_points([ep]):
        warnings = load_entry_point_plugins(registry)
    assert warnings == []
    assert "reverse-echo" in registry
    adapter = registry["reverse-echo"](AgentSpec(type="reverse-echo"))
    assert adapter.name == "reverse-echo"
    assert adapter.build_command("P") == ["sh", "-lc", "rev"]


def test_broken_entry_point_is_isolated_and_other_plugins_still_load():
    # One ep raises on load(); a second ep registers fine. The broken one must be
    # captured as a warning (not raised), and the good one must still be applied.
    bad = _FakeEP("kaboom", load_error=RuntimeError("boom on import"))
    good = _FakeEP("reverse-echo", load_result=_register_reverse_echo)
    registry = {}
    with _patch_entry_points([bad, good]):
        warnings = load_entry_point_plugins(registry)
    assert "reverse-echo" in registry  # the healthy plugin loaded
    assert len(warnings) == 1
    assert "kaboom" in warnings[0] and "boom on import" in warnings[0]


def test_entry_point_that_raises_while_registering_is_isolated():
    def _explodes(registry):
        raise ValueError("register blew up")

    bad = _FakeEP("bad-register", load_result=_explodes)
    good = _FakeEP("reverse-echo", load_result=_register_reverse_echo)
    registry = {}
    with _patch_entry_points([bad, good]):
        warnings = load_entry_point_plugins(registry)
    assert "reverse-echo" in registry
    assert any("bad-register" in w and "register blew up" in w for w in warnings)


def test_entry_point_not_callable_is_isolated():
    not_callable = _FakeEP("typo", load_result={"not": "callable"})
    registry = {}
    with _patch_entry_points([not_callable]):
        warnings = load_entry_point_plugins(registry)
    assert registry == {}
    assert len(warnings) == 1
    assert "typo" in warnings[0] and "callable" in warnings[0]


def test_entry_point_overwriting_existing_type_warns():
    def _shadow_codex(registry):
        registry["codex"] = lambda agent: ShellAdapter(["custom-codex"], name="codex")

    ep = _FakeEP("shadow", load_result=_shadow_codex)
    registry = _seed_registry()
    with _patch_entry_points([ep]):
        warnings = load_entry_point_plugins(registry)
    assert any("overwrote" in w and "codex" in w for w in warnings)


def test_entry_point_adding_new_type_does_not_warn_about_untouched_builtins():
    # A plugin that only ADDS a new type must not spuriously warn about the
    # pre-existing shell/codex keys it never touched (value-identity check).
    ep = _FakeEP("reverse-echo", load_result=_register_reverse_echo)
    registry = _seed_registry()
    with _patch_entry_points([ep]):
        warnings = load_entry_point_plugins(registry)
    assert warnings == []
    assert "reverse-echo" in registry
    assert set(registry) == {"shell", "codex", "reverse-echo"}


def test_no_entry_points_returns_empty():
    registry = {}
    with _patch_entry_points([]):
        assert load_entry_point_plugins(registry) == []
    assert registry == {}


def test_iter_entry_points_is_called_with_the_group():
    # Pin the contract: discovery queries the 'loopeng.adapters' group.
    registry = {}
    with mock.patch(
        "loopeng.plugins._iter_entry_points", return_value=[]
    ) as iter_mock:
        load_entry_point_plugins(registry)
    iter_mock.assert_called_once_with(ENTRY_POINT_GROUP)


# --- explicit dotted-module path -------------------------------------------


_PLUGIN_SRC = textwrap.dedent(
    """
    def _builder(agent):
        import loopeng.adapters as a
        return a.ShellAdapter(["echo", "from-plugin"], name="myplugin")

    def register(registry):
        registry["myplugin"] = _builder
    """
)


def _write_plugin(dir_path, module_name, src=_PLUGIN_SRC):
    path = dir_path / f"{module_name}.py"
    path.write_text(src, encoding="utf-8")
    return path


def test_explicit_dotted_module_registers(tmp_path, monkeypatch):
    _write_plugin(tmp_path, "myplug_dotted")
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = {}
    load_explicit_plugin("myplug_dotted", registry)
    assert "myplugin" in registry
    adapter = registry["myplugin"](AgentSpec(type="myplugin"))
    assert adapter.build_command("P") == ["echo", "from-plugin"]


def test_explicit_file_path_registers(tmp_path):
    # A path (contains os.sep / ends in .py) loads via spec_from_file_location,
    # so it works without being on sys.path at all.
    path = _write_plugin(tmp_path, "myplug_bypath")
    registry = {}
    load_explicit_plugin(str(path), registry)
    assert "myplugin" in registry
    assert registry["myplugin"](AgentSpec(type="myplugin")).name == "myplugin"


def test_explicit_file_path_unique_module_name_allows_two_loads(tmp_path):
    # Two different files both define register(); the unique synthetic module name
    # must keep them from colliding in sys.modules.
    a = _write_plugin(
        tmp_path,
        "plug_a",
        textwrap.dedent(
            """
            def register(registry):
                registry["a-type"] = lambda agent: None
            """
        ),
    )
    b = _write_plugin(
        tmp_path,
        "plug_b",
        textwrap.dedent(
            """
            def register(registry):
                registry["b-type"] = lambda agent: None
            """
        ),
    )
    registry = {}
    load_explicit_plugin(str(a), registry)
    load_explicit_plugin(str(b), registry)
    assert "a-type" in registry and "b-type" in registry


# --- explicit strict-failure cases -----------------------------------------


def test_explicit_module_missing_register_raises(tmp_path, monkeypatch):
    _write_plugin(
        tmp_path,
        "noregister",
        "BUILDER = lambda agent: None  # module exists but has no register()\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = {}
    with pytest.raises(PluginError, match="register"):
        load_explicit_plugin("noregister", registry)


def test_explicit_register_not_callable_raises(tmp_path, monkeypatch):
    _write_plugin(tmp_path, "register_not_callable", "register = 123\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = {}
    with pytest.raises(PluginError, match="register"):
        load_explicit_plugin("register_not_callable", registry)


def test_explicit_nonexistent_dotted_module_raises():
    registry = {}
    with pytest.raises(PluginError, match="import"):
        load_explicit_plugin("loopeng_no_such_plugin_xyz", registry)


def test_explicit_nonexistent_file_path_raises(tmp_path):
    missing = tmp_path / "does_not_exist.py"
    registry = {}
    with pytest.raises(PluginError):
        load_explicit_plugin(str(missing), registry)


def test_explicit_plugin_with_syntax_error_raises_pluginerror(tmp_path):
    path = _write_plugin(tmp_path, "broken_syntax", "def register(registry)\n    pass\n")
    registry = {}
    with pytest.raises(PluginError, match="import"):
        load_explicit_plugin(str(path), registry)


# --- the convenience aggregator --------------------------------------------


def test_load_plugins_runs_entry_points_then_explicit(tmp_path):
    path = _write_plugin(tmp_path, "agg_plugin")
    ep = _FakeEP("reverse-echo", load_result=_register_reverse_echo)
    registry = {}
    with _patch_entry_points([ep]):
        warnings = load_plugins(registry, explicit=[str(path)])
    assert warnings == []
    assert "reverse-echo" in registry  # from entry point
    assert "myplugin" in registry  # from explicit


def test_load_plugins_explicit_failure_is_not_isolated(tmp_path):
    # Explicit plugins are strict even when routed through the aggregator.
    registry = {}
    with _patch_entry_points([]):
        with pytest.raises(PluginError):
            load_plugins(registry, explicit=["loopeng_no_such_module_qqq"])


def test_load_plugins_can_skip_entry_points(tmp_path):
    path = _write_plugin(tmp_path, "skip_ep_plugin")
    registry = {}
    # _iter_entry_points must not even be consulted when use_entry_points=False.
    with mock.patch("loopeng.plugins._iter_entry_points") as iter_mock:
        warnings = load_plugins(registry, explicit=[str(path)], use_entry_points=False)
    iter_mock.assert_not_called()
    assert warnings == []
    assert "myplugin" in registry
