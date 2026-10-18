"""``router/negotiated_budget`` must not depend on a routing engine (issue #5802).

Modelled on ``tests/router/pairplan/test_import_boundary.py``: a static AST scan
(relative and ``TYPE_CHECKING`` imports resolved) plus a fresh-interpreter
runtime probe that catches indirect ``negotiated_budget -> X -> core`` chains.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import kicad_tools.router.negotiated_budget as negotiated_budget
from tests.router.pairplan.test_import_boundary import forbidden_imports

MODULE = "kicad_tools.router.negotiated_budget"


def test_negotiated_budget_imports_no_engine_module_statically() -> None:
    source = Path(negotiated_budget.__file__).read_text(encoding="utf-8")
    assert forbidden_imports(source, MODULE) == []


def test_negotiated_budget_does_not_load_core_at_runtime() -> None:
    code = (
        "import sys; import kicad_tools.router.negotiated_budget; "
        "bad = [m for m in ('kicad_tools.router.core', 'kicad_tools.router.lattice', "
        "'kicad_tools.router.mesh', 'kicad_tools.router.diffpair_routing') if m in sys.modules]; "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"loaded engine modules: {proc.stdout}{proc.stderr}"
