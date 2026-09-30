"""Guards for ``jsonschema`` being an ``mcp``-extra-only dependency (issue #5804).

``jsonschema`` is declared under the ``mcp`` / ``all`` / ``dev`` extras, *not*
in ``[project] dependencies``, so a plain ``pip install kicad-tools`` does not
pull it (nor ``attrs`` / ``jsonschema-specifications`` / ``referencing`` /
``rpds-py``) in.  That is only safe because the import lives inside
``MCPServer.call_tool``'s ``dispatch()`` rather than at ``mcp/server.py``
module scope.

The distinction is load-bearing and invisible to CI: ``kicad_tools/mcp/__init__.py``
re-exports from ``kicad_tools.mcp.server``, so importing *any*
``kicad_tools.mcp`` submodule executes that module -- and three **core** CLI
paths do exactly that, lazily, at run time:

* ``kct route-auto``  -> ``kicad_tools.mcp.tools.routing``
* ``kct screenshot``  -> ``kicad_tools.mcp.tools.screenshot``
* ``kct report`` / any ``import kicad_tools.report`` -> ``report/figures.py``

A module-scope ``from jsonschema import validate`` therefore breaks all three on
a core-only install.  Every CI job installs ``uv sync --frozen --extra dev``,
which *has* ``jsonschema``, so no other test in the suite exercises a core-only
import shape.  These tests do, by hiding the module with a ``sys.meta_path``
blocker (same idea as ``tests/test_shapely_core_dependency.py``).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[import-not-found]


# Modules whose cached copies must be dropped so the import is really re-run
# under the blocker (a cached ``kicad_tools.mcp`` would hide the regression).
_RELOAD_PREFIXES = ("kicad_tools.mcp", "kicad_tools.report", "jsonschema")


class _JsonschemaBlocker:
    """A ``sys.meta_path`` finder that makes ``import jsonschema`` fail."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if fullname == "jsonschema" or fullname.startswith("jsonschema."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


@pytest.fixture
def jsonschema_absent(monkeypatch: pytest.MonkeyPatch):
    """Simulate a core-only install where ``jsonschema`` is not installed."""
    for name in list(sys.modules):
        if name in _RELOAD_PREFIXES or name.startswith(tuple(p + "." for p in _RELOAD_PREFIXES)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    # Replace (not mutate) the list so monkeypatch restores it verbatim.
    monkeypatch.setattr(sys, "meta_path", [_JsonschemaBlocker(), *sys.meta_path])

    # Sanity: the blocker is actually in force.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("jsonschema")
    yield


# ---------------------------------------------------------------------------
# Core CLI paths that lazily import kicad_tools.mcp must still work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        # `kct route-auto` (cli/commands/routing.py)
        "kicad_tools.mcp.tools.routing",
        # `kct screenshot` (cli/screenshot_cmd.py)
        "kicad_tools.mcp.tools.screenshot",
        # `kct report` / report figures (report/figures.py)
        "kicad_tools.report",
        # The re-exporting package and the module itself, for completeness.
        "kicad_tools.mcp",
        "kicad_tools.mcp.server",
    ],
)
def test_core_paths_import_without_jsonschema(jsonschema_absent, module: str) -> None:
    """Importing these must not require the ``mcp`` extra (issue #5804)."""
    assert importlib.import_module(module) is not None


def test_route_auto_entrypoint_resolves_without_jsonschema(jsonschema_absent) -> None:
    """The exact lazy import `kct route-auto` performs at run time."""
    from kicad_tools.mcp.tools.routing import route_net_auto

    assert callable(route_net_auto)


# ---------------------------------------------------------------------------
# ...while MCP tool dispatch still validates (and so still needs the extra)
# ---------------------------------------------------------------------------


def test_tool_dispatch_still_validates_arguments() -> None:
    """The import moved, it did not disappear: dispatch still schema-validates."""
    pytest.importorskip("jsonschema")
    from jsonschema import ValidationError  # type: ignore[import-untyped]

    from kicad_tools.mcp.server import MCPServer, ToolDefinition

    server = MCPServer()
    server.tools["_probe"] = ToolDefinition(
        name="_probe",
        description="test-only tool",
        parameters={
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
        handler=lambda params: {"success": True, "n": params["n"]},
    )

    assert server.call_tool("_probe", {"n": 1}) == {"success": True, "n": 1}
    with pytest.raises(ValidationError):
        server.call_tool("_probe", {"n": "not-an-integer"})


def test_tool_dispatch_requires_jsonschema(jsonschema_absent) -> None:
    """On a core-only install the failure is confined to tool dispatch."""
    from kicad_tools.mcp.server import MCPServer, ToolDefinition

    server = MCPServer()
    server.tools["_probe"] = ToolDefinition(
        name="_probe",
        description="test-only tool",
        parameters={"type": "object", "properties": {}},
        handler=lambda params: {"success": True},
    )

    # tools/list is pure metadata -- unaffected.
    assert any(t["name"] == "_probe" for t in server.get_tools_list())

    with pytest.raises(ModuleNotFoundError) as excinfo:
        server.call_tool("_probe", {})
    assert "jsonschema" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Packaging contract: mcp/all/dev extras only, never core
# ---------------------------------------------------------------------------


def _load_pyproject() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    with open(root / "pyproject.toml", "rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)
    return data


def _pins(requirements: list[str]) -> list[str]:
    return [r for r in requirements if r.replace(" ", "").lower().startswith("jsonschema")]


def test_jsonschema_is_not_a_core_dependency() -> None:
    """``jsonschema`` must stay out of ``[project] dependencies`` (issue #5804)."""
    core = _load_pyproject()["project"]["dependencies"]
    assert _pins(core) == [], f"jsonschema is back in core dependencies: {core}"


@pytest.mark.parametrize("extra", ["mcp", "all", "dev"])
def test_jsonschema_present_in_extra(extra: str) -> None:
    """The MCP server, the kitchen-sink extra and CI's env all need it."""
    requirements = _load_pyproject()["project"]["optional-dependencies"][extra]
    assert _pins(requirements), f"jsonschema missing from [{extra}]: {requirements}"
