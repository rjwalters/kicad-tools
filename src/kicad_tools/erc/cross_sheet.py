"""Cross-sheet ERC helpers.

Provides cross-sheet checks that complement KiCad's built-in ERC:

1. **Duplicate reference detection** -- detects duplicate reference
   designators that span different hierarchical sheets.  KiCad's built-in
   ERC only reliably detects duplicates within a single flat schematic;
   this module fills the gap for hierarchical designs.

2. **Sheet re-attribution** -- moves position- and symbol-based
   violations that KiCad reports on the root sheet to the child sheet that
   actually contains the offending wire / symbol.

There is deliberately no phantom ``wire_dangling`` filter (Issue #7170).
The one added for #2205 keyed on a top-level ``pos`` that KiCad 10 never
emits, so it dropped nothing; fed the per-item ``pos`` KiCad 10 does emit
(at 1/100 scale) it would have dropped real floating wires.  A reinstated
filter needs a real kicad-cli reproducer and must report a count.

There is deliberately no name- or sheet-count-based suppression of
``isolated_pin_label`` / ``single_global_label`` or ``power_pin_not_driven``
here (Issue #7161).  KiCad 10's ERC already aggregates global labels and
power drivers across the whole hierarchy, so those heuristics only ever
removed true positives.  Whether a label really reaches a second pin is
answered by connectivity in
:func:`kicad_tools.validate.sch_orphan_label.find_orphan_labels`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .violation import ERCViolation, ERCViolationType, Severity


@dataclass
class _SymbolRef:
    """Internal record for a symbol's reference on a specific sheet."""

    reference: str
    value: str
    sheet_path: str
    uuid: str
    lib_id: str


def check_cross_sheet_duplicates(root_schematic: str) -> list[ERCViolation]:
    """Check for duplicate reference designators across hierarchical sheets.

    Traverses the full schematic hierarchy starting from *root_schematic*,
    collects every symbol reference, and reports any reference that appears
    on more than one sheet (or more than once on the same sheet with
    distinct UUIDs that are not multi-unit instances of the same component).

    Power symbols (lib_id starting with ``power:``) are excluded.

    Args:
        root_schematic: Path to the root ``.kicad_sch`` file.

    Returns:
        A list of :class:`ERCViolation` objects, one per duplicated
        reference designator.
    """
    from kicad_tools.schema.hierarchy import build_hierarchy
    from kicad_tools.schema.schematic import Schematic

    hierarchy = build_hierarchy(root_schematic)

    # Collect all (reference, sheet_path, value, uuid, lib_id) tuples.
    all_refs: list[_SymbolRef] = []
    # Each placement of a repeated sheet uses its own designators, from the
    # symbol's ``(instances (path ...))`` entry for the placement (issue
    # #7616). A symbol without one falls back to its ``Reference`` property
    # and is counted once per file, not once per placement.
    fallback_seen: set[tuple[str, int]] = set()
    loaded: dict[str, Schematic] = {}

    for node in hierarchy.all_nodes():
        sch = loaded.get(node.path)
        if sch is None:
            try:
                sch = Schematic.load(node.path)
            except Exception:
                continue
            loaded[node.path] = sch

        sheet_path = node.get_path_string()

        for sym_index, sym in enumerate(sch.symbols):
            ref = sym.instance_reference(node.instance_path)
            if ref is None:
                key = (node.path, sym_index)
                if key in fallback_seen:
                    continue
                fallback_seen.add(key)
                ref = sym.reference
            if not ref or ref.startswith("#"):
                # Skip power flags and virtual symbols
                continue
            if sym.lib_id.startswith("power:"):
                continue

            all_refs.append(
                _SymbolRef(
                    reference=ref,
                    value=sym.value,
                    sheet_path=sheet_path,
                    uuid=sym.uuid,
                    lib_id=sym.lib_id,
                )
            )

    # Group by reference designator.
    by_ref: dict[str, list[_SymbolRef]] = defaultdict(list)
    for entry in all_refs:
        by_ref[entry.reference].append(entry)

    # Build a set of all used reference numbers per prefix for suggestion logic.
    used_numbers: dict[str, set[int]] = defaultdict(set)
    ref_pattern = re.compile(r"^([A-Za-z]+)(\d+)$")
    for ref_str in by_ref:
        m = ref_pattern.match(ref_str)
        if m:
            prefix = m.group(1)
            num = int(m.group(2))
            used_numbers[prefix].add(num)

    violations: list[ERCViolation] = []

    for ref, entries in sorted(by_ref.items()):
        if len(entries) < 2:
            continue

        # Filter out multi-unit symbols on the same sheet.
        # Multi-unit symbols share the same lib_id and sheet_path but have
        # different unit numbers; their UUIDs differ but the reference is
        # intentionally shared.  We only flag a reference when it appears
        # in entries that differ by sheet_path, or that share a sheet_path
        # but have a different lib_id (truly distinct components).
        unique_instances: dict[tuple[str, str], _SymbolRef] = {}
        for entry in entries:
            key = (entry.sheet_path, entry.lib_id)
            if key not in unique_instances:
                unique_instances[key] = entry

        if len(unique_instances) < 2:
            # All occurrences are multi-unit on the same sheet -- not a dup.
            continue

        # Build a human-readable description.
        sheet_details = []
        for entry in unique_instances.values():
            sheet_details.append(f"{entry.sheet_path} (value={entry.value})")

        suggestion = _suggest_next_available(ref, used_numbers, ref_pattern)

        description = f"Reference '{ref}' is used on multiple sheets: " + "; ".join(sheet_details)

        violation = ERCViolation(
            type=ERCViolationType.DUPLICATE_REFERENCE,
            type_str=ERCViolationType.DUPLICATE_REFERENCE.value,
            severity=Severity.ERROR,
            description=description,
            sheet="/",
            suggestions=[suggestion] if suggestion else [],
        )
        violations.append(violation)

    return violations


