"""Sync PCB footprints from schematic netlist.

Compares schematic components against PCB footprints and:
- Adds missing footprints (placed at board edge)
- Updates net assignments for renamed references
- Reports orphaned footprints (in PCB but not schematic)

Supports --dry-run to preview changes without modifying files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SyncAction:
    """A single sync action to perform or report."""

    action: str  # "add", "rename", "orphan", "remove"
    reference: str
    footprint: str = ""
    value: str = ""
    detail: str = ""
    old_reference: str = ""  # for renames


@dataclass
class PinMismatch:
    """A pin-count vs pad-count mismatch for a matched component."""

    reference: str
    schematic_footprint: str
    pcb_footprint: str
    schematic_pins: int
    pcb_pads: int
    severity: str = "warning"  # "warning" or "info"

    @property
    def delta(self) -> int:
        """Difference: pcb_pads - schematic_pins."""
        return self.pcb_pads - self.schematic_pins


@dataclass
class SyncResult:
    """Result of a netlist sync operation."""

    added: list[SyncAction] = field(default_factory=list)
    renamed: list[SyncAction] = field(default_factory=list)
    orphaned: list[SyncAction] = field(default_factory=list)
    removed: list[SyncAction] = field(default_factory=list)
    pin_mismatches: list[PinMismatch] = field(default_factory=list)
    net_updated: list[SyncAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.added or self.renamed or self.orphaned
            or self.removed or self.pin_mismatches
            or self.net_updated
        )


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
    remove_orphans: bool = False,
    force: bool = False,
    auto_rename: bool = False,
    remove_orphan_nets: bool = False,
) -> SyncResult:
    """Sync PCB footprints from schematic.

    Args:
        schematic_path: Path to root .kicad_sch file.
        pcb_path: Path to .kicad_pcb file.
        dry_run: If True, compute diff without modifying files.
        output_path: If set, write modified PCB here instead of overwriting.
        remove_orphans: If True, delete orphaned footprints from the PCB.
        force: If True, remove orphans even if they have routed traces.
        auto_rename: If True, apply renames without interactive confirmation.
            When False (default) and not dry_run, the caller is responsible
            for confirming renames before applying.  The ``run_sync_netlist``
            wrapper handles the interactive prompt.
        remove_orphan_nets: If True, remove nets with no pad references after
            net assignment.

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
    # Skip power symbols and components not placed on the board.
    # Components with in_bom=False but on_board=True (e.g., net ties)
    # are included because they have physical footprints on the PCB.
    sch_components: dict[str, BOMItem] = {}
    for item in bom.items:
        if item.is_power_symbol:
            continue
        if not item.on_board:
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

    # --- Detect pin-count vs pad-count mismatches for matched components ---
    common_refs = sch_ref_set & pcb_ref_set
    if common_refs:
        from kicad_tools.pcb.footprints import get_pad_count

        for ref in sorted(common_refs):
            item = sch_components[ref]
            pcb_info = pcb_refs[ref]

            # Skip when schematic and PCB use the same footprint -- identical
            # footprints inherently have the same pad count, and comparing
            # get_pad_count (which uses name-based heuristics) against the
            # real pad list would produce false positives for names like
            # R_0402 where the heuristic misinterprets the package size.
            sch_fp_norm = _normalize_footprint(item.footprint)
            pcb_fp_norm = _normalize_footprint(pcb_info["footprint"])
            if sch_fp_norm == pcb_fp_norm:
                continue

            # Get expected pad count from schematic footprint assignment
            sch_pad_count = get_pad_count(item.footprint)
            if sch_pad_count is None:
                continue

            # Get actual pad count from the PCB footprint
            pcb_fp = None
            for fp in pcb.footprints:
                if fp.reference == ref:
                    pcb_fp = fp
                    break
            if pcb_fp is None:
                continue

            actual_pad_count = len(pcb_fp.pads)
            if sch_pad_count != actual_pad_count:
                # Thermal/exposed pad tolerance: +1 pad surplus is info, not warning
                if actual_pad_count - sch_pad_count == 1:
                    severity = "info"
                else:
                    severity = "warning"
                result.pin_mismatches.append(PinMismatch(
                    reference=ref,
                    schematic_footprint=item.footprint,
                    pcb_footprint=pcb_info["footprint"],
                    schematic_pins=sch_pad_count,
                    pcb_pads=actual_pad_count,
                    severity=severity,
                ))

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
        # (footprint, value) signature, treat it as a rename.
        # Ambiguous matches (N:M with same signature) are skipped with a warning.
        for sig, sch_refs_list in missing_by_sig.items():
            if sig in extra_by_sig:
                pcb_refs_list = extra_by_sig[sig]
                if len(sch_refs_list) == 1 and len(pcb_refs_list) == 1:
                    old_ref = pcb_refs_list[0]
                    new_ref = sch_refs_list[0]
                    rename_map[old_ref] = new_ref
                else:
                    fp_name, val = sig
                    result.warnings.append(
                        f"Ambiguous match for ({fp_name}, {val}): "
                        f"schematic refs {sorted(sch_refs_list)} vs "
                        f"PCB refs {sorted(pcb_refs_list)} - skipped"
                    )

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

    # Orphaned footprints -- categorize as remove or report-only
    for ref in sorted(truly_orphaned):
        info = pcb_refs[ref]
        if remove_orphans:
            # Check for connected traces (safety check)
            has_traces = pcb.footprint_has_traces(ref)
            if has_traces and not force:
                result.errors.append(
                    f"Orphan {ref} has routed traces; use --force to remove"
                )
                result.orphaned.append(SyncAction(
                    action="orphan",
                    reference=ref,
                    footprint=info["footprint"],
                    value=info["value"],
                    detail=f"Orphan: {ref} ({info['value']}, {info['footprint']}) - has traces",
                ))
            else:
                result.removed.append(SyncAction(
                    action="remove",
                    reference=ref,
                    footprint=info["footprint"],
                    value=info["value"],
                    detail=f"Remove: {ref} ({info['value']}, {info['footprint']})",
                ))
        else:
            result.orphaned.append(SyncAction(
                action="orphan",
                reference=ref,
                footprint=info["footprint"],
                value=info["value"],
                detail=f"Orphan: {ref} ({info['value']}, {info['footprint']})",
            ))

    # --- Apply changes if not dry-run ---
    # When auto_rename is False and there are renames, the caller must confirm
    # before calling with auto_rename=True.  dry_run always skips application.
    skip_apply = dry_run or (result.renamed and not auto_rename)
    if not skip_apply:
        # Apply footprint-level changes (renames, adds, removes)
        if result.has_changes:
            # Apply renames using collision-safe rename plan
            if result.renamed:
                _apply_renames_safe(pcb, rename_map, pcb_ref_set, result)

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

            # Remove orphaned footprints
            for action in result.removed:
                if not pcb.remove_footprint(action.reference):
                    result.errors.append(
                        f"Failed to remove footprint {action.reference}"
                    )

        # Update net assignments from schematic netlist unconditionally.
        # This catches stale pad-net assignments even when no footprints are
        # added or renamed (e.g. when pad nets drifted from the schematic).
        net_actions, net_errors = _assign_nets_from_schematic(
            pcb, schematic_path
        )
        result.net_updated.extend(net_actions)
        result.errors.extend(net_errors)

        # Optionally remove nets that have no pad references
        if remove_orphan_nets:
            _remove_unused_nets(pcb, result)

        # Save if anything changed (footprints or nets)
        if result.has_changes:
            save_path = output_path or pcb_path
            try:
                pcb.save(save_path)
            except Exception as e:
                result.errors.append(f"Failed to save PCB: {e}")

    elif dry_run:
        # Compute net diff for dry-run reporting (no PCB changes applied).
        # Note: this mutates the in-memory PCB to compute the diff but
        # never saves, so the file on disk is unchanged.
        net_actions, net_errors = _assign_nets_from_schematic(
            pcb, schematic_path
        )
        result.net_updated.extend(net_actions)
        result.errors.extend(net_errors)

    return result


