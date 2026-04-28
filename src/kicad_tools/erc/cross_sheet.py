"""Cross-sheet ERC helpers.

Provides cross-sheet checks that complement KiCad's built-in ERC:

1. **Duplicate reference detection** -- detects duplicate reference
   designators that span different hierarchical sheets.  KiCad's built-in
   ERC only reliably detects duplicates within a single flat schematic;
   this module fills the gap for hierarchical designs.

2. **False-positive global label filtering** -- KiCad reports
   ``single_global_label`` / ``isolated_pin_label`` on a per-sheet basis
   without aggregating across the full hierarchy.  A global label that
   appears in multiple sheets is *not* truly isolated; this module
   suppresses those false positives.

3. **False-positive power-pin filtering** -- KiCad reports
   ``power_pin_not_driven`` on a per-sheet basis without checking for
   ``power_out`` drivers on other sheets.  A power net driven by a
   ``PWR_FLAG`` or voltage regulator output on one sheet should not be
   flagged as undriven on another sheet.
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

    for node in hierarchy.all_nodes():
        try:
            sch = Schematic.load(node.path)
        except Exception:
            continue

        sheet_path = node.get_path_string()

        for sym in sch.symbols:
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

        description = (
            f"Reference '{ref}' is used on multiple sheets: "
            + "; ".join(sheet_details)
        )

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
# Cross-sheet global label false-positive filtering
# ---------------------------------------------------------------------------


def build_global_label_inventory(root_schematic: str) -> dict[str, set[str]]:
    """Build an inventory of global label names to the sheets they appear on.

    Traverses the full schematic hierarchy and collects every
    ``global_label`` element.

    Args:
        root_schematic: Path to the root ``.kicad_sch`` file.

    Returns:
        A mapping of label name to the set of sheet paths where it appears.
    """
    from kicad_tools.schema.hierarchy import build_hierarchy
    from kicad_tools.schema.schematic import Schematic

    hierarchy = build_hierarchy(root_schematic)

    inventory: dict[str, set[str]] = defaultdict(set)

    for node in hierarchy.all_nodes():
        try:
            sch = Schematic.load(node.path)
        except Exception:
            continue

        sheet_path = node.get_path_string()

        for gl in sch.global_labels:
            inventory[gl.text].add(sheet_path)

    return dict(inventory)


def build_sheet_label_presence(root_schematic: str) -> set[str]:
    """Return the set of sheet paths that contain at least one label.

    Checks for local labels, global labels, and hierarchical labels.
    A sheet that contains none of these is considered label-free.

    Args:
        root_schematic: Path to the root ``.kicad_sch`` file.

    Returns:
        A set of sheet path strings (e.g. ``"/"``, ``"/Power"``) for
        sheets that have at least one label of any kind.
    """
    from kicad_tools.schema.hierarchy import build_hierarchy
    from kicad_tools.schema.schematic import Schematic

    hierarchy = build_hierarchy(root_schematic)

    sheets_with_labels: set[str] = set()

    for node in hierarchy.all_nodes():
        try:
            sch = Schematic.load(node.path)
        except Exception:
            continue

        sheet_path = node.get_path_string()

        if sch.labels or sch.global_labels or sch.hierarchical_labels:
            sheets_with_labels.add(sheet_path)

    return sheets_with_labels


def _extract_label_name(description: str, items: list[dict] | None = None) -> str | None:
    """Extract a label name from a KiCad ERC violation description.

    KiCad formats these descriptions in several ways depending on the
    version:

    **Older KiCad (label name in top-level description)**:

    * ``Label 'AUDIO_L' appears only once in the design``
    * ``Global label 'AUDIO_L' is not connected anywhere else in the
      schematic``

    **KiCad 10+ (label name in items, not top-level description)**:

    * Top-level: ``Label connected to only one pin``
    * Item:      ``Global Label 'AUDIO_L'``

    Args:
        description: The violation's top-level description string.
        items: Optional list of item dicts from the violation.  Each
            dict may contain a ``"description"`` key with the label
            name (e.g. ``"Global Label 'AUDIO_L'"``)

    Returns the extracted label name, or ``None`` if no label name
    could be parsed.
    """
    # Match quoted label name patterns in the top-level description
    m = re.search(r"[Ll]abel\s+'([^']+)'", description)
    if m:
        return m.group(1)

    m = re.search(r'"([^"]+)"', description)
    if m:
        return m.group(1)

    # KiCad 10+ puts the label name in the items array instead of the
    # top-level description.  Scan item descriptions for a quoted name.
    if items:
        for item in items:
            item_desc = item.get("description", "")
            m = re.search(r"[Ll]abel\s+'([^']+)'", item_desc)
            if m:
                return m.group(1)
            m = re.search(r'"([^"]+)"', item_desc)
            if m:
                return m.group(1)

    return None


def filter_cross_sheet_global_labels(
    violations: list[dict],
    root_schematic: str,
) -> list[dict]:
    """Remove false-positive ``single_global_label`` and ``isolated_pin_label``
    violations for global labels that appear on multiple sheets, and for
    violations reported on sheets that contain no labels at all.

    KiCad's ERC engine reports these violations per-sheet without aggregating
    across the hierarchy.  A global label such as ``AUDIO_L`` that appears in
    ``/DAC``, ``/Connectors``, and ``/Sync`` may get reported as
    "only appears once" when KiCad processes each sheet in isolation.

    KiCad may also report ``isolated_pin_label`` on a sheet (commonly the
    root sheet ``/``) that contains no labels whatsoever -- only sub-sheet
    references.  These phantom violations are suppressed by checking whether
    the reported sheet actually has any labels.

    This function builds a cross-sheet global label inventory and suppresses
    violations whose label name appears on two or more distinct sheets, as
    well as violations on label-free sheets with unparseable descriptions.

    Args:
        violations: List of raw violation dicts as parsed from KiCad's JSON
            report.  Each dict must have at least ``"type"`` and
            ``"description"`` keys.  An optional ``"_sheet_path"`` key
            identifies the sheet the violation was reported on.
        root_schematic: Path to the root ``.kicad_sch`` file used to build
            the global label inventory.

    Returns:
        A new list with false-positive violations removed.  Violations of
        other types pass through unchanged.
    """
    target_types = {"single_global_label", "isolated_pin_label"}

    # Quick check: if no target violations exist, skip the expensive
    # hierarchy traversal.
    has_target = any(v.get("type", "") in target_types for v in violations)
    if not has_target:
        return violations

    inventory = build_global_label_inventory(root_schematic)
    sheet_label_presence = build_sheet_label_presence(root_schematic)

    filtered: list[dict] = []
    for v in violations:
        vtype = v.get("type", "")
        if vtype not in target_types:
            filtered.append(v)
            continue

        label_name = _extract_label_name(
            v.get("description", ""), v.get("items")
        )
        if label_name is None:
            # Could not parse label name.  If the violation's sheet has no
            # labels at all, this is a phantom detection -- suppress it.
            sheet_path = v.get("_sheet_path", "")
            if sheet_path and sheet_path not in sheet_label_presence:
                continue
            # Sheet has labels (or no sheet info available) -- keep to be safe.
            filtered.append(v)
            continue

        sheets = inventory.get(label_name, set())
        if len(sheets) >= 2:
            # Label appears on multiple sheets -- this is a false positive.
            continue

        filtered.append(v)

    return filtered


# ---------------------------------------------------------------------------
# Cross-sheet power-pin false-positive filtering
# ---------------------------------------------------------------------------


def build_power_driver_inventory(root_schematic: str) -> set[str]:
    """Build an inventory of power net names that have a ``power_out`` driver.

    Traverses the full schematic hierarchy and inspects every symbol's
    library definition for ``power_out`` pins.  Power symbols (lib_id
    starting with ``power:``) contribute their *value* as the net name
    (e.g. a ``power:+3V3`` symbol drives the ``+3V3`` net).  Non-power
    symbols contribute the pin *name* for each ``power_out`` pin (e.g. a
    voltage regulator with a ``power_out`` pin named ``VOUT``).

    ``PWR_FLAG`` symbols are included -- they have a single ``power_out``
    pin and act as explicit power-source declarations.

    Args:
        root_schematic: Path to the root ``.kicad_sch`` file.

    Returns:
        A set of net names that have at least one ``power_out`` driver
        somewhere in the hierarchy.
    """
    from kicad_tools.schema.hierarchy import build_hierarchy
    from kicad_tools.schema.library import LibraryPin
    from kicad_tools.schema.schematic import Schematic

    hierarchy = build_hierarchy(root_schematic)

    driven_nets: set[str] = set()

    for node in hierarchy.all_nodes():
        try:
            sch = Schematic.load(node.path)
        except Exception:
            continue

        for sym in sch.symbols:
            lib_sym = sch.get_lib_symbol(sym.lib_id)
            if lib_sym is None:
                continue

            # Collect all pins with power_out type from the library def.
            power_out_pins: list[LibraryPin] = []
            for sub_sym in lib_sym.find_all("symbol"):
                for pin_sexp in sub_sym.find_all("pin"):
                    pin = LibraryPin.from_sexp(pin_sexp)
                    if pin.type == "power_out":
                        power_out_pins.append(pin)

            if not power_out_pins:
                continue

            # For power symbols the net name is the symbol's value
            # property (e.g. "+3V3", "GND").  For non-power symbols
            # (e.g. a voltage regulator) the net name is the pin name.
            if sym.lib_id.startswith("power:"):
                net_name = sym.value
                if net_name:
                    driven_nets.add(net_name)
            else:
                for pin in power_out_pins:
                    if pin.name:
                        driven_nets.add(pin.name)

    return driven_nets


def _extract_power_net_name(
    description: str, items: list[dict] | None = None
) -> str | None:
    """Extract the power net / pin name from a ``power_pin_not_driven`` violation.

    KiCad formats these violations as:

    * Description: ``"Power input pin not driven by any power output"``
      (or similar generic text)
    * Item description: ``"Pin VCC (power_in) of U1"``

    The pin name (``VCC``) is used as the power net name because KiCad
    power pins are named after the net they connect to.

    Returns:
        The extracted net name, or ``None`` if parsing fails.
    """
    # Try items first -- more reliable in modern KiCad
    if items:
        for item in items:
            item_desc = item.get("description", "")
            # Match "Pin <name> (power_in) of <ref>"
            m = re.search(r"Pin\s+(\S+)\s+\(power_in\)", item_desc)
            if m:
                return m.group(1)
            # Also match "Pin <name> of <ref>" without the type qualifier
            m = re.search(r"Pin\s+(\S+)\s+of\s+", item_desc)
            if m:
                return m.group(1)

    # Fall back to the top-level description
    m = re.search(r"Pin\s+(\S+)", description)
    if m:
        return m.group(1)

    return None


def filter_cross_sheet_power_violations(
    violations: list[dict],
    root_schematic: str,
) -> list[dict]:
    """Remove false-positive ``power_pin_not_driven`` violations for power
    nets that have a ``power_out`` driver on another sheet.

    KiCad's ERC engine evaluates ``power_pin_not_driven`` on a per-sheet
    basis without aggregating power drivers across the full hierarchy.
    A ``power_in`` pin connected to ``+3V3`` is flagged as "not driven"
    even when a ``PWR_FLAG`` or voltage regulator output on a different
    sheet drives that same net.

    This function builds a cross-sheet inventory of ``power_out`` drivers
    and suppresses violations whose power net has a driver anywhere in
    the hierarchy.

    Args:
        violations: List of raw violation dicts as parsed from KiCad's
            JSON report.  Each dict must have at least ``"type"`` and
            ``"description"`` keys.
        root_schematic: Path to the root ``.kicad_sch`` file used to
            build the power driver inventory.

    Returns:
        A new list with false-positive ``power_pin_not_driven``
        violations removed.  All other violation types pass through
        unchanged.
    """
    target_type = "power_pin_not_driven"

    # Quick check: skip the expensive hierarchy traversal if there are
    # no power_pin_not_driven violations.
    has_target = any(v.get("type", "") == target_type for v in violations)
    if not has_target:
        return violations

    driven_nets = build_power_driver_inventory(root_schematic)

    filtered: list[dict] = []
    for v in violations:
        if v.get("type", "") != target_type:
            filtered.append(v)
            continue

        net_name = _extract_power_net_name(
            v.get("description", ""), v.get("items")
        )

        if net_name is not None and net_name in driven_nets:
            # Power net has a driver on some sheet -- false positive.
            continue

        filtered.append(v)

    return filtered


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
        v.get("type", "") in target_types and v.get("_sheet_path", "") == "/"
        for v in violations
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
# Phantom wire-dangling violation filtering
# ---------------------------------------------------------------------------


def _build_full_wire_coordinate_set(
    root_schematic: str,
    tolerance: float = 0.1,
) -> set[tuple[float, float]]:
    """Build a set of all known wire-related coordinates across the full hierarchy.

    Unlike :func:`_build_wire_endpoint_map`, this includes the **root sheet**
    and indexes additional coordinate sources (junctions, labels, hierarchical
    labels, global labels) that KiCad's connectivity engine may use as
    sub-segment break-points.

    The returned set can be used to distinguish *real* ``wire_dangling``
    violations (whose positions correspond to actual schematic objects) from
    *phantom* violations (whose positions are internal to KiCad's connectivity
    analysis and do not correspond to any S-expression in the ``.kicad_sch``
    files).

    Args:
        root_schematic: Path to the root ``.kicad_sch`` file.
        tolerance: Coordinate snapping bucket size in mm.

    Returns:
        A set of snapped ``(x, y)`` coordinate tuples.
    """
    from kicad_tools.schema import Schematic
    from kicad_tools.schema.hierarchy import build_hierarchy

    hierarchy = build_hierarchy(root_schematic)
    coords: set[tuple[float, float]] = set()

    def _snap(v: float) -> float:
        return round(v / tolerance) * tolerance

    for node in hierarchy.all_nodes():
        try:
            sch = Schematic.load(node.path)
        except Exception:
            continue

        # Wire endpoints and midpoints
        for wire in sch.wires:
            sx, sy = wire.start[0], wire.start[1]
            ex, ey = wire.end[0], wire.end[1]
            coords.add((_snap(sx), _snap(sy)))
            coords.add((_snap(ex), _snap(ey)))
            mx = (sx + ex) / 2.0
            my = (sy + ey) / 2.0
            coords.add((_snap(mx), _snap(my)))

        # Junctions
        for junc in sch.junctions:
            jx, jy = junc.position[0], junc.position[1]
            coords.add((_snap(jx), _snap(jy)))

        # Local labels
        for lbl in sch.labels:
            lx, ly = lbl.position[0], lbl.position[1]
            coords.add((_snap(lx), _snap(ly)))

        # Global labels
        for gl in sch.global_labels:
            gx, gy = gl.position[0], gl.position[1]
            coords.add((_snap(gx), _snap(gy)))

        # Hierarchical labels
        for hl in sch.hierarchical_labels:
            hx, hy = hl.position[0], hl.position[1]
            coords.add((_snap(hx), _snap(hy)))

    return coords


def filter_phantom_wire_violations(
    violations: list[dict],
    root_schematic: str,
) -> list[dict]:
    """Remove phantom ``wire_dangling`` violations whose position coordinates
    do not correspond to any wire endpoint, midpoint, junction, or label
    in any sheet of the hierarchy.

    KiCad's connectivity engine internally splits wires into sub-segments
    at pin connections, junctions, and other connection points.  It may
    then report ``wire_dangling`` violations at the computed sub-segment
    coordinates -- coordinates that do not appear in any ``.kicad_sch``
    file.  These are false positives that cannot be resolved by editing
    the schematic.

    This function builds a comprehensive coordinate index of all wire
    endpoints/midpoints, junctions, and labels across the full hierarchy
    (including the root sheet) and suppresses any ``wire_dangling``
    violation whose position does not match any indexed coordinate.

    Only ``wire_dangling`` violations are filtered.  Other position-based
    violation types (``endpoint_off_grid``, ``label_dangling``, etc.) are
    passed through unchanged because they have different semantics.

    Filtered violations are logged at debug level for diagnostics.

    Args:
        violations: List of raw violation dicts (must already have
            ``_sheet_path`` injected and re-attribution applied).
        root_schematic: Path to the root ``.kicad_sch`` file.

    Returns:
        A new list with phantom ``wire_dangling`` violations removed.
    """
    import logging

    logger = logging.getLogger(__name__)

    target_type = "wire_dangling"

    # Quick check: skip the expensive hierarchy traversal if there are
    # no wire_dangling violations at all.
    has_target = any(v.get("type", "") == target_type for v in violations)
    if not has_target:
        return violations

    tolerance = 0.1
    known_coords = _build_full_wire_coordinate_set(root_schematic, tolerance=tolerance)

    def _snap(val: float) -> float:
        return round(val / tolerance) * tolerance

    filtered: list[dict] = []
    for v in violations:
        if v.get("type", "") != target_type:
            filtered.append(v)
            continue

        pos = v.get("pos", {})
        x = pos.get("x", None)
        y = pos.get("y", None)

        if x is None or y is None:
            # No position data -- keep to be safe.
            filtered.append(v)
            continue

        key = (_snap(float(x)), _snap(float(y)))
        if key in known_coords:
            # Position matches a real schematic object -- keep it.
            filtered.append(v)
        else:
            # Phantom violation -- coordinates do not correspond to any
            # wire, junction, or label in any sheet.
            logger.debug(
                "Filtered phantom wire_dangling at (%.1f, %.1f) on sheet %s",
                float(x),
                float(y),
                v.get("_sheet_path", "?"),
            )

    return filtered


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
            if sym.uuid:
                mapping[sym.uuid] = sheet_path
            ref = sym.reference
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
        v.get("type", "") in target_types and v.get("_sheet_path", "") == "/"
        for v in violations
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