def _suggest_next_available(
    ref: str,
    used_numbers: dict[str, set[int]],
    pattern: re.Pattern[str],
) -> str | None:
    """Suggest the next available reference number for a given prefix.

    For example, if ``R12`` is duplicated and R1-R14 all exist, suggest
    ``R15``.

    Returns:
        A human-readable suggestion string, or ``None`` if the reference
        does not follow the ``<prefix><number>`` pattern.
    """
    m = pattern.match(ref)
    if not m:
        return None

    prefix = m.group(1)
    nums = used_numbers.get(prefix, set())
    max_num = max(nums) if nums else 0
    next_num = min(n for n in range(1, max_num + 2) if n not in nums)
    return f"Consider renaming the duplicate to {prefix}{next_num}"


# ---------------------------------------------------------------------------
# Wire-dangling / endpoint-off-grid sheet re-attribution
# ---------------------------------------------------------------------------

_WIRE_POSITION_TYPES: frozenset[str] = frozenset(
    {
        "wire_dangling",
        "endpoint_off_grid",
        "no_connect_dangling",
        "label_dangling",
        "global_label_dangling",
    }
)


def _build_wire_endpoint_map(
    root_schematic: str,
    tolerance: float = 0.1,
) -> dict[tuple[float, float], str]:
    """Build a mapping of wire endpoint and midpoint coordinates to sheet paths.

    Iterates every child sheet in the hierarchy, loads its wires, and
    records each wire start/end coordinate together with the sheet's
    hierarchical path string (e.g. ``/DAC``).  Wire midpoints are also
    indexed so that T-junction violations (where KiCad reports a
    coordinate on the interior of a wire segment rather than at an
    endpoint) can be matched.

    Coordinates are rounded to *tolerance*-sized buckets so that
    floating-point imprecision does not prevent matching.  The default
    tolerance of 0.1 mm accommodates coordinate rounding differences
    between KiCad's ERC report and the ``.kicad_sch`` wire definitions.

    The root sheet's own wires are **not** included -- the purpose of
    this map is to find a *child* sheet that owns a position that KiCad
    incorrectly attributed to the root.
    """
    from kicad_tools.schema import Schematic
    from kicad_tools.schema.hierarchy import build_hierarchy

    hierarchy = build_hierarchy(root_schematic)
    inv: dict[tuple[float, float], str] = {}

    def _snap(v: float) -> float:
        return round(v / tolerance) * tolerance

    for node in hierarchy.all_nodes():
        if node.is_root:
            continue
        try:
            sch = Schematic.load(node.path)
        except Exception:
            continue
        sheet_path = node.get_path_string()
        for wire in sch.wires:
            sx, sy = wire.start[0], wire.start[1]
            ex, ey = wire.end[0], wire.end[1]
            inv[(_snap(sx), _snap(sy))] = sheet_path
            inv[(_snap(ex), _snap(ey))] = sheet_path
            # Index the midpoint so T-junction violations can be matched.
            mx = (sx + ex) / 2.0
            my = (sy + ey) / 2.0
            inv[(_snap(mx), _snap(my))] = sheet_path

    return inv


