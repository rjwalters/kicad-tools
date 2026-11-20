"""Static scan for C++-router-backend skip guards in test sources (Issue #9705).

A test case that needs the compiled router extension must *fail* with the
build hint (``tests/conftest.py`` :func:`require_router_cpp` / the
``requires_router_cpp`` marker) when the extension is missing -- never skip.
Silent skips are how a worktree or CI leg whose ``kct build-native`` step
broke once reported a green run with every native parity case missing.

This module holds the pure scanning logic so the pytest-visible assertions
(:mod:`tests.test_router_cpp_no_skip_guards`) can unit-test the detector on
small source samples and then run it over the whole ``tests/`` tree.

Distinguishing the router probe from identically named probes of *other*
extensions matters: ``kicad_tools.drc.cpp_backend.is_cpp_available`` and the
placement backend expose the same function name, and their skips are
deliberately out of scope (Issue #9705 "Out of Scope"). The scanner therefore
resolves, per file, which local names are bound to the *router* probe and
only flags guards expressed through those names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ROUTER_CPP_MODULE = "kicad_tools.router.router_cpp"

_FROM_ROUTER_CPP_BACKEND = re.compile(
    r"^\s*from\s+kicad_tools\.router\.cpp_backend\s+import\s+(?P<names>[^\n#]+)",
    re.MULTILINE,
)
_FROM_ROUTER_PKG = re.compile(
    r"^\s*from\s+kicad_tools\.router\s+import\s+(?P<names>[^\n#]+)",
    re.MULTILINE,
)
_IMPORT_AS = re.compile(
    r"^\s*import\s+kicad_tools\.router\.cpp_backend\s+as\s+(?P<alias>\w+)",
    re.MULTILINE,
)
_IMPORTORSKIP_CPP_BACKEND = re.compile(
    r"(?P<var>\w+)\s*=\s*pytest\.importorskip\(\s*[\"']kicad_tools\.router\.cpp_backend[\"']"
)
_ALIAS_FROM_PROBE = re.compile(
    r"^\s*(?P<var>\w+)\s*=\s*(?:bool\(\s*)?(?P<expr>[\w.]*is_cpp_available)\(\s*\)?", re.MULTILINE
)


def _names_from_import_list(names: str) -> list[tuple[str, str]]:
    """``[(imported_name, local_name), ...]`` from an import list body."""
    stripped = names.strip().strip("()").strip()
    result: list[tuple[str, str]] = []
    for chunk in stripped.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split()
        if len(parts) >= 3 and parts[-2] == "as":
            result.append((parts[0], parts[-1]))
        else:
            result.append((parts[0], parts[0]))
    return result


def router_probe_refs(source: str) -> set[str]:
    """Expressions through which *this* source refers to the router probe.

    Covers plain imports (``from kicad_tools.router.cpp_backend import
    is_cpp_available``, optionally aliased), package re-exports
    (``from kicad_tools.router import is_cpp_available``), module aliases
    (``import kicad_tools.router.cpp_backend as cb`` ->
    ``cb.is_cpp_available()``), ``importorskip``-bound modules and variables
    assigned from a probe call (``available = is_cpp_available()``). A file
    that binds only the DRC or placement probe of the same name -- or does
    not reference the router extension at all -- yields an empty set.
    """
    refs: set[str] = set()

    def add_probe_binding(names: str) -> bool:
        bound = False
        for imported, local in _names_from_import_list(names):
            if imported == "is_cpp_available":
                refs.add(local)
                bound = True
        return bound

    probe_bound = False
    for match in _FROM_ROUTER_CPP_BACKEND.finditer(source):
        probe_bound |= add_probe_binding(match.group("names"))
    for match in _FROM_ROUTER_PKG.finditer(source):
        probe_bound |= add_probe_binding(match.group("names"))
        for imported, local in _names_from_import_list(match.group("names")):
            if imported == "cpp_backend":
                refs.add(f"{local}.is_cpp_available")
                probe_bound = True

    for match in _IMPORT_AS.finditer(source):
        refs.add(f"{match.group('alias')}.is_cpp_available")
        probe_bound = True
    if re.search(r"^\s*import\s+kicad_tools\.router\.cpp_backend\s*$", source, re.MULTILINE):
        refs.add("kicad_tools.router.cpp_backend.is_cpp_available")
        probe_bound = True
    for match in _IMPORTORSKIP_CPP_BACKEND.finditer(source):
        refs.add(f"{match.group('var')}.is_cpp_available")
        probe_bound = True

    if probe_bound:
        # Variables assigned from any probe expression already known.
        for match in _ALIAS_FROM_PROBE.finditer(source):
            expr = match.group("expr")
            if expr in refs or expr.endswith("is_cpp_available") and "is_cpp_available" in refs:
                refs.add(match.group("var"))
    return refs


def references_router_module(source: str) -> bool:
    """Whether the source names the router extension module at all."""
    return ROUTER_CPP_MODULE in source


def _balanced_call(text: str, open_index: int) -> str:
    """Text from the ``(`` at *open_index* through its matching ``)``."""
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[open_index : index + 1]
    return text[open_index:]


@dataclass(frozen=True)
class Violation:
    """One skip guard found on the router C++ backend."""

    path: str
    line: int
    detail: str

    def __str__(self) -> str:  # pragma: no cover - presentation only
        return f"{self.path}:{self.line}: {self.detail}"


def scan_source(source: str, path: str = "<source>") -> list[Violation]:
    """Return every router-C++ skip guard in *source*.

    Flags, multiline-aware:

    * ``pytest.mark.skipif(not is_cpp_available(), ...)`` in any form --
      decorator, ``pytestmark``, ``marks=`` in a parametrization, or an
      intermediate alias like ``requires_cpp = pytest.mark.skipif(...)``;
    * ``pytest.importorskip("kicad_tools.router.router_cpp")``;
    * ``pytest.skip(...)`` inside a block guarded by ``not is_cpp_available()``
      (including ``if backend == "cpp" and not is_cpp_available():`` and
      variables assigned from the probe).
    """
    refs = router_probe_refs(source)
    module_ref = references_router_module(source)
    if not refs and not module_ref:
        return []

    violations: list[Violation] = []

    def line_of(offset: int) -> int:
        return source.count("\n", 0, offset) + 1

    def mentions_probe(text: str) -> bool:
        for ref in refs:
            if re.search(rf"(?<![\w.]){re.escape(ref)}\b", text):
                return True
        return bool(module_ref and re.search(r"(?<![\w.])router_cpp\b", text))

    # 1. skipif(...) whose arguments reference the router probe.
    for match in re.finditer(r"skipif\s*\(", source):
        call = _balanced_call(source, match.end() - 1)
        if mentions_probe(call):
            violations.append(
                Violation(
                    path,
                    line_of(match.start()),
                    "pytest.mark.skipif on the C++ router backend: use the "
                    "`requires_router_cpp` marker (fails with the build hint), "
                    "not a skip (Issue #9705)",
                )
            )

    # 2. importorskip on the router extension module.
    for match in re.finditer(r"importorskip\s*\(", source):
        call = _balanced_call(source, match.end() - 1)
        if ROUTER_CPP_MODULE in call:
            violations.append(
                Violation(
                    path,
                    line_of(match.start()),
                    'pytest.importorskip("kicad_tools.router.router_cpp"): import '
                    "conditionally and mark the tests `requires_router_cpp` "
                    "(Issue #9705)",
                )
            )

    # 3. pytest.skip guarded by a negated probe reference.
    if refs:
        negated = re.compile(
            r"not\s+(?:" + "|".join(re.escape(ref) for ref in refs) + r")\s*\(|"
            r"not\s+(?:" + "|".join(re.escape(ref) for ref in refs) + r")\b(?!\s*\()"
        )
        lines = source.splitlines()
        for index, line in enumerate(lines):
            if negated.search(line) is None:
                continue
            if "pytest.skip(" in line:
                violations.append(
                    Violation(
                        path,
                        index + 1,
                        "pytest.skip guarded by the router availability probe: call "
                        "require_router_cpp() instead (Issue #9705)",
                    )
                )
            elif line.rstrip().endswith(":"):
                indent = len(line) - len(line.lstrip())
                body_skip = _first_skip_in_block(lines, index + 1, indent)
                if body_skip is not None:
                    violations.append(
                        Violation(
                            path,
                            body_skip + 1,
                            "pytest.skip guarded by the router availability probe: call "
                            "require_router_cpp() instead (Issue #9705)",
                        )
                    )
    return violations


def _first_skip_in_block(lines: list[str], start: int, header_indent: int) -> int | None:
    """First ``pytest.skip(`` line inside the block opened at *header_indent*."""
    for index in range(start, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= header_indent:
            return None
        if "pytest.skip(" in line:
            return index
    return None


def scan_tree(root: str, *, skip: set[str] = frozenset()) -> list[Violation]:
    """Scan every Python file under *root* (including conftests)."""
    from pathlib import Path

    violations: list[Violation] = []
    for path in sorted(Path(root).rglob("*.py")):
        if path.name in skip:
            continue
        violations.extend(scan_source(path.read_text(encoding="utf-8"), str(path)))
    return violations
