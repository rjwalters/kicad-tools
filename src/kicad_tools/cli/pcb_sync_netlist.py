"""Sync PCB footprints from schematic netlist.

Compares schematic components against PCB footprints and:
- Adds missing footprints (placed at board edge)
- Updates net assignments for renamed references
- Reports orphaned footprints (in PCB but not schematic)

Supports --dry-run to preview changes without modifying files.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SyncAction:
    """A single sync action to perform or report."""

    action: str  # "add", "rename", "orphan"
    reference: str
    footprint: str = ""
    value: str = ""
    detail: str = ""
    old_reference: str = ""  # for renames


@dataclass
class SyncResult:
    """Result of a netlist sync operation."""

    added: list[SyncAction] = field(default_factory=list)
    renamed: list[SyncAction] = field(default_factory=list)
    orphaned: list[SyncAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.renamed or self.orphaned)


def _normalize_footprint(fp: str) -> str:
    """Normalize footprint identifier for comparison.

    Strips library prefix for comparison since schematic and PCB may use
    different library path conventions.
    """
    if ":" in fp:
        return fp.split(":", 1)[1]
    return fp


def sync_netlist(
    schematic_path: Path,
    pcb_path: Path,
    dry_run: bool = False,
    output_path: Path | None = None,
) -> SyncResult:
    """Sync PCB footprints from schematic.

    Args:
        schematic_path: Path to root .kicad_sch file.
        pcb_path: Path to .kicad_pcb file.
        dry_run: If True, compute diff without modifying files.
        output_path: If set, write modified PCB here instead of overwriting.

    Returns:
        SyncResult describing all actions taken or planned.
    """
    from kicad_tools.schema.bom import BOMItem, extract_bom
    from kicad_tools.schema.pcb import PCB

    result = SyncResult()

    # --- Extract schematic components (hierarchical) ---
    try:
        bom = extract_bom(str(schematic_path), hierarchical=True)
    except Exception as e:
        result.errors.append(f"Failed to extract BOM from schematic: {e}")
        return result

    # Build schematic component dict: ref -> BOMItem
    # Skip virtual/power symbols but keep DNP (they still need footprints)
    sch_components: dict[str, BOMItem] = {}
    for item in bom.items:
        if item.is_virtual or item.is_power_symbol:
            continue
        if item.reference and not item.reference.startswith("#"):
            sch_components[item.reference] = item

    # --- Load PCB ---
    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        result.errors.append(f"Failed to load PCB: {e}")
        return result

    # Build PCB component dict: ref -> footprint info
    pcb_refs: dict[str, dict[str, str]] = {}
    for fp in pcb.footprints:
        if fp.reference and not fp.reference.startswith("#"):
            pcb_refs[fp.reference] = {
                "footprint": fp.name,
                "value": fp.value,
            }

    sch_ref_set = set(sch_components.keys())
    pcb_ref_set = set(pcb_refs.keys())

    # --- Detect renames via value+footprint matching ---
    missing_refs = sch_ref_set - pcb_ref_set
    extra_refs = pcb_ref_set - sch_ref_set

    # Try to match missing schematic refs to extra PCB refs by value+footprint
    rename_map: dict[str, str] = {}  # pcb_old_ref -> sch_new_ref
    if missing_refs and extra_refs:
        # Build lookup: (normalized_footprint, value) -> list of refs
        missing_by_sig: dict[tuple[str, str], list[str]] = {}
        for ref in missing_refs:
            item = sch_components[ref]
            sig = (_normalize_footprint(item.footprint), item.value)
            missing_by_sig.setdefault(sig, []).append(ref)

        extra_by_sig: dict[tuple[str, str], list[str]] = {}
        for ref in extra_refs:
            info = pcb_refs[ref]
            sig = (_normalize_footprint(info["footprint"]), info["value"])
            extra_by_sig.setdefault(sig, []).append(ref)

        # Match unique pairs: if exactly one missing and one extra share the same
        # (footprint, value) signature, treat it as a rename
        for sig, sch_refs_list in missing_by_sig.items():
            if sig in extra_by_sig:
                pcb_refs_list = extra_by_sig[sig]
                if len(sch_refs_list) == 1 and len(pcb_refs_list) == 1:
                    old_ref = pcb_refs_list[0]
                    new_ref = sch_refs_list[0]
                    rename_map[old_ref] = new_ref

    # Remove matched renames from missing/extra sets
    renamed_old_refs = set(rename_map.keys())
    renamed_new_refs = set(rename_map.values())
    truly_missing = missing_refs - renamed_new_refs
    truly_orphaned = extra_refs - renamed_old_refs

    # --- Build actions ---

    # Renames
    for old_ref, new_ref in sorted(rename_map.items()):
        item = sch_components[new_ref]
        result.renamed.append(SyncAction(
            action="rename",
            reference=new_ref,
            old_reference=old_ref,
            footprint=item.footprint,
            value=item.value,
            detail=f"{old_ref} -> {new_ref}",
        ))

    # Missing components to add
    for ref in sorted(truly_missing):
        item = sch_components[ref]
        if not item.footprint:
            result.errors.append(
                f"Component {ref} has no footprint assigned in schematic"
            )
            continue
        result.added.append(SyncAction(
            action="add",
            reference=ref,
            footprint=item.footprint,
            value=item.value,
            detail=f"Add {ref} ({item.value}, {item.footprint})",
        ))

    # Orphaned footprints
    for ref in sorted(truly_orphaned):
        info = pcb_refs[ref]
        result.orphaned.append(SyncAction(
            action="orphan",
            reference=ref,
            footprint=info["footprint"],
            value=info["value"],
            detail=f"Orphan: {ref} ({info['value']}, {info['footprint']})",
        ))

    # --- Apply changes if not dry-run ---
    if not dry_run and result.has_changes:
        # Apply renames first
        for action in result.renamed:
            _apply_rename(pcb, action.old_reference, action.reference)

        # Add missing footprints at board edge
        placement_x, placement_y = _get_board_edge_position(pcb)
        x_offset = 0.0
        for action in result.added:
            try:
                pcb.add_footprint(
                    library_id=action.footprint,
                    reference=action.reference,
                    x=placement_x + x_offset,
                    y=placement_y,
                    value=action.value,
                )
                x_offset += 5.0  # Space footprints horizontally
            except Exception as e:
                result.errors.append(
                    f"Failed to add footprint for {action.reference}: {e}"
                )

        # Update net assignments from schematic netlist (covers renamed refs
        # and newly added footprints)
        if result.renamed or result.added:
            _assign_nets_from_schematic(pcb, schematic_path)

        # Save
        save_path = output_path or pcb_path
        try:
            pcb.save(save_path)
        except Exception as e:
            result.errors.append(f"Failed to save PCB: {e}")

    return result


def _assign_nets_from_schematic(pcb, schematic_path: Path) -> None:
    """Export netlist from schematic and assign nets to PCB pads.

    Updates pad-to-net mappings for all footprints, including renamed and
    newly added ones. Failures are silently ignored since net assignment is
    best-effort; the user can run KiCad's Update PCB from Schematic for a
    full refresh.
    """
    try:
        from kicad_tools.operations.netlist import export_netlist

        netlist = export_netlist(str(schematic_path))
        for net in netlist.nets:
            if net.name:
                pcb.add_net(net.name)
        pcb.assign_nets_from_netlist(netlist)
    except Exception:
        pass


def _apply_rename(pcb, old_ref: str, new_ref: str) -> bool:
    """Rename a footprint reference in the PCB.

    Delegates to the same logic used by ``pcb reannotate``.
    """
    from kicad_tools.cli.commands.pcb import _update_footprint_reference

    return _update_footprint_reference(pcb, old_ref, new_ref)


def _get_board_edge_position(pcb) -> tuple[float, float]:
    """Determine a position just outside the board edge for staging footprints.

    Places new footprints 10mm to the right of the board outline.
    Falls back to (0, 0) if no outline is detected.
    """
    outline = pcb.get_board_outline()
    if outline:
        max_x = max(pt[0] for pt in outline)
        min_y = min(pt[1] for pt in outline)
        # Place 10mm to the right of the board, at the top edge
        # Subtract board origin since add_footprint uses board-relative coords
        origin_x, origin_y = pcb.board_origin
        return (max_x - origin_x + 10.0, min_y - origin_y)
    return (0.0, 0.0)


def format_text(result: SyncResult, dry_run: bool, pcb_path: Path) -> str:
    """Format sync result as human-readable text."""
    lines: list[str] = []
    label = "PCB Sync Netlist (dry run)" if dry_run else "PCB Sync Netlist"
    lines.append(label)
    lines.append(f"  PCB: {pcb_path}")
    lines.append("")

    if not result.has_changes and not result.errors:
        lines.append("  No changes needed - PCB is in sync with schematic.")
        return "\n".join(lines)

    if result.renamed:
        lines.append(f"  Renames ({len(result.renamed)}):")
        for action in result.renamed:
            lines.append(f"    {action.old_reference} -> {action.reference}")
        lines.append("")

    if result.added:
        lines.append(f"  Missing footprints to add ({len(result.added)}):")
        for action in result.added:
            lines.append(f"    {action.reference}: {action.value} ({action.footprint})")
        lines.append("")

    if result.orphaned:
        lines.append(f"  Orphaned footprints ({len(result.orphaned)}):")
        for action in result.orphaned:
            lines.append(f"    {action.reference}: {action.value} ({action.footprint})")
        lines.append("")

    if result.errors:
        lines.append(f"  Errors ({len(result.errors)}):")
        for err in result.errors:
            lines.append(f"    {err}")
        lines.append("")

    return "\n".join(lines)


def format_json(result: SyncResult, dry_run: bool, pcb_path: Path) -> str:
    """Format sync result as JSON."""
    output = {
        "pcb": str(pcb_path),
        "dry_run": dry_run,
        "renamed": [
            {
                "old_reference": a.old_reference,
                "new_reference": a.reference,
                "footprint": a.footprint,
                "value": a.value,
            }
            for a in result.renamed
        ],
        "added": [
            {
                "reference": a.reference,
                "footprint": a.footprint,
                "value": a.value,
            }
            for a in result.added
        ],
        "orphaned": [
            {
                "reference": a.reference,
                "footprint": a.footprint,
                "value": a.value,
            }
            for a in result.orphaned
        ],
        "errors": result.errors,
    }
    return json.dumps(output, indent=2)


def run_sync_netlist(
    schematic_path: Path,
    pcb_path: Path,
    dry_run: bool = False,
    output_path: Path | None = None,
    output_format: str = "text",
) -> int:
    """Run the sync-netlist command.

    Args:
        schematic_path: Path to root .kicad_sch file.
        pcb_path: Path to .kicad_pcb file.
        dry_run: Preview changes without modifying.
        output_path: Alternative output path for modified PCB.
        output_format: "text" or "json".

    Returns:
        Exit code (0 for success, 1 for errors).
    """
    result = sync_netlist(
        schematic_path=schematic_path,
        pcb_path=pcb_path,
        dry_run=dry_run,
        output_path=output_path,
    )

    if output_format == "json":
        print(format_json(result, dry_run, pcb_path))
    else:
        print(format_text(result, dry_run, pcb_path))

    # Return non-zero only on errors, not on orphaned footprints
    return 1 if result.errors else 0