def reattribute_wire_dangling_violations(
    violations: list[dict],
    root_schematic: str,
) -> list[dict]:
    """Re-attribute wire-position-dependent violations from the root sheet
    to the correct child sheet.

    KiCad's ERC JSON groups violations under a ``sheets`` array.  For
    wire-position-dependent types (``wire_dangling``, ``endpoint_off_grid``,
    ``no_connect_dangling``, ``label_dangling``, ``global_label_dangling``),
    KiCad sometimes attributes them to the root sheet path (``/``) even
    when the actual wire endpoint lives in a child sheet.

    This function matches each root-attributed violation's ``pos``
    coordinates against wire endpoints and midpoints across the hierarchy
    and updates ``_sheet_path`` to the correct child sheet when a match
    is found.

    Additionally the violation ``description`` is enriched with the
    position coordinates so the user can locate the offending wire.

    Args:
        violations: List of raw violation dicts (must already have
            ``_sheet_path`` injected by the caller).
        root_schematic: Path to the root ``.kicad_sch`` file.

    Returns:
        The same list (mutated in place for efficiency) with updated
        ``_sheet_path`` values and enriched descriptions.
    """
    target_types = _WIRE_POSITION_TYPES

    # Quick check: skip expensive hierarchy traversal when unnecessary.
    has_target = any(
        v.get("type", "") in target_types and v.get("_sheet_path", "") == "/" for v in violations
    )
    if not has_target:
        # Still enrich descriptions with coordinates even when no
        # re-attribution is needed.
        for v in violations:
            if v.get("type", "") in target_types:
                _enrich_description_with_pos(v)
        return violations

    tolerance = 0.1
    endpoint_map = _build_wire_endpoint_map(root_schematic, tolerance=tolerance)

    def _snap(val: float) -> float:
        return round(val / tolerance) * tolerance

    for v in violations:
        vtype = v.get("type", "")
        if vtype not in target_types:
            continue

        # Enrich description with coordinates regardless of re-attribution.
        _enrich_description_with_pos(v)

        if v.get("_sheet_path", "") != "/":
            continue

        pos = v.get("pos", {})
        x = pos.get("x", None)
        y = pos.get("y", None)
        if x is None or y is None:
            continue

        key = (_snap(float(x)), _snap(float(y)))
        sheet_path = endpoint_map.get(key)
        if sheet_path is not None:
            v["_sheet_path"] = sheet_path

    return violations


def _enrich_description_with_pos(v: dict) -> None:
    """Append ``at (x, y)`` to the violation description if position data
    is available and not already present."""
    pos = v.get("pos", {})
    x = pos.get("x", None)
    y = pos.get("y", None)
    if x is None or y is None:
        return
    coord_str = f"at ({float(x):.1f}, {float(y):.1f})"
    desc = v.get("description", "")
    if coord_str not in desc:
        v["description"] = f"{desc} {coord_str}"


# ---------------------------------------------------------------------------
# Symbol-based violation sheet re-attribution
# ---------------------------------------------------------------------------

# Violation types that reference specific symbols (via UUID or reference
# designator in the items array) and may be mis-attributed to the root
# sheet in hierarchical designs.
_SYMBOL_VIOLATION_TYPES: frozenset[str] = frozenset(
    {
        "pin_not_connected",
        "pin_not_driven",
        "power_pin_not_driven",
        "different_unit_value",
        "different_unit_footprint",
        "unresolved_variable",
        "extra_units",
        "missing_units",
    }
)

# Label-based violation types that can also be re-attributed using the
# label's UUID from the items array.
_LABEL_VIOLATION_TYPES: frozenset[str] = frozenset(
    {
        "global_label_dangling",
        "label_dangling",
        "single_global_label",
        "isolated_pin_label",
    }
)


