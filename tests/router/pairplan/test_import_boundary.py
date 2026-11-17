"""``router/pairplan`` must not depend on any routing engine (issue #6837).

Two checks, because each misses something the other catches:

* **static** -- every import statement in every ``pairplan`` module, relative
  imports resolved, function-local and ``TYPE_CHECKING`` ones included;
* **runtime** -- a fresh interpreter imports ``pairplan`` and every submodule
  and must not have loaded an engine module (catches an indirect
  ``pairplan -> X -> lattice`` chain the AST scan cannot see).
"""

from __future__ import annotations

import ast
import pkgutil
import subprocess
import sys
from pathlib import Path

import kicad_tools.router.pairplan as pairplan

FORBIDDEN = (
    "kicad_tools.router.lattice",
    "kicad_tools.router.mesh",
    "kicad_tools.router.core",
    "kicad_tools.router.diffpair_routing",
)

PACKAGE_DIR = Path(pairplan.__file__).resolve().parent


def _is_forbidden(name: str) -> bool:
    return any(name == f or name.startswith(f + ".") for f in FORBIDDEN)


def forbidden_imports(source: str, module: str, *, is_package: bool = False) -> list[str]:
    """Every forbidden absolute module name ``source`` imports.

    ``module`` is the dotted name of the module ``source`` belongs to; it
    anchors relative imports (``from ..lattice import x`` in
    ``kicad_tools.router.pairplan.chain`` is ``kicad_tools.router.lattice``).
    ``from P import a`` also counts ``P.a``, so ``from .. import lattice`` is
    caught too.
    """
    parts = module.split(".")
    package = parts if is_package else parts[:-1]
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            names = [prefix] + [f"{prefix}.{alias.name}" for alias in node.names]
        found += [f"{module}:{node.lineno} imports {n}" for n in names if _is_forbidden(n)]
    return found


def _pairplan_modules() -> list[tuple[str, Path, bool]]:
    out = [("kicad_tools.router.pairplan", PACKAGE_DIR / "__init__.py", True)]
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        if path.name != "__init__.py":
            out.append((f"kicad_tools.router.pairplan.{path.stem}", path, False))
    return out


def test_pairplan_has_modules_to_scan() -> None:
    names = {m for m, _p, _pkg in _pairplan_modules()}
    assert {"chain", "tie", "gateway", "geometry"} <= {n.rsplit(".", 1)[1] for n in names}


def test_pairplan_imports_no_engine_module_statically() -> None:
    offenders: list[str] = []
    for module, path, is_package in _pairplan_modules():
        offenders += forbidden_imports(
            path.read_text(encoding="utf-8"), module, is_package=is_package
        )
    assert not offenders, f"router/pairplan must not import a routing engine: {offenders}"


def test_static_scan_resolves_relative_and_nested_imports() -> None:
    """Self-test: each spelling of a lattice import is caught."""
    mod = "kicad_tools.router.pairplan.chain"
    for source in (
        "from ..lattice import geometry\n",
        "from ..lattice.geometry import dist\n",
        "from .. import lattice\n",
        "import kicad_tools.router.lattice.coupled\n",
        "from kicad_tools.router.core import Autorouter\n",
        "def f():\n    from ..mesh import x\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..diffpair_routing import D\n",
    ):
        assert forbidden_imports(source, mod), source
    # Legitimate neighbours stay allowed.
    for source in (
        "from ..primitives import Pad\n",
        "from ..quantize import dogleg_points\n",
        "from .geometry import dist\n",
        "from kicad_tools.geometry.copper import pad_waiver_polygon\n",
    ):
        assert not forbidden_imports(source, mod), source
    # In the package ``__init__``, ``.`` is pairplan itself.
    assert forbidden_imports(
        "from ..lattice import x\n", "kicad_tools.router.pairplan", is_package=True
    )
    assert not forbidden_imports(
        "from .chain import x\n", "kicad_tools.router.pairplan", is_package=True
    )


_RUNTIME_PROBE = """
import importlib, pkgutil, sys
import kicad_tools.router.pairplan as pp
for info in pkgutil.iter_modules(pp.__path__):
    importlib.import_module(f"kicad_tools.router.pairplan.{info.name}")
__EXTRA__
forbidden = __FORBIDDEN__
bad = sorted(m for m in sys.modules if any(m == f or m.startswith(f + ".") for f in forbidden))
print("\\n".join(bad))
"""


def _loaded_engine_modules(extra: str = "") -> list[str]:
    code = _RUNTIME_PROBE.replace("__EXTRA__", extra).replace("__FORBIDDEN__", repr(FORBIDDEN))
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=300
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def test_importing_pairplan_loads_no_engine_module() -> None:
    assert [i.name for i in pkgutil.iter_modules(pairplan.__path__)]
    loaded = _loaded_engine_modules()
    assert not loaded, f"importing router.pairplan loaded engine modules: {loaded}"


def test_runtime_probe_detects_an_engine_import() -> None:
    """Self-test: the probe sees a lattice module once one is imported."""
    loaded = _loaded_engine_modules("import kicad_tools.router.lattice.geometry")
    assert "kicad_tools.router.lattice.geometry" in loaded