def _get_pad_net_snapshot(pcb) -> dict[tuple[str, str], str]:
    """Snapshot current pad-to-net assignments from all footprints.

    Returns a dict mapping ``(reference, pad_number)`` to ``net_name``.
    """
    snapshot: dict[tuple[str, str], str] = {}
    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue
        for pad in fp.pads:
            snapshot[(fp.reference, pad.number)] = pad.net_name
    return snapshot


def _normalize_kicad_cli_net_names(netlist) -> None:
    """Strip leading ``/`` from hierarchical-label net names in-place.

    The ``kicad-cli sch export netlist`` exporter prefixes hierarchical
    local labels with the sheet path (e.g. ``/BST_A``, ``/DRV_CP1``)
    while global power-symbol labels come through unprefixed
    (e.g. ``+3V3``, ``GND``).  PCB net tables conventionally store bare
    names, so adding the prefixed form creates ghost nets that get
    dropped by orphan-net cleanup and prevents pads from being assigned
    to the intended net.

    This normalization mirrors what ``_extract_schematic_nets`` in
    :mod:`kicad_tools.cli.fleet_cmd` does -- it consumes label-leaf
    names that are already bare, so the drift detector and the sync
    writer agree on the same naming convention.

    Only a single leading ``/`` is stripped, and only from names that
    do not begin with another ``/`` after the strip (defensive against
    deeper hierarchical paths such as ``/sheetA/SIGNAL`` -- those keep
    a leading ``/`` only if the parser was confused; we still strip
    one and leave the rest as a sub-path token, which then becomes a
    valid bare net name).

    Power-rail aliases (``+3V3``, ``GND``, ``+5V``, ...) are left
    unchanged because they don't carry the ``/`` prefix in the
    exported netlist.

    Args:
        netlist: A :class:`Netlist` whose ``nets`` list will be
            mutated in place.
    """
    for net in netlist.nets:
        if net.name and net.name.startswith("/"):
            net.name = net.name[1:]


