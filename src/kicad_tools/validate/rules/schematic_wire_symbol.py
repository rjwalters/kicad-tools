"""Schematic wire-through-symbol lint (Issue #7996).

One warning-severity cosmetic rule for the ``sch_fields`` category of
``kct check``:

* ``sch_wire_through_symbol`` -- a wire segment whose interior passes
  through another symbol's placed body bounding box.  Such a wire is
  visually ambiguous (it reads as connected to a part it merely crosses).

Scope and semantics:

* Body boxes come from :func:`kicad_tools.schema.field_geometry.placed_body_bbox`
  (rotation, mirror and placed unit already applied), the same box the
  ``sch_fields`` rules use.
* A wire is exempt from a symbol when one of its endpoints lands on one of
  that symbol's own pins -- a connection stub into the part is not a crossing.
* A wire that only touches the box edge, or runs along it, does not fire; the
  segment must enter the box interior.
* Power/virtual symbols (Reference starting with ``#``) are skipped, as are
  symbols whose ``lib_id`` cannot be resolved (stderr warning, no crash) and
  bodies with no area (e.g. a single-line power glyph).
* Sub-sheets are linted too, each unique sheet file once; crossings never
  span sheet files.

The rule is WARNING severity and ``advisory-quality`` in
:attr:`DRCChecker.RULE_CATEGORY`, so it never blocks a fab gate.

Attribution: written from this repo's own schematic model; no code was copied
from upstream ``mixelpixx/KiCAD-MCP-Server`` (pinned ``628d320f``), which only
motivated the check, so no MIT notice is required.

Determinism: findings are sorted by (sheet file name, wire start, wire end,
symbol reference) and values are rendered with fixed precision.
"""

from __future__ import annotations

import sys
from pathlib import Path

from kicad_tools.schema.field_geometry import BBox, placed_body_bbox

from ..violations import DRCResults, DRCViolation
from .schematic_fields import _sheet_files

__all__ = [
    "RULE_WIRE_THROUGH_SYMBOL",
    "check_schematic_wire_symbol",
]

RULE_WIRE_THROUGH_SYMBOL = "sch_wire_through_symbol"

#: Shrink applied to a body box before the crossing test so a wire that merely
#: touches or runs along the edge does not count (mm).
_EDGE_EPS_MM = 1e-3

#: Tolerance for "endpoint sits on a pin" (mm).
_PIN_EPS_MM = 1e-2


def _segment_enters_box(p0: tuple[float, float], p1: tuple[float, float], box: BBox) -> bool:
    """Liang-Barsky: does the segment have a positive-length part inside *box*?"""
    min_x, min_y, max_x, max_y = box
    dx = p1[0] - p0[0]
    dy = p1[1] - p0[1]
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, p0[0] - min_x),
        (dx, max_x - p0[0]),
        (-dy, p0[1] - min_y),
        (dy, max_y - p0[1]),
    ):
        if p == 0:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return False
            t0 = max(t0, r)
        else:
            if r < t0:
                return False
            t1 = min(t1, r)
    # A point-sized wire, or one that only grazes the box, is not a crossing.
    return t1 > t0


def _near(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) <= _PIN_EPS_MM and abs(a[1] - b[1]) <= _PIN_EPS_MM


def _lint_sheet(sheet_path: Path) -> list[tuple[tuple, DRCViolation]]:
    """Lint one sheet file; return (sort_key, violation) pairs."""
    from kicad_tools.schema import Schematic
    from kicad_tools.schema.symbol import SymbolInstance

    try:
        schematic = Schematic.load(sheet_path)
    except Exception as exc:
        print(
            f"Warning: sch_wire_through_symbol: could not parse {sheet_path.name} ({exc}); skipped",
            file=sys.stderr,
        )
        return []

    sheet_name = sheet_path.name

    # (reference, shrunk body box, pin positions)
    bodies: list[tuple[str, BBox, list[tuple[float, float]]]] = []
    for sym_sexp in schematic.sexp.find_children("symbol"):
        inst = SymbolInstance.from_sexp(sym_sexp)
        reference = inst.reference
        if reference.startswith("#"):
            continue

        lib_sym = schematic.get_lib_symbol_resolved(inst.lib_id)
        if lib_sym is None:
            print(
                f"Warning: sch_wire_through_symbol: {sheet_name}: {reference}: lib_id "
                f"'{inst.lib_id}' not found in embedded lib_symbols; skipped",
                file=sys.stderr,
            )
            continue

        min_x, min_y, max_x, max_y = placed_body_bbox(
            lib_sym,
            inst.position,
            rotation=inst.rotation,
            mirror=inst.mirror,
            unit=inst.unit,
        )
        if max_x - min_x <= 2 * _EDGE_EPS_MM or max_y - min_y <= 2 * _EDGE_EPS_MM:
            continue  # No interior to cross.
        shrunk = (
            min_x + _EDGE_EPS_MM,
            min_y + _EDGE_EPS_MM,
            max_x - _EDGE_EPS_MM,
            max_y - _EDGE_EPS_MM,
        )
        pins = list(
            lib_sym.get_all_pin_positions(inst.position, inst.rotation, inst.mirror).values()
        )
        bodies.append((reference, shrunk, pins))

    findings: list[tuple[tuple, DRCViolation]] = []
    for wire in schematic.wires:
        for reference, box, pins in bodies:
            if any(_near(wire.start, p) or _near(wire.end, p) for p in pins):
                continue  # Connection stub into this symbol's own pin.
            if not _segment_enters_box(wire.start, wire.end, box):
                continue
            message = (
                f"wire ({wire.start[0]:.2f}, {wire.start[1]:.2f}) -> "
                f"({wire.end[0]:.2f}, {wire.end[1]:.2f}) passes through "
                f"{reference} body [{sheet_name}]"
            )
            findings.append(
                (
                    (sheet_name, str(sheet_path), wire.start, wire.end, reference),
                    DRCViolation(
                        rule_id=RULE_WIRE_THROUGH_SYMBOL,
                        severity="warning",
                        message=message,
                        location=wire.start,
                        items=(reference,),
                    ),
                )
            )
    return findings


def check_schematic_wire_symbol(sch_path: Path) -> DRCResults:
    """Run the wire-through-symbol lint (Issue #7996).

    Args:
        sch_path: Path to the root ``.kicad_sch``; sub-sheets are linted too.

    Returns:
        :class:`DRCResults` with only WARNING-severity findings, sorted
        deterministically.
    """
    results = DRCResults()
    results.rules_checked = 1
    results.rules_checked_by_rule[RULE_WIRE_THROUGH_SYMBOL] = 1

    findings: list[tuple[tuple, DRCViolation]] = []
    for sheet_path in _sheet_files(sch_path):
        findings.extend(_lint_sheet(sheet_path))

    findings.sort(key=lambda pair: pair[0])
    for _, violation in findings:
        results.add(violation)
    return results
