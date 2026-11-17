"""License scan: no GPL code is imported or vendored into ``src/`` (issue #7687).

kicad-tools is MIT.  KiCad's 3D generators (``kicad-library-tools``, formerly
``kicad-packages3D-generator``) and their helpers are GPL-3.0-or-later, and
kicadStepUpMod / easyeda2kicad are AGPL: they may only be *run* as external
tools (``kicad_tools.modelgen.bridge``), never imported or copied.  CadQuery
and OCP (Apache-2.0) are the only CAD dependencies, and only in the optional
``[models]`` extra.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kicad_tools"

#: Top-level module names of GPL/AGPL projects in the 3D-model space.
FORBIDDEN_MODULES = frozenset(
    {
        "KicadModTree",  # kicad-footprint-generator (GPL-3.0)
        "kicad_library_tools",
        "kicad_packages3d_generator",
        "cq_parameters",  # packages3D-generator family parameter modules
        "cq_base_model",
        "cq_base_parameters",
        "cqToolsExporters",
        "cq_cad_tools",
        "kicadStepUptools",  # kicadStepUpMod (AGPL)
        "easyeda2kicad",  # AGPL
    }
)

#: License-notice phrases that mark a copied GPL/AGPL source file.
GPL_NOTICES = (
    # a file's own SPDX tag (comment-prefixed at line start), not prose quoting one
    re.compile(
        r"^\s*(#|//|/\*|\*|;|--)?\s*SPDX-License-Identifier:\s*A?GPL",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(r"under the terms of the GNU (Affero )?General Public License", re.IGNORECASE),
    re.compile(r"GNU (Affero )?General Public License as published by", re.IGNORECASE),
    re.compile(r"This program is free software; you can redistribute it", re.IGNORECASE),
)

#: Third-party imports the model generator may use (all Apache-2.0).
MODELGEN_ALLOWED_THIRD_PARTY = frozenset({"cadquery", "OCP"})


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            # importlib.import_module("x") / __import__("x")
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    roots.add(arg.value.split(".")[0])
    return roots


def test_scan_covers_the_tree():
    files = _py_files()
    assert len(files) > 100
    assert SRC / "modelgen" / "bridge.py" in files


def test_no_gpl_module_is_imported_anywhere_in_src():
    offenders = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _imported_roots(tree) & FORBIDDEN_MODULES
        if hits:
            offenders.append(f"{path.relative_to(SRC)}: {sorted(hits)}")
    assert not offenders, "GPL/AGPL modules imported:\n" + "\n".join(offenders)


def test_no_gpl_license_notice_in_src():
    offenders = []
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or path.suffix in (".so", ".pyd", ".dylib", ".png", ".step"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in GPL_NOTICES:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(SRC)}: {pattern.pattern}")
    assert not offenders, "GPL/AGPL license notices (vendored code?):\n" + "\n".join(offenders)


def test_modelgen_third_party_imports_are_apache_cad_only():
    import sys

    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for path in sorted((SRC / "modelgen").glob("*.py")):
        roots = _imported_roots(ast.parse(path.read_text(encoding="utf-8")))
        third_party = roots - stdlib - {"kicad_tools"}
        assert third_party <= MODELGEN_ALLOWED_THIRD_PARTY, (path.name, third_party)


def test_cadquery_is_imported_lazily():
    """Importing kicad_tools.modelgen must not need (or import) the CAD kernel."""
    for path in sorted((SRC / "modelgen").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module level only
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                assert not any(n.split(".")[0] in MODELGEN_ALLOWED_THIRD_PARTY for n in names), (
                    path.name
                )


@pytest.mark.parametrize("snippet", ["import KicadModTree", "from cq_parameters import X"])
def test_scanner_catches_forbidden_imports(snippet):
    assert _imported_roots(ast.parse(snippet)) & FORBIDDEN_MODULES


def test_notice_patterns_catch_a_copied_header():
    header = "# SPDX-License-Identifier: GPL-3.0-or-later\n"
    assert any(p.search(header) for p in GPL_NOTICES)
    prose = "its NOTICE says `SPDX-License-Identifier: GPL-3.0-or-later`"
    assert not any(p.search(prose) for p in GPL_NOTICES)