def _assign_nets_from_schematic(
    pcb, schematic_path: Path
) -> tuple[list[SyncAction], list[str]]:
    """Export netlist from schematic and assign nets to PCB pads.

    Always applies net assignments to the in-memory *pcb* object.  The
    caller is responsible for deciding whether to persist (save) the
    result.

    Returns:
        A tuple of ``(net_actions, errors)`` where *net_actions* is a list
        of :class:`SyncAction` items with ``action="net_updated"`` and
        *errors* is a list of error strings.
    """
    actions: list[SyncAction] = []
    errors: list[str] = []

    try:
        from kicad_tools.operations.netlist import (
            build_pin_to_pad_map,
            export_netlist,
        )

        netlist = export_netlist(str(schematic_path))
    except Exception as exc:
        errors.append(f"Failed to export netlist for net assignment: {exc}")
        return actions, errors

    # Normalize ``/``-prefixed hierarchical net names from kicad-cli.
    # The Python fallback already emits bare names, so this is a no-op
    # for that path; running it unconditionally keeps the downstream
    # invariant simple (``net.name`` is always the bare PCB-side name).
    _normalize_kicad_cli_net_names(netlist)

    # Build pin-to-pad mapping for all netlist sources.
    #
    # Both the Python fallback and the kicad-cli exporter emit
    # ``NetNode.pin`` values that are *schematic* symbol pin numbers.
    # These may differ from footprint pad numbers when a symbol uses
    # name-based pads (BGA-style ``A1``/``B2``) or when the symbol's
    # pin numbering follows a different package family than the
    # footprint's pad numbering.  ``build_pin_to_pad_map`` resolves the
    # translation by walking ``lib_symbols`` and the PCB footprint pad
    # lists; the map is identity for the common case (pin number ==
    # pad number) and only diverges for genuine mismatches, so building
    # it unconditionally is safe.
    pin_to_pad_map = None
    try:
        pin_to_pad_map = build_pin_to_pad_map(schematic_path, pcb)
    except Exception as exc:
        errors.append(
            f"Failed to build pin-to-pad map (proceeding without): {exc}"
        )

    # Snapshot before assignment
    before = _get_pad_net_snapshot(pcb)

    # Ensure all schematic nets exist in the PCB
    for net in netlist.nets:
        if net.name:
            pcb.add_net(net.name)

    # Apply net assignments with pin-to-pad resolution
    try:
        pcb.assign_nets_from_netlist(netlist, pin_to_pad_map=pin_to_pad_map)
    except Exception as exc:
        errors.append(f"Failed to assign nets from netlist: {exc}")
        return actions, errors

    # Snapshot after assignment
    after = _get_pad_net_snapshot(pcb)

    # Compute diff
    for key in sorted(set(before.keys()) | set(after.keys())):
        old_net = before.get(key, "")
        new_net = after.get(key, "")
        if old_net != new_net:
            ref, pad_num = key
            actions.append(SyncAction(
                action="net_updated",
                reference=ref,
                detail=f"{ref}.{pad_num}: \"{old_net}\" -> \"{new_net}\"",
            ))

    return actions, errors


