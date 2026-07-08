"""Lint-style regression test for Issue #3907.

Background
----------
Issue #3532 established the fleet's 45-degree copper policy with an
*emit-then-repair* architecture: any pass could emit arbitrary-angle
copper, and a post-hoc ``quantize_pcb_file`` sweep (plus the
``tests/test_fleet_45_census.py`` ratchet) was expected to catch the
leaks.  That architecture kept leaking -- every new emitter had to
independently remember to dogleg, and the post-hoc repair had no
obstacle model (PR #3906's first quantize pass doglegged board-05 copper
straight into a 3-way short).

Issue #3907 moves legality to a single *by-construction* choke point:
:meth:`kicad_tools.router.primitives.Segment.to_sexp` verifies the
serialized displacement is on the {0, 45, 90, 135} angle set and raises
:class:`kicad_tools.router.quantize.OffAngleSegmentError` otherwise.
Every router-emitted segment flows through that method
(``Route.to_sexp`` / ``Autorouter.to_sexp`` fan into it), so the guard
cannot be bypassed by an emitter that forgets to quantize -- unless a
new emitter hand-writes ``(segment ...)`` s-expression TEXT directly,
bypassing ``Segment.to_sexp`` entirely.

This lint forbids exactly that.  It statically scans every string
literal under ``src/kicad_tools/router/`` for the ``(segment``
s-expression opener and asserts each occurrence sits in an allowlisted
file-level emitter (the choke point itself, or the exact-decimal dogleg
writer in ``quantize.py``).  A new router pass that hand-builds
``(segment ...)`` text -- and therefore escapes the by-construction
45-degree guard -- fails here, pointing the author at
``Segment.to_sexp`` (or ``dogleg_points`` for the geometry).

Why a *lint* and not just the census: the census
(``test_fleet_45_census.py``) only inspects committed artifacts, so it
catches a leak AFTER an off-angle board is committed (in CI, after every
local gate passed).  This lint catches the architectural bypass at the
source, before any artifact exists.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROUTER_ROOT = _REPO_ROOT / "src" / "kicad_tools" / "router"

# An EMISSION template: ``(segment`` at the start of a string literal or
# immediately after a newline (only whitespace before it).  This is the
# shape of a KiCad copper-segment s-expression being WRITTEN --
# ``f"(segment\n\t\t(start ..."`` or a ``"(segment"`` line fragment.
#
# It deliberately does NOT match:
#   * the escaped regex form ``\(segment`` (a PARSE pattern, e.g.
#     ``quantize._SEGMENT_BLOCK_RE`` / ``optimizer/pcb.py``'s walker),
#     because the backslash breaks the ``\s*\(`` alternation here;
#   * prose like ``"remove ``(segment ...)`` copper"`` in a docstring or
#     mid-sentence string, because ``(segment`` there is not at a line
#     start.
# Docstrings are excluded structurally as well (see ``_docstring_ids``).
_EMISSION_OPENER = re.compile(r"(?:^|\n)[ \t]*\(segment")

# ----------------------------------------------------------------------------
# Allowlist: files permitted to hand-build ``(segment ...)`` s-expression
# text.  Each is a deliberate, 45-legal-by-construction emitter that is
# itself the choke point (or part of it).  Every OTHER router file must
# route through ``Segment.to_sexp`` so the #3907 by-construction guard
# runs.
#
# Format: {relative_path_within_router: reason}
# ----------------------------------------------------------------------------
_ALLOWLIST: dict[str, str] = {
    # The by-construction choke point itself (issue #3907): the one place
    # a router ``Segment`` becomes s-expression text, guarded by
    # ``verify_segment_45``.
    "primitives.py": "Segment.to_sexp -- the #3907 serialization choke point",
    # File-level dogleg writer used by ``quantize_pcb_file``: emits
    # EXACT-decimal 45-legal two-leg doglegs into committed artifacts.
    # This is the demoted one-time artifact-repair tool (#3907 end
    # state), not a recurring per-emitter escape hatch -- its legs are
    # 45-aligned by construction in ``_decimal_dogleg_mid``.
    "quantize.py": "quantize_pcb_file dogleg writer -- exact-decimal 45-legal legs",
}


def _router_python_files() -> list[Path]:
    return sorted(p for p in _ROUTER_ROOT.rglob("*.py") if p.is_file())


def _docstring_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every module/class/function docstring Constant node.

    Docstrings routinely REFERENCE ``(segment ...)`` in prose (e.g.
    ``partial_rescue.strip_net_copper``'s "remove ``(segment ...)``
    copper"); they are not emitters, so they are excluded structurally.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _emission_template_lines(tree: ast.AST) -> list[int]:
    """Line numbers of non-docstring string literals that EMIT ``(segment``.

    Only ``ast.Constant`` string nodes matching :data:`_EMISSION_OPENER`
    (``(segment`` at a line start within the literal) are reported, and
    docstrings are excluded.  This isolates s-expression WRITER templates
    from PARSE patterns (escaped ``\\(segment``) and prose.
    """
    docstrings = _docstring_ids(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _EMISSION_OPENER.search(node.value)
        ):
            lines.append(node.lineno)
    return lines


def test_only_choke_point_emits_segment_sexp_text() -> None:
    """No router file outside the allowlist may hand-write ``(segment ...)``.

    Any router pass that builds segment s-expression text directly
    bypasses ``Segment.to_sexp`` and therefore the issue-#3907
    by-construction 45-degree guard.  Such a pass must instead construct
    a :class:`~kicad_tools.router.primitives.Segment` (doglegging any
    off-axis geometry with
    :func:`~kicad_tools.router.quantize.dogleg_points` first) and call
    ``.to_sexp()``.
    """
    offenders: list[str] = []
    for path in _router_python_files():
        rel = path.relative_to(_ROUTER_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - defensive
            offenders.append(f"{rel}: unparseable ({exc})")
            continue
        hit_lines = _emission_template_lines(tree)
        if not hit_lines:
            continue
        if rel in _ALLOWLIST:
            continue
        offenders.append(f"{rel}: lines {hit_lines} hand-build '(segment' s-expression text")

    assert not offenders, (
        "Router files emit segment s-expression text OUTSIDE the #3907 "
        "choke point -- they bypass the by-construction 45-degree guard "
        "in Segment.to_sexp.  Construct a router.primitives.Segment "
        "(dogleg off-axis geometry with quantize.dogleg_points first) and "
        "call .to_sexp() instead, or add the file to _ALLOWLIST with a "
        "documented reason if it is itself a 45-legal-by-construction "
        f"emitter.  Offenders: {offenders}"
    )


def test_allowlist_entries_are_live() -> None:
    """Ratchet: every allowlisted file must still emit ``(segment`` text.

    If a refactor removes the direct emission from an allowlisted file,
    the entry is stale and must be deleted so it cannot silently
    re-authorize a future hand-built emitter in that file.
    """
    stale: list[str] = []
    for rel in _ALLOWLIST:
        path = _ROUTER_ROOT / rel
        if not path.exists():
            stale.append(f"{rel} (file missing)")
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        if not _emission_template_lines(tree):
            stale.append(f"{rel} (no longer emits '(segment' text)")
    assert not stale, (
        "Stale _ALLOWLIST entries -- these files no longer hand-build "
        f"'(segment' s-expression text, so remove them: {stale}"
    )
