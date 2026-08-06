"""Schematic field-geometry lint (Issue #4595).

Two warning-severity legibility rules for the ``sch_fields`` category of
``kct check``:

* ``sch_field_offset`` -- a visible ``Reference``/``Value`` field placed
  farther than a threshold from its symbol's placed body bounding box
  (measured with :func:`kicad_tools.schema.field_geometry.field_offset_mm`,
  the same metric ``kct sch tidy`` fixes, so "lint reports it, tidy fixes
  it" compose).  A field fires only when its offset is STRICTLY GREATER
  than the threshold; a field exactly at the threshold does not fire.
* ``sch_field_overlap`` -- a visible field's estimated text bounding box
  intersects another symbol's placed body bbox, or another symbol's
  visible field text bbox (the ``+3.3VA9`` superimposed-label composite
  from the motivating report).  Each colliding pair is reported once.

Both rules are WARNING severity only and are classified
``advisory-quality`` in :attr:`DRCChecker.RULE_CATEGORY` -- they never
raise the error count, so they can never block a fab gate (``kct build``
/ ``kct pipeline``).  Under ``--strict`` they become fatal by the
pre-existing global warnings-are-fatal contract, same as
``silk_over_copper`` / ``copper_sliver`` -- no special-casing here.

Threshold policy (recorded per the issue #4595 curator decision): v1
ships a FIXED default of 15.0 mm (overridable via ``kct check
--sch-field-threshold``).  The adaptive per-sheet-median outlier variant
suggested in the original report is explicitly DEFERRED: healthy sheets
measure ~4-6 mm median offsets and the motivating broken sheet 36 mm, so
a fixed 15 mm cleanly separates the observed populations without the
determinism/explainability cost of a data-dependent threshold.

Text-bbox approximation: KiCad does not store rendered text extents in
the schematic, so the overlap rule estimates each field's box from its
font height ``h`` (from ``(effects (font (size h w)))``, default 1.27 mm)
as ``height = h`` and ``width = 0.8 * h`` per character -- adequate for a
warning-severity heuristic.  The anchor is treated as the text center
unless a ``(justify left|right)`` effect says otherwise; vertically the
anchor is always treated as centered.  Rotated fields (90/270) swap the
box axes.

Scope (mirrors ``kct sch tidy`` / issue #4596 so lint and remediation
agree):

* only ``Reference`` and ``Value`` fields are linted;
* hidden fields (``(effects ... hide)``) are skipped;
* power/virtual symbols (Reference starting with ``#``) are skipped
  entirely -- neither their fields nor their bodies participate;
* symbols whose ``lib_id`` cannot be resolved from the embedded
  ``lib_symbols`` are skipped with a stderr warning, not a crash;
* field/field overlap is only checked ACROSS symbols (a symbol's own
  Reference/Value pair is not compared -- cross-symbol composites were
  the motivating defect, and tidy places same-symbol fields on opposite
  body sides);
* a field is never compared against its own symbol's body (fields
  legitimately sit inside large bodies, e.g. an IC's Value).

Hierarchy: sub-sheets referenced by the root's ``(sheet ...)`` nodes are
linted too (the motivating defect lived on a sub-sheet); each unique
sheet file is linted exactly once, with a visited-set guard against
sheet cycles.  Overlaps are only meaningful within one sheet, so
collision checks never cross sheet files.

Determinism: findings are sorted by (sheet file name, reference, field
name, rule id, message), and all measured values are rendered with fixed
precision, so repeated runs are byte-identical for CI.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from kicad_tools.schema.field_geometry import (
    BBox,
    field_offset_mm,
    placed_body_bbox,
)
from kicad_tools.sexp import SExp

from ..violations import DRCResults, DRCViolation

__all__ = [
    "DEFAULT_SCH_FIELD_THRESHOLD_MM",
    "RULE_FIELD_OFFSET",
    "RULE_FIELD_OVERLAP",
    "check_schematic_fields",
    "field_text_bbox",
]

#: Fixed v1 offset threshold (mm); see module docstring for the recorded
#: adaptive-vs-fixed decision.
DEFAULT_SCH_FIELD_THRESHOLD_MM = 15.0

RULE_FIELD_OFFSET = "sch_field_offset"
RULE_FIELD_OVERLAP = "sch_field_overlap"

#: Fields in scope (mirrors ``kct sch tidy``'s TIDY_FIELDS).
LINT_FIELDS = ("Reference", "Value")

#: KiCad default schematic font height in mm.
DEFAULT_FONT_HEIGHT_MM = 1.27

#: Estimated advance width per character, as a fraction of font height.
#: A crude monospace approximation -- see the module docstring.
CHAR_WIDTH_FACTOR = 0.8


def field_text_bbox(
    text: str,
    at: tuple[float, float],
    rotation: float = 0.0,
    font_height_mm: float = DEFAULT_FONT_HEIGHT_MM,
    justify: str = "",
) -> BBox:
    """Estimate the axis-aligned bbox of a rendered field text.

    Args:
        text: The rendered string (the property *value*, e.g. ``"C13"``).
        at: The field anchor position in sheet coordinates.
        rotation: Field text rotation in degrees; 90/270 swap the axes.
        font_height_mm: Font height ``h`` from the property's effects.
        justify: Horizontal justification (``""``/``"left"``/``"right"``).
            Applied along the text run direction for horizontal text;
            vertical (90/270) text is always treated as center-anchored.

    Returns:
        ``(min_x, min_y, max_x, max_y)`` in sheet coordinates.  This is a
        heuristic estimate (see module docstring), not a glyph-accurate
        extent.
    """
    x, y = at
    length = max(len(text), 1) * CHAR_WIDTH_FACTOR * font_height_mm
    height = font_height_mm

    vertical = rotation % 180 == 90
    if vertical:
        # Text runs vertically; treat the anchor as the box center.
        return (x - height / 2, y - length / 2, x + height / 2, y + length / 2)

    if justify == "left":
        min_x, max_x = x, x + length
    elif justify == "right":
        min_x, max_x = x - length, x
    else:
        min_x, max_x = x - length / 2, x + length / 2
    return (min_x, y - height / 2, max_x, y + height / 2)


def _bboxes_overlap(a: BBox, b: BBox) -> bool:
    """Strict axis-aligned intersection (touching edges do NOT overlap)."""
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _is_hidden(prop_sexp: SExp) -> bool:
    """Check whether a property is hidden.

    Handles both the KiCad 8+ ``(effects ... (hide yes))`` list form and
    the older bare-atom ``(effects ... hide)`` form (same logic as
    ``kct sch tidy``).
    """
    effects = prop_sexp.find_child("effects")
    if effects is None:
        return False
    if effects.find_child("hide") is not None:
        return True
    return any(c.is_atom and c.value == "hide" for c in effects.children)


def _font_height_mm(prop_sexp: SExp) -> float:
    """Extract the font height from ``(effects (font (size h w)))``."""
    effects = prop_sexp.find_child("effects")
    if effects is None:
        return DEFAULT_FONT_HEIGHT_MM
    font = effects.find_child("font")
    if font is None:
        return DEFAULT_FONT_HEIGHT_MM
    size = font.find_child("size")
    if size is None:
        return DEFAULT_FONT_HEIGHT_MM
    height = size.get_float(0)
    return height if height and height > 0 else DEFAULT_FONT_HEIGHT_MM


def _justify(prop_sexp: SExp) -> str:
    """Extract the horizontal justification token (``""`` = centered)."""
    effects = prop_sexp.find_child("effects")
    if effects is None:
        return ""
    justify = effects.find_child("justify")
    if justify is None:
        return ""
    for child in justify.children:
        if child.is_atom and child.value in ("left", "right"):
            return str(child.value)
    return ""


@dataclass
class _FieldRecord:
    """A visible in-scope field of a linted symbol."""

    reference: str
    field_name: str
    text: str
    position: tuple[float, float]
    text_bbox: BBox


def _sheet_files(root: Path) -> list[Path]:
    """Enumerate the root sheet plus all reachable sub-sheet files.

    Breadth-first over each sheet's ``(sheet ...)`` nodes, resolving the
    ``Sheetfile`` property relative to the referencing sheet's directory.
    Each unique resolved file is returned once; missing or unparseable
    sheets are skipped with a stderr warning (never a crash), and a
    visited set guards against sheet cycles.
    """
    from kicad_tools.schema import Schematic

    ordered: list[Path] = []
    visited: set[Path] = set()
    queue: list[Path] = [root.resolve()]

    while queue:
        sheet_path = queue.pop(0)
        if sheet_path in visited:
            continue
        visited.add(sheet_path)
        if not sheet_path.exists():
            print(
                f"Warning: sch_fields: sheet file not found: {sheet_path}; skipped",
                file=sys.stderr,
            )
            continue
        ordered.append(sheet_path)
        try:
            schematic = Schematic.load(sheet_path)
        except Exception as exc:
            # Already appended: the caller's per-sheet lint re-loads and
            # re-warns; keep enumeration resilient either way.
            print(
                f"Warning: sch_fields: could not parse {sheet_path.name} "
                f"for sheet enumeration ({exc})",
                file=sys.stderr,
            )
            continue
        for sheet in schematic.sheets:
            if not sheet.filename:
                continue
            child = Path(sheet.filename)
            if not child.is_absolute():
                child = sheet_path.parent / child
            queue.append(child.resolve())

    return ordered


def _lint_sheet(
    sheet_path: Path,
    threshold_mm: float,
) -> list[tuple[tuple, DRCViolation]]:
    """Lint one sheet file; return (sort_key, violation) pairs."""
    from kicad_tools.schema import Schematic
    from kicad_tools.schema.symbol import SymbolInstance

    try:
        schematic = Schematic.load(sheet_path)
    except Exception as exc:
        print(
            f"Warning: sch_fields: could not parse {sheet_path.name} ({exc}); skipped",
            file=sys.stderr,
        )
        return []

    sheet_name = sheet_path.name
    findings: list[tuple[tuple, DRCViolation]] = []

    def _key(reference: str, field_name: str, rule_id: str, message: str) -> tuple:
        # AC ordering: (sheet file name, reference, field name, rule id);
        # full path + message break residual ties deterministically.
        return (sheet_name, str(sheet_path), reference, field_name, rule_id, message)

    # Pass 1: collect per-symbol body bboxes and visible in-scope fields.
    bodies: list[tuple[str, BBox]] = []  # (reference, body bbox)
    fields: list[_FieldRecord] = []
    for sym_sexp in schematic.sexp.find_children("symbol"):
        inst = SymbolInstance.from_sexp(sym_sexp)
        reference = inst.reference

        if reference.startswith("#"):
            # Power/virtual symbols: fully out of scope (mirrors sch tidy).
            continue

        lib_sym = schematic.get_lib_symbol_resolved(inst.lib_id)
        if lib_sym is None:
            print(
                f"Warning: sch_fields: {sheet_name}: {reference}: lib_id "
                f"'{inst.lib_id}' not found in embedded lib_symbols; skipped",
                file=sys.stderr,
            )
            continue

        bbox = placed_body_bbox(
            lib_sym,
            inst.position,
            rotation=inst.rotation,
            mirror=inst.mirror,
            unit=inst.unit,
        )

        symbol_fields: list[_FieldRecord] = []
        for prop_sexp in sym_sexp.find_children("property"):
            field_name = prop_sexp.get_string(0) or ""
            if field_name not in LINT_FIELDS:
                continue
            if _is_hidden(prop_sexp):
                continue
            at_node = prop_sexp.find_child("at")
            if at_node is None:
                continue
            pos = (at_node.get_float(0) or 0.0, at_node.get_float(1) or 0.0)
            rot = at_node.get_float(2) or 0.0
            text = prop_sexp.get_string(1) or ""

            # Sub-check 1: field far from body (strictly greater than).
            offset = field_offset_mm(pos, bbox)
            if offset > threshold_mm:
                message = (
                    f"{reference}.{field_name} {offset:.1f}mm from body "
                    f"(threshold {threshold_mm:.1f}mm) [{sheet_name}]"
                )
                findings.append(
                    (
                        _key(reference, field_name, RULE_FIELD_OFFSET, message),
                        DRCViolation(
                            rule_id="sch_field_offset",
                            severity="warning",
                            message=message,
                            location=pos,
                            actual_value=round(offset, 3),
                            required_value=threshold_mm,
                            items=(reference,),
                        ),
                    )
                )

            symbol_fields.append(
                _FieldRecord(
                    reference=reference,
                    field_name=field_name,
                    text=text,
                    position=pos,
                    text_bbox=field_text_bbox(
                        text,
                        pos,
                        rotation=rot,
                        font_height_mm=_font_height_mm(prop_sexp),
                        justify=_justify(prop_sexp),
                    ),
                )
            )

        bodies.append((reference, bbox))
        fields.extend(symbol_fields)

    # Deterministic collision scan order regardless of file order.
    bodies.sort(key=lambda b: b[0])
    fields.sort(key=lambda f: (f.reference, f.field_name))

    # Sub-check 2a: field text bbox vs ANOTHER symbol's body bbox.
    for record in fields:
        for body_ref, body_bbox in bodies:
            if body_ref == record.reference:
                continue  # A field over its own body is normal.
            if _bboxes_overlap(record.text_bbox, body_bbox):
                message = (
                    f"{record.reference}.{record.field_name} text "
                    f"overlaps {body_ref} body [{sheet_name}]"
                )
                findings.append(
                    (
                        _key(record.reference, record.field_name, RULE_FIELD_OVERLAP, message),
                        DRCViolation(
                            rule_id="sch_field_overlap",
                            severity="warning",
                            message=message,
                            location=record.position,
                            items=(record.reference, body_ref),
                        ),
                    )
                )

    # Sub-check 2b: field text bbox vs another SYMBOL's field text bbox
    # (cross-symbol only; each unordered pair reported once, attributed
    # to the lexicographically-first field).
    for i, a in enumerate(fields):
        for b in fields[i + 1 :]:
            if a.reference == b.reference:
                continue
            if _bboxes_overlap(a.text_bbox, b.text_bbox):
                message = (
                    f"{a.reference}.{a.field_name} text overlaps "
                    f"{b.reference}.{b.field_name} text [{sheet_name}]"
                )
                findings.append(
                    (
                        _key(a.reference, a.field_name, RULE_FIELD_OVERLAP, message),
                        DRCViolation(
                            rule_id="sch_field_overlap",
                            severity="warning",
                            message=message,
                            location=a.position,
                            items=(a.reference, b.reference),
                        ),
                    )
                )

    return findings


def check_schematic_fields(
    sch_path: Path,
    threshold_mm: float = DEFAULT_SCH_FIELD_THRESHOLD_MM,
) -> DRCResults:
    """Run the schematic field-geometry lint (Issue #4595).

    Args:
        sch_path: Path to the root ``.kicad_sch``.  Sub-sheets referenced
            via ``(sheet ...)`` nodes are linted too, each unique file
            once.
        threshold_mm: ``sch_field_offset`` distance threshold in mm
            (strictly-greater-than comparison).

    Returns:
        :class:`DRCResults` containing only WARNING-severity findings,
        deterministically sorted by (sheet file name, reference, field
        name, rule id).
    """
    results = DRCResults()
    results.rules_checked = 2
    results.rules_checked_by_rule[RULE_FIELD_OFFSET] = 1
    results.rules_checked_by_rule[RULE_FIELD_OVERLAP] = 1

    findings: list[tuple[tuple, DRCViolation]] = []
    for sheet_path in _sheet_files(sch_path):
        findings.extend(_lint_sheet(sheet_path, threshold_mm))

    findings.sort(key=lambda pair: pair[0])
    for _, violation in findings:
        results.add(violation)
    return results
