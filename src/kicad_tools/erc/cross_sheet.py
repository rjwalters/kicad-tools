"""Cross-sheet ERC helpers.

Provides two cross-sheet checks that complement KiCad's built-in ERC:

1. **Duplicate reference detection** -- detects duplicate reference
   designators that span different hierarchical sheets.  KiCad's built-in
   ERC only reliably detects duplicates within a single flat schematic;
   this module fills the gap for hierarchical designs.

2. **False-positive global label filtering** -- KiCad reports
   ``single_global_label`` / ``isolated_pin_label`` on a per-sheet basis
   without aggregating across the full hierarchy.  A global label that
   appears in multiple sheets is *not* truly isolated; this module
   suppresses those false positives.
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
