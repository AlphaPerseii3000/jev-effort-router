"""The plugin's `register(ctx)` surface, loaded from the repository root.

Hermes loads a directory plugin as the package ``hermes_plugins.<slug>`` and calls the
package's ``register(ctx)``. The plugin body must not import anything from this test tree, so
these tests import the plugin's sibling modules (``config``, ``grid``, ...) through the same
path insertion the plugin relies on: a directory plugin's modules are top-level siblings.

For the tests, the simplest faithful arrangement is to import each module by path with the
package prefix Hermes uses.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Import the plugin the way Hermes' loader does: a package rooted at the repo directory,
#: under ``hermes_plugins.<slug>``. `sys.path` gets the repo root so the plugin's own
#: ``from .config import ...`` relatives resolve inside the package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULE_NAME = "hermes_plugins_jev_router_registration_test"


def _load_plugin():
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def plugin():
    yield _load_plugin()


class Args:
    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_register_wires_the_declared_surface(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ctx = StubContext(config={}, state=StubState(tmp_path))

    plugin.register(ctx)

    assert "llm_request" in ctx.middleware
    assert set(ctx.tools) == {"jev_router_status", "jev_router_route"}
    assert all(entry["toolset"] == "jev-router" for entry in ctx.tools.values())
    assert set(ctx.hooks) == {"on_session_end", "post_llm_call"}
    assert "jev-router" in ctx.cli_commands
    assert "jev-router" in ctx.slash_commands


def test_manifest_declares_exactly_what_is_registered(plugin, tmp_path, monkeypatch):
    yaml = pytest.importorskip("yaml")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    ctx = StubContext(config={}, state=StubState(tmp_path))

    plugin.register(ctx)

    assert list(manifest["provides_middleware"]) == sorted(ctx.middleware)
    assert sorted(manifest["provides_tools"]) == sorted(ctx.tools)
    assert sorted(manifest["provides_hooks"]) == sorted(ctx.hooks)


def test_register_does_no_network_io(plugin, tmp_path, monkeypatch):
    """`hermes plugins doctor` blocks sockets during register(ctx); registration must be lazy."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    import httpx

    def forbidden(*_args, **_kwargs):
        raise AssertionError("registration must not open a socket")

    monkeypatch.setattr(httpx, "Client", forbidden)
    ctx = StubContext(config={}, state=StubState(tmp_path))

    plugin.register(ctx)  # must not raise


def test_registration_without_an_api_key_still_succeeds(plugin, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ctx = StubContext(config={}, state=StubState(tmp_path))

    plugin.register(ctx)

    assert "llm_request" in ctx.middleware


def test_config_schema_matches_the_settings_the_plugin_reads(tmp_path):
    yaml = pytest.importorskip("yaml")
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    declared = set(manifest["config_schema"])

    from config import Settings

    # `endpoint` is read by load_settings but intentionally not exposed as a setting.
    assert declared == set(Settings.__dataclass_fields__) - {"endpoint"}


def test_status_tool_reports_the_grid_and_the_audit(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ctx = StubContext(config={}, state=StubState(tmp_path))
    plugin.register(ctx)

    payload = json.loads(ctx.tools["jev_router_status"]["handler"](recent=3))

    assert payload["enabled"] is True
    assert payload["api_key_present"] is True
    assert len(payload["grid"]) == 6
    assert payload["grid"][0]["model"] == "deepseek-v4.1-flash"
    assert payload["audit"]["records"] == 0


def test_slash_command_status_grid_and_usage(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ctx = StubContext(config={}, state=StubState(tmp_path))
    plugin.register(ctx)

    handler = ctx.slash_commands["jev-router"]["handler"]

    assert "jev-router" in handler("")
    assert "Grid" in handler("grid")
    assert "Usage" in handler("nonsense")


def test_cli_command_status_and_missing_task(plugin, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ctx = StubContext(config={}, state=StubState(tmp_path))
    plugin.register(ctx)

    handler = ctx.cli_commands["jev-router"]["handler_fn"]

    assert handler(Args(jev_router_command="status")) == 0
    assert "jev-router" in capsys.readouterr().out

    assert handler(Args(jev_router_command="route", task=[])) == 2


def test_cli_reset_drops_the_memo(plugin, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ctx = StubContext(config={}, state=StubState(tmp_path))
    plugin.register(ctx)

    assert ctx.cli_commands["jev-router"]["handler_fn"](Args(jev_router_command="reset")) == 0
    assert "dropped" in capsys.readouterr().out


def test_cli_tail_prints_json_records(plugin, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ctx = StubContext(config={}, state=StubState(tmp_path))
    plugin.register(ctx)

    handler = ctx.cli_commands["jev-router"]["handler_fn"]
    assert handler(Args(jev_router_command="tail", count=5)) == 0
    assert capsys.readouterr().out.strip() == ""


# -- ctx/state stubs ---------------------------------------------------------------


class Registration:
    def __init__(self, kind: str, name: str) -> None:
        self.kind = kind
        self.name = name


class StubState:
    def __init__(self, data_dir) -> None:
        self.data_dir = data_dir
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class StubContext:
    """The subset of ``PluginContext`` the plugin uses, with a recording surface."""

    def __init__(self, config=None, state=None) -> None:
        self._config = dict(config or {})
        self.state = state
        self.middleware = {}
        self.hooks = {}
        self.tools = {}
        self.cli_commands = {}
        self.slash_commands = {}

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def set_config(self, key, value):
        self._config[key] = value

    def register_middleware(self, kind, callback):
        self.middleware[kind] = callback
        return Registration("middleware", kind)

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)
        return Registration("hook", name)

    def register_tool(self, *, name, toolset, schema, handler):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}
        return Registration("tool", name)

    def register_cli_command(self, *, name, help, setup_fn, handler_fn):  # noqa: A002
        self.cli_commands[name] = {"help": help, "setup_fn": setup_fn, "handler_fn": handler_fn}
        return Registration("cli", name)

    def register_command(self, name, *, handler, description="", args_hint=""):
        self.slash_commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}
        return Registration("command", name)