def _build_symbol_sheet_map(root_schematic: str) -> dict[str, str]:
    """Build a mapping of symbol UUID and reference to sheet path.

    Traverses the full schematic hierarchy, collecting each symbol's
    UUID and reference designator, and mapping them to the sheet path
    string (e.g. ``"/DAC"``, ``"/Power"``).

    Args:
        root_schematic: Path to the root ``.kicad_sch`` file.

    Returns:
        A dict mapping UUID strings and reference designator strings
        to their containing sheet path.  If a reference appears on
        multiple sheets only the first occurrence is recorded (the
        cross-sheet duplicate checker handles that separately).
    """
    from kicad_tools.schema.hierarchy import build_hierarchy
    from kicad_tools.schema.schematic import Schematic

    hierarchy = build_hierarchy(root_schematic)
    mapping: dict[str, str] = {}

    for node in hierarchy.all_nodes():
        try:
            sch = Schematic.load(node.path)
        except Exception:
            continue

        sheet_path = node.get_path_string()

        for sym in sch.symbols:
            if sym.uuid and sym.uuid not in mapping:
                mapping[sym.uuid] = sheet_path
            # This placement's designator (issue #7616), then the property.
            for ref in (sym.instance_reference(node.instance_path), sym.reference):
                if ref and ref not in mapping:
                    mapping[ref] = sheet_path

        # Also index labels by UUID for label-type violations.
        for gl in sch.global_labels:
            if hasattr(gl, "uuid") and gl.uuid:
                mapping[gl.uuid] = sheet_path
        for lbl in sch.labels:
            if hasattr(lbl, "uuid") and lbl.uuid:
                mapping[lbl.uuid] = sheet_path

    return mapping


def _extract_identifiers_from_items(items: list[dict]) -> list[str]:
    """Extract UUIDs and component references from a violation's items array.

    KiCad's ERC JSON ``items`` entries may contain:
    - A ``"uuid"`` key with a UUID string
    - A ``"description"`` containing a reference like ``"Pin VCC of U3"``

    Args:
        items: List of item dicts from a KiCad ERC violation.

    Returns:
        A list of identifier strings (UUIDs and/or references) that
        can be looked up in the symbol-sheet mapping.
    """
    identifiers: list[str] = []

    for item in items:
        # Direct UUID field
        uuid = item.get("uuid", "")
        if uuid:
            identifiers.append(uuid)

        # Extract reference from description patterns like:
        # "Pin VCC (power_in) of U3"
        # "Symbol U3"
        desc = item.get("description", "")
        if desc:
            # Match "of <reference>" at end of description
            m = re.search(r"\bof\s+([A-Za-z]+\d+)\b", desc)
            if m:
                identifiers.append(m.group(1))
            # Match "Symbol <reference>" at start
            m = re.search(r"\bSymbol\s+([A-Za-z]+\d+)\b", desc)
            if m:
                identifiers.append(m.group(1))

    return identifiers


def reattribute_symbol_violations(
    violations: list[dict],
    root_schematic: str,
) -> list[dict]:
    """Re-attribute symbol-based ERC violations from the root sheet to the
    correct child sheet based on component reference or UUID lookup.

    KiCad's ERC JSON sometimes attributes violations to the root sheet
    (``"/"``) even when the offending symbol lives in a child sheet.
    This affects symbol-based violation types (``pin_not_connected``,
    ``pin_not_driven``, etc.) and label-based types whose items contain
    identifiable UUIDs or references.

    This function builds a symbol-to-sheet mapping from the hierarchy
    and updates ``_sheet_path`` for violations that can be matched to
    a specific child sheet.

    Args:
        violations: List of raw violation dicts (must already have
            ``_sheet_path`` injected by the caller).
        root_schematic: Path to the root ``.kicad_sch`` file.

    Returns:
        The same list (mutated in place for efficiency) with updated
        ``_sheet_path`` values where re-attribution was possible.
    """
    target_types = _SYMBOL_VIOLATION_TYPES | _LABEL_VIOLATION_TYPES

    # Quick check: skip expensive hierarchy traversal when unnecessary.
    has_target = any(
        v.get("type", "") in target_types and v.get("_sheet_path", "") == "/" for v in violations
    )
    if not has_target:
        return violations

    symbol_map = _build_symbol_sheet_map(root_schematic)

    for v in violations:
        vtype = v.get("type", "")
        if vtype not in target_types:
            continue

        if v.get("_sheet_path", "") != "/":
            continue

        items = v.get("items", [])
        if not items:
            continue

        identifiers = _extract_identifiers_from_items(items)

        # Try each identifier against the map; use the first match
        # that points to a non-root sheet.
        for ident in identifiers:
            sheet_path = symbol_map.get(ident)
            if sheet_path and sheet_path != "/":
                v["_sheet_path"] = sheet_path
                break

    return violations
