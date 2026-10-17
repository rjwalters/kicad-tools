#!/usr/bin/env python3
"""Shared helpers for the wire-editing ``kct sch`` commands.

These used to be copy-pasted across a dozen ``sch_*.py`` modules.  Centralising
them keeps the commands in lock-step: if one copy drifts, the others no longer
silently drift with it.

Endpoint-coincidence semantics deliberately stay *per caller*:

* ``sch_remove_wire`` / ``sch_remove_component`` / ``sch_disconnect`` bucket
  at 0.1 mm by truncation (``int(x * 10)``, see :func:`truncating_bucket`).
* ``sch_cleanup_wires`` buckets at micron resolution by rounding.
* ``sch_move_component`` / ``operations.symbol_ops`` match with a float
  tolerance and report the endpoint index.

Do not unify these without a changelog fragment; they can disagree for
coordinates within float epsilon of a 0.1 mm boundary.
"""

from __future__ import annotations

import re
import uuid as uuid_mod
from collections.abc import Callable
from pathlib import Path

from kicad_tools.operations.wire_primitives import (
    find_wires_at_point_with_index,
    iter_wire_endpoint_matches,
)
from kicad_tools.schema import Schematic
from kicad_tools.sexp import SExp

# Default matching tolerance for the truncating-bucket ``_find_wires_at_point``.
POINT_TOLERANCE = 1.27  # mm - standard KiCad grid

# Default float tolerance for endpoint matching with an endpoint index.
INDEXED_POINT_TOLERANCE = 0.127  # mm - 1/10 grid


def truncating_bucket(x: float, y: float) -> tuple[int, int]:
    """Bucket a point at 0.1 mm by truncation (``remove-wire`` / ``remove-component``)."""
    return (int(x * 10), int(y * 10))


def _point_on_wire_midpoint(
    point: tuple[float, float],
    wire_start: tuple[float, float],
    wire_end: tuple[float, float],
    tolerance: float = 0.1,
) -> bool:
    """Check if a point lies on a wire segment but not at its endpoints."""
    x, y = point

    # Check if point is at either endpoint
    for ep in (wire_start, wire_end):
        if abs(x - ep[0]) < tolerance and abs(y - ep[1]) < tolerance:
            return False

    # Check if point is on the segment
    x1, y1 = wire_start
    x2, y2 = wire_end

    # Bounding box check
    if not (min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance):
        return False
    if not (min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance):
        return False

    # Perpendicular distance check
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if length < tolerance:
        return False

    dist = abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1) / length
    return bool(dist < tolerance)


def _wire_start_end(wire_sexp: SExp) -> tuple[tuple[float, float], tuple[float, float]]:
    """Extract start and end points from a wire S-expression node."""
    pts_node = wire_sexp.find("pts")
    if not pts_node:
        return (0.0, 0.0), (0.0, 0.0)

    xy_nodes = pts_node.find_all("xy")
    if len(xy_nodes) < 2:
        return (0.0, 0.0), (0.0, 0.0)

    x1 = xy_nodes[0].get_float(0) or 0.0
    y1 = xy_nodes[0].get_float(1) or 0.0
    x2 = xy_nodes[1].get_float(0) or 0.0
    y2 = xy_nodes[1].get_float(1) or 0.0

    return (x1, y1), (x2, y2)


def _wire_endpoint_counts(
    wire_sexps: list[SExp],
    *,
    key: Callable[[float, float], tuple[int, int]],
) -> dict[tuple[int, int], int]:
    """Count how many wires touch each endpoint bucket.

    *key* maps an endpoint ``(x, y)`` to its bucket.  It is required on
    purpose: callers disagree on bucketing (see module docstring) and a
    default would silently pick one.
    """
    counts: dict[tuple[int, int], int] = {}
    for ws in wire_sexps:
        start, end = _wire_start_end(ws)
        for pt in (start, end):
            bucket = key(pt[0], pt[1])
            counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _find_wires_at_point(
    schematic: Schematic,
    point: tuple[float, float],
    tolerance: float = POINT_TOLERANCE,
) -> list[SExp]:
    """Find all wire S-expression nodes with an endpoint at or near a point.

    Matches on 0.1 mm truncating buckets (``sch_disconnect`` /
    ``sch_remove_component`` semantics); *tolerance* is converted to 0.1 mm
    units via ``int(tolerance * 10)``.
    """
    target_key = truncating_bucket(*point)
    tol_units = int(tolerance * 10)

    def _matches(x: float, y: float) -> bool:
        key = truncating_bucket(x, y)
        return abs(key[0] - target_key[0]) <= tol_units and abs(key[1] - target_key[1]) <= tol_units

    return [wire for wire, _idx in iter_wire_endpoint_matches(schematic.sexp, _matches)]


def _find_wires_at_point_indexed(
    schematic: Schematic,
    point: tuple[float, float],
    tolerance: float = INDEXED_POINT_TOLERANCE,
) -> list[tuple[SExp, int]]:
    """Find wires with an endpoint within a float *tolerance* of *point*.

    Returns ``(wire_sexp, endpoint_index)`` tuples (``sch_move_component``
    semantics).
    """
    return find_wires_at_point_with_index(schematic.sexp, point, tolerance)


def _find_symbol_sexp(schematic: Schematic, reference: str) -> SExp | None:
    """Find the top-level symbol S-expression node by reference designator."""
    for sym_sexp in schematic.sexp.find_all("symbol"):
        for prop in sym_sexp.find_all("property"):
            prop_name = prop.get_string(0)
            if prop_name == "Reference":
                prop_value = prop.get_string(1)
                if prop_value == reference:
                    return sym_sexp
    return None


def _build_no_connect_sexp(x: float, y: float) -> SExp:
    """Build an S-expression node for a no_connect marker."""
    return SExp.list(
        "no_connect",
        SExp.list("at", x, y),
        SExp.list("uuid", str(uuid_mod.uuid4())),
    )


def _auto_reference(sch: Schematic, prefix: str = "C") -> str:
    """Find the next available reference designator for a given prefix.

    Scans existing symbols and returns ``prefix + (max_N + 1)``
    where max_N is the highest existing number for that prefix.
    Falls back to ``prefix + "1"`` if none exist.
    """
    max_num = 0
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for sym in sch.symbols:
        ref = sym.reference
        m = pattern.match(ref)
        if m:
            num = int(m.group(1))
            if num > max_num:
                max_num = num
    return f"{prefix}{max_num + 1}"


def _find_subsheet_files(text: str, schematic_dir: Path) -> list[Path]:
    """Extract sub-sheet file paths from schematic text.

    Looks for (sheet ... (property "Sheetfile" "filename.kicad_sch" ...)) blocks.
    """
    subsheets = []
    pattern = re.compile(r'\(property "Sheetfile" "([^"]+)"')
    for match in pattern.finditer(text):
        subsheet_path = schematic_dir / match.group(1)
        if subsheet_path.exists():
            subsheets.append(subsheet_path)
    return subsheets


def _collect_schematic_files(root_path: Path) -> list[Path]:
    """Recursively collect all schematic files in the hierarchy.

    Returns list starting with root, followed by sub-sheets (depth-first).
    """
    visited: set[Path] = set()
    result: list[Path] = []

    def _walk(sch_path: Path) -> None:
        resolved = sch_path.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        result.append(sch_path)

        try:
            text = sch_path.read_text(encoding="utf-8")
        except OSError:
            return

        for sub in _find_subsheet_files(text, sch_path.parent):
            _walk(sub)

    _walk(root_path)
    return result
