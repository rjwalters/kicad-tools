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
blocker inside a fresh child interpreter (see ``_run_without_jsonschema`` for
why it must not be done in the pytest process).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[import-not-found]


# The core-only checks run in a *child* interpreter, never in the pytest
# process.  Simulating a missing ``jsonschema`` in-process means purging the
# cached ``kicad_tools.mcp`` modules so the import really re-runs under the
# blocker -- and re-importing them rebinds ``kicad_tools.mcp`` on the parent
# package to a fresh module object.  ``monkeypatch`` restores ``sys.modules``
# afterwards but not that attribute, leaving two ``kicad_tools.mcp.server``
# copies alive in the xdist worker: a later ``monkeypatch.setattr(
# "kicad_tools.mcp.server.run_server", ...)`` (attribute walk) then patches the
# orphan while ``from kicad_tools.mcp.server import run_server`` (sys.modules)
# gets the real one, so ``test_mcp_http.py::...test_run_serve_stdio_import_error``
# started a real uvicorn server and hit the 60 s timeout.  A subprocess gets a
# pristine module graph and leaves ours alone.
_BLOCKER_PRELUDE = textwrap.dedent(
    """
    import sys

    class _JsonschemaBlocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "jsonschema" or fullname.startswith("jsonschema."):
                raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
            return None

    sys.meta_path.insert(0, _JsonschemaBlocker())

    # Sanity: the blocker is actually in force.
    try:
        import jsonschema  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        raise SystemExit("jsonschema blocker is not in force")
    """
)


def _run_without_jsonschema(body: str) -> None:
    """Run ``body`` in a fresh interpreter where ``import jsonschema`` fails."""
    code = _BLOCKER_PRELUDE + textwrap.dedent(body)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"core-only check failed (exit {proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )


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
def test_core_paths_import_without_jsonschema(module: str) -> None:
    """Importing these must not require the ``mcp`` extra (issue #5804)."""
    _run_without_jsonschema(
        f"""
        import importlib
        assert importlib.import_module({module!r}) is not None
        """
    )


def test_route_auto_entrypoint_resolves_without_jsonschema() -> None:
    """The exact lazy import `kct route-auto` performs at run time."""
    _run_without_jsonschema(
        """
        from kicad_tools.mcp.tools.routing import route_net_auto
        assert callable(route_net_auto)
        """
    )


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


def test_tool_dispatch_requires_jsonschema() -> None:
    """On a core-only install the failure is confined to tool dispatch."""
    _run_without_jsonschema(
        """
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

        try:
            server.call_tool("_probe", {})
        except ModuleNotFoundError as exc:
            assert "jsonschema" in str(exc), exc
        else:
            raise AssertionError("call_tool succeeded without jsonschema")
        """
    )


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