def _remove_unused_nets(pcb, result: SyncResult) -> None:
    """Remove nets that have zero pad references from the PCB.

    Skips the unconnected net (net 0 / empty name).  Removed net names
    are appended as warnings on *result* for visibility.
    """
    # Collect nets referenced by at least one pad
    referenced_nets: set[str] = set()
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_name:
                referenced_nets.add(pad.net_name)

    # Also count nets used by segments, vias, and zones
    for seg in pcb.segments:
        if seg.net_name:
            referenced_nets.add(seg.net_name)
    for via in pcb.vias:
        if via.net_name:
            referenced_nets.add(via.net_name)
    for zone in pcb.zones:
        if zone.net_name:
            referenced_nets.add(zone.net_name)

    # Find unreferenced nets
    orphan_net_names: list[str] = []
    for net in pcb.nets.values():
        if not net.name:
            continue  # skip unconnected net
        if net.name not in referenced_nets:
            orphan_net_names.append(net.name)

    if not orphan_net_names:
        return

    # Remove from S-expression tree and internal dict
    removed: list[str] = []
    for name in sorted(orphan_net_names):
        net = pcb.get_net_by_name(name)
        if net is None:
            continue
        # Remove from _nets dict
        if net.number in pcb.nets:
            del pcb.nets[net.number]
        # Remove from S-expression
        for net_sexp in pcb._sexp.find_all("net"):
            sexp_name = net_sexp.get_string(1)
            if sexp_name is None:
                sexp_name = net_sexp.get_string(0)
            if sexp_name == name:
                pcb._sexp.remove(net_sexp)
                break
        removed.append(name)

    if removed:
        result.warnings.append(
            f"Removed {len(removed)} orphan net(s): {', '.join(removed)}"
        )


def _apply_rename(pcb, old_ref: str, new_ref: str) -> bool:
    """Rename a footprint reference in the PCB.

    Delegates to the same logic used by ``pcb reannotate``.
    """
    from kicad_tools.cli.commands.pcb import _update_footprint_reference

    return _update_footprint_reference(pcb, old_ref, new_ref)


def _apply_renames_safe(
    pcb, rename_map: dict[str, str], pcb_ref_set: set[str], result: SyncResult
) -> None:
    """Apply renames using collision-safe rename plan.

    Uses ``_build_rename_plan`` from the reannotate command to resolve
    collision chains (e.g. U3->U8 and U10->U3) via temporary intermediate
    references.
    """
    from kicad_tools.cli.commands.pcb import _build_rename_plan

    steps, plan_warnings, plan_errors = _build_rename_plan(rename_map, pcb_ref_set)
    result.warnings.extend(plan_warnings)

    if plan_errors:
        result.errors.extend(plan_errors)
        return

    for from_ref, to_ref, _via_temp in steps:
        _apply_rename(pcb, from_ref, to_ref)


