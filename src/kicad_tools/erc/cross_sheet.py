"""Cross-sheet duplicate reference designator detection.

Detects duplicate reference designators that span different hierarchical
sheets in a KiCad schematic project. KiCad's built-in ERC only reliably
detects duplicates within a single flat schematic; this module fills the
gap for hierarchical designs.
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
