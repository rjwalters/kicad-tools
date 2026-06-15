"""PCB (pcb) subcommand handlers."""

import json
import sys
from pathlib import Path

__all__ = ["run_pcb_command"]


def run_pcb_command(args) -> int:
    """Handle PCB subcommands."""
    if not args.pcb_command:
        print("Usage: kicad-tools pcb <command> [options] <file>")
        print(
            "Commands: summary, footprints, nets, traces, stackup, zones, strip, reannotate, sync-netlist, remove-footprint, move-footprint, page-fit, lock-footprints, unlock-footprints, add-zone, snap-rotation, edit-outline, net-audit, export-dsn, import-ses"
        )
        return 1

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Handle zones command
    if args.pcb_command == "zones":
        return _run_zones_command(args, pcb_path)

    # Handle strip command separately (doesn't use pcb_query)
    if args.pcb_command == "strip":
        return _run_strip_command(args, pcb_path)

    # Handle reannotate command separately (doesn't use pcb_query)
    if args.pcb_command == "reannotate":
        return _run_reannotate_command(args, pcb_path)

    # Handle sync-netlist command
    if args.pcb_command == "sync-netlist":
        return _run_sync_netlist_command(args, pcb_path)

    # Handle remove-footprint command
    if args.pcb_command == "remove-footprint":
        return _run_remove_footprint_command(args, pcb_path)

    # Handle move-footprint command
    if args.pcb_command == "move-footprint":
        return _run_move_footprint_command(args, pcb_path)

    # Handle page-fit command
    if args.pcb_command == "page-fit":
        return _run_page_fit_command(args, pcb_path)

    # Handle lock-footprints / unlock-footprints commands
    if args.pcb_command in ("lock-footprints", "unlock-footprints"):
        return _run_lock_footprints_command(args, pcb_path)

    # Handle add-zone command
    if args.pcb_command == "add-zone":
        return _run_add_zone_command(args, pcb_path)

    # Handle snap-rotation command
    if args.pcb_command == "snap-rotation":
        return _run_snap_rotation_command(args, pcb_path)

    # Handle edit-outline command
    if args.pcb_command == "edit-outline":
        return _run_edit_outline_command(args, pcb_path)

    # Handle net-audit command
    if args.pcb_command == "net-audit":
        return _run_net_audit_command(args, pcb_path)

    # Handle export-dsn command
    if args.pcb_command == "export-dsn":
        return _run_export_dsn_command(args, pcb_path)

    # Handle import-ses command
    if args.pcb_command == "import-ses":
        return _run_import_ses_command(args, pcb_path)

    from ..pcb_query import main as pcb_main

    if args.pcb_command == "summary":
        sub_argv = [str(pcb_path), "summary"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "footprints":
        sub_argv = [str(pcb_path), "footprints"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        if args.sorted:
            sub_argv.append("--sorted")
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "nets":
        sub_argv = [str(pcb_path), "nets"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        if args.sorted:
            sub_argv.append("--sorted")
        if getattr(args, "check_connectivity", False):
            sub_argv.append("--check-connectivity")
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "traces":
        sub_argv = [str(pcb_path), "traces"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.layer:
            sub_argv.extend(["--layer", args.layer])
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "stackup":
        sub_argv = [str(pcb_path), "stackup"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return pcb_main(sub_argv) or 0

    return 1


def _run_zones_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb zones' command."""
    from kicad_tools.schema.pcb import PCB

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    zones = pcb.zones
    output_format = getattr(args, "format", "text")

    if output_format == "json":
        zone_data = []
        for zone in zones:
            # Compute bounding box from polygon points
            bounding_box = None
            if zone.polygon:
                xs = [p[0] for p in zone.polygon]
                ys = [p[1] for p in zone.polygon]
                bounding_box = {
                    "min_x": min(xs),
                    "min_y": min(ys),
                    "max_x": max(xs),
                    "max_y": max(ys),
                }
            zone_data.append(
                {
                    "net_number": zone.net_number,
                    "net_name": zone.net_name,
                    "layer": zone.layer,
                    "priority": zone.priority,
                    "clearance": zone.clearance,
                    "thermal_gap": zone.thermal_gap,
                    "thermal_bridge_width": zone.thermal_bridge_width,
                    "is_filled": zone.is_filled,
                    "fill_type": zone.fill_type,
                    "boundary_points": len(zone.polygon),
                    "bounding_box": bounding_box,
                }
            )
        print(json.dumps({"zones": zone_data, "count": len(zones)}, indent=2))
    else:
        if not zones:
            print("No zones found in PCB.")
            return 0

        print(f"Found {len(zones)} zone(s):\n")
        for i, zone in enumerate(zones, 1):
            print(f"Zone {i}:")
            print(f"  Net:       {zone.net_name or f'(net {zone.net_number})'}")
            print(f"  Layer:     {zone.layer}")
            print(f"  Priority:  {zone.priority}")
            print(f"  Clearance: {zone.clearance}mm")
            print(f"  Fill type: {zone.fill_type}")
            print(f"  Filled:    {'Yes' if zone.is_filled else 'No'}")
            print(f"  Boundary:  {len(zone.polygon)} points")
            if zone.polygon:
                xs = [p[0] for p in zone.polygon]
                ys = [p[1] for p in zone.polygon]
                print(
                    f"  Bounds:    ({min(xs):.2f}, {min(ys):.2f}) to ({max(xs):.2f}, {max(ys):.2f})"
                )
            print()

    return 0


def _run_strip_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb strip' command."""
    import re as _re

    from kicad_tools.schema.pcb import PCB

    # Parse net names if provided
    nets = None
    if args.nets:
        nets = [n.strip() for n in args.nets.split(",")]

    # Parse layer names if provided
    layers = None
    if getattr(args, "layers", None):
        layers = [l.strip() for l in args.layers.split(",")]

    # Power net options — exclude power nets by default when --layers is used
    include_power = getattr(args, "include_power", False)
    exclude_power = not include_power if layers else False
    power_pattern = None
    if getattr(args, "power_pattern", None):
        power_pattern = _re.compile(args.power_pattern, _re.IGNORECASE)

    remove_orphan_vias = getattr(args, "remove_orphan_vias", False)

    # Load PCB
    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Get initial counts for reporting
    initial_segments = len(pcb.segments)
    initial_vias = len(pcb.vias)
    initial_zones = len(pcb.zones)

    # Perform strip operation
    keep_zones = getattr(args, "keep_zones", True)
    stats = pcb.strip_traces(
        nets=nets,
        layers=layers,
        keep_zones=keep_zones,
        exclude_power=exclude_power,
        power_pattern=power_pattern,
        remove_orphan_vias=remove_orphan_vias,
    )

    # Determine output path
    output_path = pcb_path
    if args.output:
        output_path = Path(args.output)
    elif not args.dry_run:
        # If no output specified and not dry-run, add -stripped suffix
        output_path = pcb_path.with_stem(f"{pcb_path.stem}-stripped")

    # Format output
    output_format = getattr(args, "format", "text")
    dry_run = getattr(args, "dry_run", False)

    result = {
        "input": str(pcb_path),
        "output": str(output_path) if not dry_run else None,
        "dry_run": dry_run,
        "nets_filtered": nets,
        "layers_filtered": layers,
        "keep_zones": keep_zones,
        "exclude_power": exclude_power,
        "remove_orphan_vias": remove_orphan_vias,
        "before": {
            "segments": initial_segments,
            "vias": initial_vias,
            "zones": initial_zones,
        },
        "removed": stats,
        "after": {
            "segments": initial_segments - stats["segments"],
            "vias": initial_vias - stats["vias"],
            "zones": initial_zones - stats["zones"],
        },
    }

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        # Text format
        print(f"PCB Strip {'(dry run)' if dry_run else ''}")
        print(f"  Input:  {pcb_path}")
        if not dry_run:
            print(f"  Output: {output_path}")
        print()

        if nets:
            print(f"  Filtering nets: {', '.join(nets)}")
        else:
            print("  Stripping all nets")
        if layers:
            print(f"  Filtering layers: {', '.join(layers)}")
        print(f"  Exclude power nets: {exclude_power}")
        print(f"  Keep zones: {keep_zones}")
        if remove_orphan_vias:
            print("  Remove orphan vias: yes")
        print()

        print("  Removed:")
        print(f"    Segments: {stats['segments']:,}")
        print(f"    Vias:     {stats['vias']:,}")
        if not keep_zones:
            print(f"    Zones:    {stats['zones']:,}")
        print()

        print("  Remaining:")
        print(f"    Segments: {result['after']['segments']:,}")
        print(f"    Vias:     {result['after']['vias']:,}")
        print(f"    Zones:    {result['after']['zones']:,}")

    # Save unless dry-run
    if not dry_run:
        try:
            pcb.save(output_path)
            if output_format == "text":
                print()
                print(f"  Saved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB: {e}", file=sys.stderr)
            return 1

    return 0


def _update_footprint_reference(pcb, old_ref: str, new_ref: str) -> bool:
    """Update a footprint's reference designator in both parsed data and S-expression tree.

    Handles both KiCad 7 (fp_text) and KiCad 8+ (property) formats.

    Args:
        pcb: PCB instance.
        old_ref: Current reference designator.
        new_ref: New reference designator.

    Returns:
        True if the reference was found and updated.
    """
    # Find the footprint S-expression node
    fp_sexp = None
    for candidate in pcb._sexp.find_all("footprint"):
        ref = pcb._get_footprint_reference(candidate)
        if ref == old_ref:
            fp_sexp = candidate
            break

    if fp_sexp is None:
        return False

    # Update S-expression: KiCad 7 fp_text format
    for fp_text in fp_sexp.find_all("fp_text"):
        if fp_text.get_string(0) == "reference":
            fp_text.set_atom(1, new_ref)
            break

    # Update S-expression: KiCad 8+ property format
    for prop in fp_sexp.find_all("property"):
        if prop.get_string(0) == "Reference":
            prop.set_atom(1, new_ref)
            break

    # Update parsed footprint object
    for fp in pcb._footprints:
        if fp.reference == old_ref:
            fp.reference = new_ref
            # Also update the FootprintText objects
            for text in fp.texts:
                if text.text_type == "reference":
                    text.text = new_ref
            break

    return True


def _build_rename_plan(
    mapping: dict[str, str], existing_refs: set[str]
) -> tuple[list[tuple[str, str, str | None]], list[str], list[str]]:
    """Build a collision-safe rename plan from a mapping.

    Detects collision chains (where a rename target is also a rename source)
    and uses temporary intermediate references to resolve them safely.

    Args:
        mapping: Dict of old_ref -> new_ref.
        existing_refs: Set of all reference designators currently in the PCB.

    Returns:
        Tuple of (rename_steps, warnings, errors) where:
        - rename_steps is a list of (from_ref, to_ref, via_temp) tuples.
          via_temp is None for direct renames, or the temp ref for chain renames.
        - warnings is a list of warning messages.
        - errors is a list of error messages.
    """
    warnings: list[str] = []
    errors: list[str] = []

    # Validate: check that all source refs exist in the PCB
    for old_ref in mapping:
        if old_ref not in existing_refs:
            errors.append(f"Source reference '{old_ref}' not found in PCB")

    # Validate: check that target refs don't collide with existing refs
    # that are NOT themselves being renamed away
    mapping_sources = set(mapping.keys())
    for old_ref, new_ref in mapping.items():
        if new_ref in existing_refs and new_ref not in mapping_sources:
            errors.append(
                f"Target reference '{new_ref}' already exists in PCB "
                f"and is not being renamed (collision with '{old_ref}' -> '{new_ref}')"
            )

    if errors:
        return [], warnings, errors

    # Identify which renames are part of collision chains
    # A collision chain exists when a target ref is also a source ref
    sources_set = set(mapping.keys())
    targets_set = set(mapping.values())
    conflicting = sources_set & targets_set  # refs that are both source and target

    # Separate direct renames from chain renames
    direct_renames: list[tuple[str, str, str | None]] = []
    chain_sources: set[str] = set()

    # Walk chains to find all members
    for ref in conflicting:
        # Trace the chain: ref is a target of some other rename
        chain_sources.add(ref)

    # Also include sources that target a conflicting ref
    for old_ref, new_ref in mapping.items():
        if new_ref in conflicting or old_ref in conflicting:
            chain_sources.add(old_ref)

    # Generate temp refs that don't collide with anything
    all_refs = existing_refs | targets_set
    temp_counter = 0
    temp_map: dict[str, str] = {}  # old_ref -> temp_ref
    for old_ref in sorted(chain_sources):
        while f"_TEMP_{temp_counter}" in all_refs:
            temp_counter += 1
        temp_ref = f"_TEMP_{temp_counter}"
        temp_map[old_ref] = temp_ref
        all_refs.add(temp_ref)
        temp_counter += 1

    # Build rename steps
    rename_steps: list[tuple[str, str, str | None]] = []

    # Direct renames (not part of any chain)
    for old_ref, new_ref in sorted(mapping.items()):
        if old_ref not in chain_sources:
            direct_renames.append((old_ref, new_ref, None))

    rename_steps.extend(direct_renames)

    # Chain renames: phase 1 (source -> temp), phase 2 (temp -> final)
    for old_ref in sorted(chain_sources):
        rename_steps.append((old_ref, temp_map[old_ref], None))

    for old_ref in sorted(chain_sources):
        new_ref = mapping[old_ref]
        rename_steps.append((temp_map[old_ref], new_ref, old_ref))

    return rename_steps, warnings, errors


def _run_reannotate_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb reannotate' command."""
    from kicad_tools.schema.pcb import PCB

    # Load mapping from --map file
    map_path = getattr(args, "map", None)
    if not map_path:
        print("Error: --map is required (path to JSON mapping file)", file=sys.stderr)
        return 1

    map_file = Path(map_path)
    if not map_file.exists():
        print(f"Error: Mapping file not found: {map_file}", file=sys.stderr)
        return 1

    try:
        with open(map_file) as f:
            mapping = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in mapping file: {e}", file=sys.stderr)
        return 1

    if not isinstance(mapping, dict):
        print("Error: Mapping file must contain a JSON object (dict)", file=sys.stderr)
        return 1

    # Validate mapping values are strings
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            print("Error: All keys and values in mapping must be strings", file=sys.stderr)
            return 1

    # Handle empty mapping (no-op)
    if not mapping:
        output_format = getattr(args, "format", "text")
        if output_format == "json":
            print(json.dumps({"input": str(pcb_path), "renames": [], "status": "no-op"}))
        else:
            print("No renames specified in mapping file.")
        return 0

    # Load PCB
    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Get all existing references
    existing_refs = {fp.reference for fp in pcb.footprints}

    # Build collision-safe rename plan
    rename_steps, warnings, errors = _build_rename_plan(mapping, existing_refs)

    if errors:
        for err in errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    dry_run = getattr(args, "dry_run", False)
    output_format = getattr(args, "format", "text")

    # Build result for reporting
    result: dict = {
        "input": str(pcb_path),
        "dry_run": dry_run,
        "mapping": mapping,
        "renames": [],
        "warnings": warnings,
    }

    if dry_run:
        # Report what would be done without modifying
        for step_from, step_to, via_original in rename_steps:
            entry: dict[str, str | None] = {"from": step_from, "to": step_to}
            if via_original is not None:
                entry["original_source"] = via_original
            result["renames"].append(entry)

        if output_format == "json":
            print(json.dumps(result, indent=2))
        else:
            print("PCB Reannotate (dry run)")
            print(f"  Input: {pcb_path}")
            print()
            print(f"  Planned renames ({len(mapping)} mappings, {len(rename_steps)} steps):")
            for step_from, step_to, via_original in rename_steps:
                if via_original is not None:
                    print(f"    {step_from} -> {step_to}  (temp for {via_original})")
                else:
                    print(f"    {step_from} -> {step_to}")
            if warnings:
                print()
                for w in warnings:
                    print(f"  Warning: {w}")
        return 0

    # Execute renames
    applied: list[dict[str, str | None]] = []
    for step_from, step_to, via_original in rename_steps:
        success = _update_footprint_reference(pcb, step_from, step_to)
        if success:
            entry = {"from": step_from, "to": step_to}
            if via_original is not None:
                entry["original_source"] = via_original
            applied.append(entry)
        else:
            print(
                f"Error: Failed to rename '{step_from}' -> '{step_to}'",
                file=sys.stderr,
            )
            return 1

    result["renames"] = applied

    # Determine output path
    output_path = pcb_path
    if args.output:
        output_path = Path(args.output)

    result["output"] = str(output_path)

    # Save
    try:
        pcb.save(output_path)
    except Exception as e:
        print(f"Error saving PCB: {e}", file=sys.stderr)
        return 1

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        print("PCB Reannotate")
        print(f"  Input:  {pcb_path}")
        print(f"  Output: {output_path}")
        print()
        print(f"  Applied {len(mapping)} renames ({len(rename_steps)} steps):")
        for entry in applied:
            from_ref = entry["from"]
            to_ref = entry["to"]
            orig = entry.get("original_source")
            if orig is not None:
                print(f"    {from_ref} -> {to_ref}  (temp for {orig})")
            else:
                print(f"    {from_ref} -> {to_ref}")
        if warnings:
            print()
            for w in warnings:
                print(f"  Warning: {w}")

    return 0


def _run_sync_netlist_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb sync-netlist' command."""
    from kicad_tools.cli.pcb_sync_netlist import run_sync_netlist

    schematic = getattr(args, "schematic", None)
    if not schematic:
        print("Error: --schematic is required", file=sys.stderr)
        return 1

    schematic_path = Path(schematic)
    if not schematic_path.exists():
        print(f"Error: Schematic not found: {schematic_path}", file=sys.stderr)
        return 1

    output = getattr(args, "output", None)
    output_path = Path(output) if output else None
    dry_run = getattr(args, "dry_run", False)
    output_format = getattr(args, "format", "text")
    remove_orphans = getattr(args, "remove_orphans", False)
    force = getattr(args, "force", False)
    auto_rename = getattr(args, "auto_rename", False)
    remove_orphan_nets = getattr(args, "remove_orphan_nets", False)

    return run_sync_netlist(
        schematic_path=schematic_path,
        pcb_path=pcb_path,
        dry_run=dry_run,
        output_path=output_path,
        output_format=output_format,
        remove_orphans=remove_orphans,
        force=force,
        auto_rename=auto_rename,
        remove_orphan_nets=remove_orphan_nets,
    )


def _run_remove_footprint_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb remove-footprint' command."""
    from kicad_tools.cli.pcb_remove_footprint import run_remove_footprint

    ref = getattr(args, "ref", None)
    if not ref:
        print("Error: --ref is required", file=sys.stderr)
        return 1

    output = getattr(args, "output", None)
    output_path = Path(output) if output else None
    dry_run = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)
    output_format = getattr(args, "format", "text")

    return run_remove_footprint(
        pcb_path=pcb_path,
        reference=ref,
        dry_run=dry_run,
        output_path=output_path,
        force=force,
        output_format=output_format,
    )


def _run_move_footprint_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb move-footprint' command."""
    from kicad_tools.cli.pcb_move_footprint import run_move_footprint

    ref = getattr(args, "ref", None)
    to = getattr(args, "to", None)
    rotation = getattr(args, "rotation", None)
    batch_map_str = getattr(args, "batch_map", None)
    output = getattr(args, "output", None)
    output_path = Path(output) if output else None
    dry_run = getattr(args, "dry_run", False)
    output_format = getattr(args, "format", "text")

    # Parse batch map JSON if provided
    batch_map = None
    if batch_map_str is not None:
        try:
            batch_map = json.loads(batch_map_str)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in --map: {e}", file=sys.stderr)
            return 1
        if not isinstance(batch_map, dict):
            print("Error: --map must be a JSON object", file=sys.stderr)
            return 1

    # Validate: need either --ref/--to or --map
    if batch_map is None:
        if not ref:
            print("Error: --ref is required (or use --map for batch mode)", file=sys.stderr)
            return 1
        if not to:
            print("Error: --to is required (or use --map for batch mode)", file=sys.stderr)
            return 1

    to_tuple = tuple(to) if to else None

    return run_move_footprint(
        pcb_path=pcb_path,
        reference=ref,
        to=to_tuple,
        rotation=rotation,
        batch_map=batch_map,
        dry_run=dry_run,
        output_path=output_path,
        output_format=output_format,
    )


def _run_page_fit_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb page-fit' command."""
    from kicad_tools.cli.pcb_page_fit import run_page_fit

    margin = getattr(args, "margin", 5.0)
    output = getattr(args, "output", None)
    output_path = Path(output) if output else None
    dry_run = getattr(args, "dry_run", False)
    output_format = getattr(args, "format", "text")

    return run_page_fit(
        pcb_path=pcb_path,
        margin=margin,
        dry_run=dry_run,
        output_path=output_path,
        output_format=output_format,
    )


def _run_lock_footprints_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb lock-footprints' / 'pcb unlock-footprints' commands."""
    from kicad_tools.cli.pcb_lock_footprints import run_lock_footprints

    refs_str = getattr(args, "refs", None)
    all_perimeter = getattr(args, "all_perimeter", False)
    perimeter_margin = getattr(args, "perimeter_margin", None)
    output = getattr(args, "output", None)
    output_path = Path(output) if output else None
    dry_run = getattr(args, "dry_run", False)
    output_format = getattr(args, "format", "text")
    unlock = args.pcb_command == "unlock-footprints"

    refs: list[str] | None = None
    if refs_str:
        refs = [r.strip() for r in refs_str.split(",") if r.strip()]
        if not refs:
            print("Error: --refs must contain at least one reference", file=sys.stderr)
            return 1

    return run_lock_footprints(
        pcb_path=pcb_path,
        refs=refs,
        all_perimeter=all_perimeter,
        perimeter_margin=perimeter_margin,
        unlock=unlock,
        dry_run=dry_run,
        output_path=output_path,
        output_format=output_format,
    )


def _run_add_zone_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb add-zone' command.

    Delegates to the existing ZoneGenerator infrastructure from
    kicad_tools.zones, adding support for --rect/--origin/--size
    rectangular boundary specification.
    """
    from kicad_tools.zones import ZoneGenerator

    # Validate --rect requires --origin and --size
    use_rect = getattr(args, "rect", False)
    origin = getattr(args, "origin", None)
    size = getattr(args, "size", None)

    if use_rect:
        if origin is None or size is None:
            print(
                "Error: --rect requires both --origin X Y and --size W H",
                file=sys.stderr,
            )
            return 1

    # Build rectangular boundary polygon if requested
    boundary = None
    if use_rect and origin is not None and size is not None:
        x0, y0 = origin
        w, h = size
        if w <= 0 or h <= 0:
            print("Error: --size width and height must be positive", file=sys.stderr)
            return 1
        boundary = [
            (x0, y0),
            (x0 + w, y0),
            (x0 + w, y0 + h),
            (x0, y0 + h),
        ]

    # Determine output path
    output = getattr(args, "output", None)
    if output:
        output_path = Path(output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_zones")

    dry_run = getattr(args, "dry_run", False)
    output_format = getattr(args, "format", "text")

    try:
        gen = ZoneGenerator.from_pcb(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Map CLI flags to ZoneGenerator parameters
    try:
        zone = gen.add_zone(
            net=args.net,
            layer=args.layer,
            priority=getattr(args, "priority", 0),
            clearance=getattr(args, "min_clearance", 0.3),
            thermal_gap=getattr(args, "thermal_relief_gap", 0.3),
            thermal_bridge_width=getattr(args, "thermal_relief_width", 0.4),
            min_thickness=getattr(args, "min_thickness", 0.25),
            boundary=boundary,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Build result for reporting
    result = {
        "input": str(pcb_path),
        "output": str(output_path) if not dry_run else None,
        "dry_run": dry_run,
        "zone": {
            "net": zone.config.net,
            "layer": zone.config.layer,
            "priority": zone.config.priority,
            "clearance": zone.config.clearance,
            "thermal_gap": zone.config.thermal_gap,
            "thermal_bridge_width": zone.config.thermal_bridge_width,
            "boundary_points": len(zone.boundary),
            "boundary_type": "rectangle" if use_rect else "board_outline",
        },
        "warnings": [w.message for w in gen.warnings],
    }

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        label = "(dry run) " if dry_run else ""
        print(f"PCB Add Zone {label}")
        print(f"  Input:  {pcb_path}")
        if not dry_run:
            print(f"  Output: {output_path}")
        print()
        print(f"  Net:              {zone.config.net}")
        print(f"  Layer:            {zone.config.layer}")
        print(f"  Priority:         {zone.config.priority}")
        print(f"  Clearance:        {zone.config.clearance}mm")
        print(f"  Thermal gap:      {zone.config.thermal_gap}mm")
        print(f"  Thermal width:    {zone.config.thermal_bridge_width}mm")
        print(f"  Boundary:         {len(zone.boundary)} points", end="")
        if use_rect:
            print(" (rectangle)")
        else:
            print(" (board outline)")

    # Surface overlap warnings
    if gen.warnings:
        if output_format == "text":
            print(f"\n{len(gen.warnings)} overlap warning(s):")
            for w in gen.warnings:
                print(f"  WARNING: {w.message}", file=sys.stderr)

    if dry_run:
        return 0

    # Save
    try:
        gen.save(output_path)
    except Exception as e:
        print(f"Error saving PCB: {e}", file=sys.stderr)
        return 1

    if output_format == "text":
        print(f"\n  Saved to: {output_path}")

    return 0


def _run_snap_rotation_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb snap-rotation' command.

    Normalizes footprint rotation angles to the nearest cardinal angle
    (0, 90, 180, 270 by default).
    """
    from kicad_tools.schema.pcb import PCB

    grid = getattr(args, "grid", 90.0)
    tolerance = getattr(args, "tolerance", None)
    exclude = getattr(args, "exclude", None)
    only = getattr(args, "only", None)
    dry_run = getattr(args, "dry_run", False)
    output_format = getattr(args, "format", "text")

    # Parse comma-separated reference lists
    exclude_refs: set[str] = set()
    if exclude:
        exclude_refs = {r.strip() for r in exclude.split(",")}
    only_refs: set[str] = set()
    if only:
        only_refs = {r.strip() for r in only.split(",")}

    # Load PCB
    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Snap rotations
    changes: list[dict] = []
    for fp in pcb.footprints:
        ref = fp.reference

        # Filter by --exclude / --only
        if exclude_refs and ref in exclude_refs:
            continue
        if only_refs and ref not in only_refs:
            continue

        old_rotation = fp.rotation
        snapped = round(old_rotation / grid) * grid % 360

        # Apply tolerance filter: skip if delta exceeds tolerance
        delta = abs(old_rotation - snapped)
        # Handle wraparound (e.g., 359 -> 0 has delta 1, not 359)
        if delta > 180:
            delta = 360 - delta
        if tolerance is not None and delta > tolerance:
            continue

        if abs(old_rotation - snapped) > 1e-6 or (
            abs(old_rotation - 360.0) < 1e-6 and snapped == 0.0
        ):
            changes.append(
                {
                    "reference": ref,
                    "old_rotation": old_rotation,
                    "new_rotation": snapped,
                }
            )
            if not dry_run:
                fp.rotation = snapped
                # When snapping to 0, remove the third child from the (at ...)
                # node so KiCad does not serialize a stale rotation value.
                if snapped == 0.0 and fp._sexp_node is not None:
                    at_node = fp._sexp_node.find("at")
                    if at_node is not None and len(at_node.children) >= 3:
                        del at_node.children[2]

    # Determine output path
    output_path = pcb_path
    if args.output:
        output_path = Path(args.output)

    # Build result
    result = {
        "input": str(pcb_path),
        "output": str(output_path) if not dry_run else None,
        "dry_run": dry_run,
        "grid": grid,
        "tolerance": tolerance,
        "total_footprints": len(pcb.footprints),
        "snapped": len(changes),
        "changes": changes,
    }

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(f"PCB Snap Rotation {'(dry run)' if dry_run else ''}")
        print(f"  Input:  {pcb_path}")
        if not dry_run:
            print(f"  Output: {output_path}")
        print(f"  Grid:   {grid} degrees")
        if tolerance is not None:
            print(f"  Tolerance: {tolerance} degrees")
        print()

        if changes:
            print(f"  {len(changes)} footprint(s) snapped:")
            for c in changes:
                print(f"    {c['reference']}: {c['old_rotation']:.1f} -> {c['new_rotation']:.1f}")
        else:
            print("  No footprints needed rotation adjustment.")

    # Save unless dry-run
    if not dry_run and changes:
        try:
            pcb.save(output_path)
            if output_format == "text":
                print()
                print(f"  Saved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB: {e}", file=sys.stderr)
            return 1

    return 0


def _run_edit_outline_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb edit-outline' command."""
    from kicad_tools.schema.pcb import PCB

    output_format = getattr(args, "format", "text")
    dry_run = getattr(args, "dry_run", False)

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # --list: display contours
    if getattr(args, "list_contours", False):
        contours = pcb.list_edge_contours()
        if not contours:
            if output_format == "json":
                print(json.dumps({"contours": []}))
            else:
                print("No Edge.Cuts contours found.")
            return 0

        if output_format == "json":
            data = []
            for c in contours:
                data.append(
                    {
                        "index": c.index,
                        "elements": c.element_count,
                        "bbox": {
                            "min_x": round(c.bbox[0], 2),
                            "min_y": round(c.bbox[1], 2),
                            "max_x": round(c.bbox[2], 2),
                            "max_y": round(c.bbox[3], 2),
                        },
                        "width_mm": round(c.bbox_width, 2),
                        "height_mm": round(c.bbox_height, 2),
                        "area_mm2": round(c.bbox_area, 2),
                        "is_mounting_hole": c.is_mounting_hole,
                    }
                )
            print(json.dumps({"contours": data}, indent=2))
        else:
            print(f"Found {len(contours)} Edge.Cuts contour(s):\n")
            for c in contours:
                label = " [mounting hole]" if c.is_mounting_hole else ""
                print(
                    f"  [{c.index}] {c.element_count} element(s), "
                    f"bbox ({c.bbox[0]:.2f}, {c.bbox[1]:.2f}) - "
                    f"({c.bbox[2]:.2f}, {c.bbox[3]:.2f}), "
                    f"{c.bbox_width:.2f} x {c.bbox_height:.2f} mm"
                    f"{label}"
                )
        return 0

    # --remove-outline INDEX
    remove_idx = getattr(args, "remove_outline", None)
    if remove_idx is not None:
        contours = pcb.list_edge_contours()
        target = None
        for c in contours:
            if c.index == remove_idx:
                target = c
                break
        if target is None:
            print(f"Error: No contour with index {remove_idx}", file=sys.stderr)
            return 1

        if dry_run:
            print(
                f"Would remove contour [{remove_idx}]: "
                f"{target.element_count} element(s), "
                f"bbox ({target.bbox[0]:.2f}, {target.bbox[1]:.2f}) - "
                f"({target.bbox[2]:.2f}, {target.bbox[3]:.2f})"
            )
            return 0

        ok = pcb.remove_edge_contour(remove_idx)
        if not ok:
            print(f"Error: Failed to remove contour {remove_idx}", file=sys.stderr)
            return 1

        output = getattr(args, "output", None)
        save_path = Path(output) if output else pcb_path
        pcb.save(save_path)
        print(
            f"Removed contour [{remove_idx}] ({target.element_count} element(s)). Saved to {save_path}"
        )
        return 0

    # --keep-only INDEX
    keep_idx = getattr(args, "keep_only", None)
    if keep_idx is not None:
        contours = pcb.list_edge_contours()
        target = None
        for c in contours:
            if c.index == keep_idx:
                target = c
                break
        if target is None:
            print(f"Error: No contour with index {keep_idx}", file=sys.stderr)
            return 1

        to_remove = [c for c in contours if c.index != keep_idx and not c.is_mounting_hole]

        if dry_run:
            print(f"Would keep contour [{keep_idx}] and remove {len(to_remove)} other outline(s).")
            for c in to_remove:
                print(
                    f"  Remove [{c.index}]: {c.element_count} element(s), "
                    f"bbox ({c.bbox[0]:.2f}, {c.bbox[1]:.2f}) - "
                    f"({c.bbox[2]:.2f}, {c.bbox[3]:.2f})"
                )
            return 0

        # Remove in reverse index order to keep indices stable
        for c in sorted(to_remove, key=lambda c: c.index, reverse=True):
            pcb.remove_edge_contour(c.index)

        output = getattr(args, "output", None)
        save_path = Path(output) if output else pcb_path
        pcb.save(save_path)
        print(
            f"Kept contour [{keep_idx}], removed {len(to_remove)} outline(s). Saved to {save_path}"
        )
        return 0

    # --set-outline rect
    set_outline = getattr(args, "set_outline", None)
    if set_outline == "rect":
        origin = getattr(args, "origin", None)
        size = getattr(args, "size", None)
        if not origin or not size:
            print(
                "Error: --set-outline rect requires --origin X Y and --size W H",
                file=sys.stderr,
            )
            return 1

        origin_x, origin_y = origin
        width, height = size

        if dry_run:
            contours = pcb.list_edge_contours()
            outline_count = sum(1 for c in contours if not c.is_mounting_hole)
            print(
                f"Would replace {outline_count} outline contour(s) with "
                f"rect at ({origin_x}, {origin_y}), size {width} x {height} mm"
            )
            return 0

        removed = pcb.replace_outline(origin_x, origin_y, width, height)

        output = getattr(args, "output", None)
        save_path = Path(output) if output else pcb_path
        pcb.save(save_path)
        print(
            f"Replaced {removed} outline(s) with rect at "
            f"({origin_x}, {origin_y}), size {width} x {height} mm. "
            f"Saved to {save_path}"
        )
        return 0

    print(
        "Error: No action specified. Use --list, --remove-outline, --keep-only, or --set-outline.",
        file=sys.stderr,
    )
    return 1


def _run_net_audit_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb net-audit' command."""
    from kicad_tools.audit.net_audit import find_stale_nets, fix_stale_nets
    from kicad_tools.schema.pcb import PCB

    output_format = getattr(args, "format", "text")
    fix = getattr(args, "fix", False)
    dry_run = getattr(args, "dry_run", False)

    # --dry-run implies --fix (shows what would be fixed)
    if dry_run:
        fix = True

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    groups = find_stale_nets(pcb)

    # Build result data
    result = {
        "input": str(pcb_path),
        "stale_nets": len(groups),
        "findings": [],
    }

    for group in groups:
        finding = {
            "stale_net": group.stale_net_name,
            "stale_net_number": group.stale_net_number,
            "active_net": group.active_net_name,
            "active_net_number": group.active_net_number,
            "active_segments": group.active_segment_count,
            "active_vias": group.active_via_count,
            "affected_pads": [
                {
                    "footprint": pad.footprint_ref,
                    "pad": pad.pad_number,
                    "current_net": pad.current_net,
                }
                for pad in group.affected_pads
            ],
        }
        result["findings"].append(finding)

    # Apply fix if requested
    fixed_count = 0
    if fix and groups:
        if not dry_run:
            fixed_count = fix_stale_nets(pcb, groups)
            result["fixed_pads"] = fixed_count

            # Save
            output = getattr(args, "output", None)
            output_path = Path(output) if output else pcb_path
            result["output"] = str(output_path)
            try:
                pcb.save(output_path)
            except Exception as e:
                print(f"Error saving PCB: {e}", file=sys.stderr)
                return 1
        else:
            # Dry-run: count what would be fixed
            total_pads = sum(len(g.affected_pads) for g in groups)
            result["dry_run"] = True
            result["would_fix_pads"] = total_pads

    # Output
    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        if not groups:
            print("No stale or duplicate nets found.")
            return 0

        label = " (dry run)" if dry_run else ""
        print(f"PCB Net Audit{label}")
        print(f"  Input: {pcb_path}")
        print(f"  Found {len(groups)} stale net(s):\n")

        for group in groups:
            print(f"  Stale:  {group.stale_net_name} (net {group.stale_net_number})")
            print(f"  Active: {group.active_net_name} (net {group.active_net_number})")
            print(f"    Segments: {group.active_segment_count}, Vias: {group.active_via_count}")
            if group.affected_pads:
                print(f"    Affected pads ({len(group.affected_pads)}):")
                for pad in group.affected_pads:
                    print(f"      {pad.footprint_ref} pad {pad.pad_number}")
            print()

        if fix and not dry_run:
            print(f"  Fixed {fixed_count} pad(s).")
            output = getattr(args, "output", None)
            output_path = Path(output) if output else pcb_path
            print(f"  Saved to: {output_path}")
        elif dry_run:
            total_pads = sum(len(g.affected_pads) for g in groups)
            print(f"  Would fix {total_pads} pad(s).")

    # Return non-zero if stale nets found and --fix was not used
    if groups and not fix:
        return 1
    return 0


def _run_export_dsn_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb export-dsn' command."""
    from kicad_tools.export.dsn import KiCadToDSNExporter

    output = getattr(args, "output", None)
    if output is None:
        output = pcb_path.with_suffix(".dsn")
    else:
        output = Path(output)

    try:
        exporter = KiCadToDSNExporter(str(pcb_path))
        exporter.export(str(output))
    except Exception as e:
        print(f"Error exporting DSN: {e}", file=sys.stderr)
        return 1

    print(f"Exported DSN to {output}")
    print(f"  Layers: {len(exporter.layers)}")
    print(f"  Nets: {len(exporter.nets)}")
    print(f"  Components: {len(exporter.footprints)}")
    return 0


def _run_import_ses_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb import-ses' command."""
    from kicad_tools.export.ses import SESToKiCadImporter

    ses_path = Path(args.ses)
    if not ses_path.exists():
        print(f"Error: SES file not found: {ses_path}", file=sys.stderr)
        return 1

    output = getattr(args, "output", None)
    if output is not None:
        output = Path(output)

    try:
        importer = SESToKiCadImporter(str(ses_path))
        importer.parse()
        importer.merge_into(str(pcb_path), str(output) if output else None)
    except Exception as e:
        print(f"Error importing SES: {e}", file=sys.stderr)
        return 1

    dest = output or pcb_path
    print(f"Imported SES routes into {dest}")
    print(f"  Wires: {len(importer.wires)}")
    print(f"  Vias: {len(importer.vias)}")
    return 0