def _get_board_edge_position(pcb) -> tuple[float, float]:
    """Determine a position just outside the board edge for staging footprints.

    Places new footprints 10mm to the right of the board outline.
    Falls back to (0, 0) if no outline is detected.
    """
    outline = pcb.get_board_outline()
    if outline:
        max_x = max(pt[0] for pt in outline)
        min_y = min(pt[1] for pt in outline)
        # Place 10mm to the right of the board, at the top edge.
        # get_board_outline() already returns board-relative coords.
        return (max_x + 10.0, min_y)
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

    if result.removed:
        lines.append(f"  Removed footprints ({len(result.removed)}):")
        for action in result.removed:
            lines.append(f"    {action.reference}: {action.value} ({action.footprint})")
        lines.append("")

    if result.net_updated:
        lines.append(f"  Net updates ({len(result.net_updated)}):")
        for action in result.net_updated:
            lines.append(f"    {action.detail}")
        lines.append("")

    if result.orphaned:
        lines.append(f"  Orphaned footprints ({len(result.orphaned)}):")
        for action in result.orphaned:
            lines.append(f"    {action.reference}: {action.value} ({action.footprint})")
        lines.append("")

    if result.pin_mismatches:
        lines.append(f"  Pin-count mismatches ({len(result.pin_mismatches)}):")
        for pm in result.pin_mismatches:
            level = "info" if pm.severity == "info" else "warning"
            lines.append(
                f"    [{level}] {pm.reference}: schematic expects {pm.schematic_pins} pins "
                f"({pm.schematic_footprint}), PCB has {pm.pcb_pads} pads "
                f"({pm.pcb_footprint})"
            )
        lines.append("")

    if result.warnings:
        lines.append(f"  Warnings ({len(result.warnings)}):")
        for warn in result.warnings:
            lines.append(f"    {warn}")
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
        "removed": [
            {
                "reference": a.reference,
                "footprint": a.footprint,
                "value": a.value,
            }
            for a in result.removed
        ],
        "pin_mismatches": [
            {
                "reference": pm.reference,
                "schematic_footprint": pm.schematic_footprint,
                "pcb_footprint": pm.pcb_footprint,
                "schematic_pins": pm.schematic_pins,
                "pcb_pads": pm.pcb_pads,
                "severity": pm.severity,
            }
            for pm in result.pin_mismatches
        ],
        "net_updated": [
            {
                "reference": a.reference,
                "detail": a.detail,
            }
            for a in result.net_updated
        ],
        "warnings": result.warnings,
        "errors": result.errors,
    }
    return json.dumps(output, indent=2)


def run_sync_netlist(
    schematic_path: Path,
    pcb_path: Path,
    dry_run: bool = False,
    output_path: Path | None = None,
    output_format: str = "text",
    remove_orphans: bool = False,
    force: bool = False,
    auto_rename: bool = False,
    remove_orphan_nets: bool = False,
) -> int:
    """Run the sync-netlist command.

    Args:
        schematic_path: Path to root .kicad_sch file.
        pcb_path: Path to .kicad_pcb file.
        dry_run: Preview changes without modifying.
        output_path: Alternative output path for modified PCB.
        output_format: "text" or "json".
        remove_orphans: If True, delete orphaned footprints.
        force: If True, remove orphans even with routed traces.
        auto_rename: If True, apply renames without interactive confirmation.
            When False (default) and not dry_run, shows a confirmation prompt
            before applying renames.
        remove_orphan_nets: If True, remove nets with no pad references after
            net assignment.

    Returns:
        Exit code (0 for success, 1 for errors).
    """
    # First pass: compute diff (renames not applied yet unless auto_rename)
    result = sync_netlist(
        schematic_path=schematic_path,
        pcb_path=pcb_path,
        dry_run=dry_run,
        output_path=output_path,
        remove_orphans=remove_orphans,
        force=force,
        auto_rename=auto_rename,
        remove_orphan_nets=remove_orphan_nets,
    )

    if output_format == "json":
        print(format_json(result, dry_run, pcb_path))
    else:
        print(format_text(result, dry_run, pcb_path))

    # Interactive confirmation for renames when not dry_run and not auto_rename
    if (
        not dry_run
        and not auto_rename
        and result.renamed
        and not result.errors
    ):
        try:
            answer = input("Apply renames? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() in ("y", "yes"):
            # Re-run with auto_rename to actually apply
            result = sync_netlist(
                schematic_path=schematic_path,
                pcb_path=pcb_path,
                dry_run=False,
                output_path=output_path,
                remove_orphans=remove_orphans,
                force=force,
                auto_rename=True,
                remove_orphan_nets=remove_orphan_nets,
            )
            print("Renames applied.")
        else:
            print("Renames skipped.")

    # Return non-zero only on errors, not on orphaned footprints
    return 1 if result.errors else 0
