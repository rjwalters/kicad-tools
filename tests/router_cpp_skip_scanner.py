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

Since Issue #9821 the tracked names also include:

* the router backend's module-level ``_CPP_AVAILABLE`` flag, imported from
  ``kicad_tools.router.cpp_backend`` or ``kicad_tools.router`` (aliases
  included), or read as ``<module alias>._CPP_AVAILABLE``. The DRC and
  placement backends' flags of the same name stay out of scope;
* module-level helper functions whose body imports the router extension
  (``import kicad_tools.router.router_cpp``, ``from kicad_tools.router import
  router_cpp``, ``from kicad_tools.router.router_cpp import ...``) or reads a
  router probe already tracked -- e.g. a local ``def _cpp_available():`` that
  tries the import -- plus variables assigned from a call to such a helper.

Anything subtler (a helper defined in another module, a probe hidden behind a
dynamic import) is left to the runtime backstop in ``tests/conftest.py``
(Issue #9804), which fails any skip whose reason mentions the C++ router.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ROUTER_CPP_MODULE = "kicad_tools.router.router_cpp"

_FROM_ROUTER_CPP_BACKEND = re.compile(
    r"^[ \t]*from\s+kicad_tools\.router\.cpp_backend\s+import\s+"
    r"(?P<names>\([^)]*\)|[^\n#]+)",
    re.MULTILINE,
)
_FROM_ROUTER_PKG = re.compile(
    r"^[ \t]*from\s+kicad_tools\.router\s+import\s+"
    r"(?P<names>\([^)]*\)|[^\n#]+)",
    re.MULTILINE,
)
_IMPORT_AS = re.compile(
    r"^\s*import\s+kicad_tools\.router\.cpp_backend\s+as\s+(?P<alias>\w+)",
    re.MULTILINE,
)
_IMPORTORSKIP_CPP_BACKEND = re.compile(
    r"(?P<var>\w+)\s*=\s*pytest\.importorskip\(\s*[\"']kicad_tools\.router\.cpp_backend[\"']"
)
# Attributes of the router ``cpp_backend`` module that report availability.
_MODULE_PROBE_ATTRS = ("is_cpp_available", "_CPP_AVAILABLE")
_MODULE_LEVEL_DEF = re.compile(r"^(?:async\s+)?def\s+(?P<name>\w+)\s*\(", re.MULTILINE)
# The router extension imported inside a helper body (Issue #9821).
_IMPORTS_ROUTER_EXTENSION = re.compile(
    r"^[ \t]+(?:import\s+kicad_tools\.router\.router_cpp\b"
    r"|from\s+kicad_tools\.router\.router_cpp\s+import\b"
    r"|from\s+kicad_tools\.router\s+import\s+(?:\([^)]*\brouter_cpp\b|[^\n#]*\brouter_cpp\b))",
    re.MULTILINE,
)
# Module-level only: a local variable inside some test that happens to call
# a helper must not turn every later use of that name into a "probe".
_ASSIGNED_FROM_CALL = re.compile(
    r"^(?P<var>\w+)\s*=\s*(?:bool\(\s*)?(?P<expr>[\w.]+)\s*\(\s*\)", re.MULTILINE
)
_ALIAS_FROM_PROBE = re.compile(
    r"^\s*(?P<var>\w+)\s*=\s*(?:bool\(\s*)?(?P<expr>[\w.]*is_cpp_available)\(\s*\)?", re.MULTILINE
)


def _names_from_import_list(names: str) -> list[tuple[str, str]]:
    """``[(imported_name, local_name), ...]`` from an import list body."""
    names = re.sub(r"#[^\n]*", "", names)
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
    assigned from a probe call (``available = is_cpp_available()``). The
    router backend's ``_CPP_AVAILABLE`` flag is tracked through the same
    bindings (Issue #9821). A file that binds only the DRC or placement probe
    of the same name -- or does not reference the router extension at all --
    yields an empty set.
    """
    refs: set[str] = set()

    def add_probe_binding(names: str) -> bool:
        bound = False
        for imported, local in _names_from_import_list(names):
            if imported in ("is_cpp_available", "_CPP_AVAILABLE"):
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
                refs.update(f"{local}.{attr}" for attr in _MODULE_PROBE_ATTRS)
                probe_bound = True

    for match in _IMPORT_AS.finditer(source):
        refs.update(f"{match.group('alias')}.{attr}" for attr in _MODULE_PROBE_ATTRS)
        probe_bound = True
    if re.search(r"^\s*import\s+kicad_tools\.router\.cpp_backend\s*$", source, re.MULTILINE):
        refs.update(f"kicad_tools.router.cpp_backend.{attr}" for attr in _MODULE_PROBE_ATTRS)
        probe_bound = True
    for match in _IMPORTORSKIP_CPP_BACKEND.finditer(source):
        refs.update(f"{match.group('var')}.{attr}" for attr in _MODULE_PROBE_ATTRS)
        probe_bound = True

    if probe_bound:
        # Variables assigned from any probe expression already known.
        for match in _ALIAS_FROM_PROBE.finditer(source):
            expr = match.group("expr")
            if expr in refs or expr.endswith("is_cpp_available") and "is_cpp_available" in refs:
                refs.add(match.group("var"))
    return refs


def _module_level_function_bodies(source: str) -> list[tuple[str, str]]:
    """``[(name, body), ...]`` for every column-0 ``def`` in *source*."""
    matches = list(_MODULE_LEVEL_DEF.finditer(source))
    bodies: list[tuple[str, str]] = []
    for match in matches:
        start = match.end()
        end = len(source)
        for line in re.finditer(r"^(?=\S)(?!#)[^\n]*", source[start:], re.MULTILINE):
            if line.start() == 0:
                continue  # rest of the ``def`` line itself
            end = start + line.start()
            break
        bodies.append((match.group("name"), source[start:end]))
    return bodies


def router_probe_helpers(source: str, refs: set[str] | None = None) -> set[str]:
    """Module-level helpers (and variables bound from them) probing the router.

    A helper counts when its body imports the router extension -- the
    ``try: import kicad_tools.router.router_cpp`` / ``except ImportError``
    shape of ``def _cpp_available()`` -- or reads a router probe in *refs*
    (defaults to :func:`router_probe_refs`). Helpers calling such helpers are
    resolved transitively. Issue #9821.
    """
    if refs is None:
        refs = router_probe_refs(source)
    bodies = _module_level_function_bodies(source)
    helpers: set[str] = set()
    changed = True
    while changed:
        changed = False
        known = refs | helpers
        for name, body in bodies:
            if name in helpers:
                continue
            if _IMPORTS_ROUTER_EXTENSION.search(body) or any(
                re.search(rf"(?<![\w.]){re.escape(ref)}\b", body) for ref in known
            ):
                helpers.add(name)
                changed = True
    if helpers:
        for match in _ASSIGNED_FROM_CALL.finditer(source):
            if match.group("expr") in helpers:
                helpers.add(match.group("var"))
    return helpers


def router_info_refs(source: str) -> set[str]:
    """Expressions bound to the router ``get_backend_info`` (``["available"]`` probe)."""
    refs: set[str] = set()
    for pattern in (_FROM_ROUTER_CPP_BACKEND, _FROM_ROUTER_PKG):
        for match in pattern.finditer(source):
            for imported, local in _names_from_import_list(match.group("names")):
                if imported == "get_backend_info":
                    refs.add(local)
                elif imported == "cpp_backend":
                    refs.add(f"{local}.get_backend_info")
    for match in _IMPORT_AS.finditer(source):
        refs.add(f"{match.group('alias')}.get_backend_info")
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
      intermediate alias like ``requires_cpp = pytest.mark.skipif(...)`` --
      and, since Issue #9804, any other ``skipif`` whose condition reads the
      router backend's availability through ``get_backend_info()``, and,
      since Issue #9821, any ``skipif`` reading the router ``_CPP_AVAILABLE``
      flag or a module-level helper that probes the router extension;
    * ``pytest.importorskip("kicad_tools.router.router_cpp")``;
    * ``pytest.skip(...)`` inside a block guarded by ``not is_cpp_available()``
      (including ``if backend == "cpp" and not is_cpp_available():`` and
      variables assigned from the probe);
    * an unconditional ``pytest.mark.skip`` (decorator, ``pytestmark``,
      ``marks=`` in a parametrization, or an alias binding) in any module
      that references the router backend at all (Issue #9804).
    """
    refs = router_probe_refs(source)
    refs |= router_probe_helpers(source, refs)
    info_refs = router_info_refs(source)
    module_ref = references_router_module(source)
    if not refs and not info_refs and not module_ref and "router_cpp" not in source:
        return []

    violations: list[Violation] = []

    def line_of(offset: int) -> int:
        return source.count("\n", 0, offset) + 1

    def mentions_probe(text: str) -> bool:
        for ref in refs:
            if re.search(rf"(?<![\w.]){re.escape(ref)}\b", text):
                return True
        return bool(module_ref and re.search(r"(?<![\w.])router_cpp\b", text))

    def mentions_info_probe(text: str) -> bool:
        # ``get_backend_info()["available"]`` / ``.get("available")`` reads
        # the same availability through a different name (Issue #9804).
        return any(re.search(rf"(?<![\w.]){re.escape(ref)}\s*\(\s*\)", text) for ref in info_refs)

    # 1. skipif(...) whose arguments reference the router probe.
    for match in re.finditer(r"skipif\s*\(", source):
        call = _balanced_call(source, match.end() - 1)
        if mentions_probe(call) or mentions_info_probe(call):
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

    # 3. pytest.skip guarded by a negated probe reference
    #    (``not is_cpp_available()``, ``not get_backend_info()["available"]``).
    if refs or info_refs:
        alternatives = []
        if refs:
            joined = "|".join(re.escape(ref) for ref in refs)
            alternatives.append(rf"not\s+(?:{joined})\s*\(")
            alternatives.append(rf"not\s+(?:{joined})\b(?!\s*\()")
        if info_refs:
            joined = "|".join(re.escape(ref) for ref in info_refs)
            alternatives.append(
                rf"not\s+(?:{joined})\s*\(\s*\)\s*"
                r"(?:\[\s*[\"']available[\"']\s*\]|\.get\(\s*[\"']available[\"']\s*\))"
            )
        negated = re.compile("|".join(alternatives))
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
    # 4. Unconditional ``pytest.mark.skip`` in a module that references the
    #    router backend at all (the early return above already exempted
    #    modules that do not): with no condition text to read, the skip
    #    hides native cases as effectively as a probe-guarded ``skipif``
    #    (Issue #9804).
    for match in re.finditer(r"(?<![\w.])pytest\.mark\.skip\b", source):
        violations.append(
            Violation(
                path,
                line_of(match.start()),
                "unconditional pytest.mark.skip in a module referencing the C++ "
                "router backend: use the `requires_router_cpp` marker (fails with "
                "the build hint), not a skip (Issue #9804)",
            )
        )
    # 5. ``try: from kicad_tools.router import router_cpp`` /
    #    ``except ImportError: pytest.skip(...)``.
    for match in re.finditer(
        r"except\s+(?:\(?\s*)?(?:ImportError|ModuleNotFoundError)[^\n]*:[ \t]*\n", source
    ):
        lines_before = source[: match.start()]
        try_at = lines_before.rfind("try:")
        if try_at == -1 or "router_cpp" not in source[try_at : match.start()]:
            continue
        indent = match.start() - (source.rfind("\n", 0, match.start()) + 1)
        rest = source[match.end() :].splitlines()
        block_skip = _first_skip_in_block(rest, 0, indent)
        if block_skip is not None:
            violations.append(
                Violation(
                    path,
                    line_of(match.end()) + block_skip,
                    "pytest.skip on ImportError of the router extension: call "
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
