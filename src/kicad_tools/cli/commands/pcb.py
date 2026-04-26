"""PCB (pcb) subcommand handlers."""

import json
import sys
from pathlib import Path

__all__ = ["run_pcb_command"]


def run_pcb_command(args) -> int:
    """Handle PCB subcommands."""
    if not args.pcb_command:
        print("Usage: kicad-tools pcb <command> [options] <file>")
        print("Commands: summary, footprints, nets, traces, stackup, strip, reannotate, sync-netlist, remove-footprint, add-zone, snap-rotation")
        return 1

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

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

    # Handle add-zone command
    if args.pcb_command == "add-zone":
        return _run_add_zone_command(args, pcb_path)

    # Handle snap-rotation command
    if args.pcb_command == "snap-rotation":
        return _run_snap_rotation_command(args, pcb_path)

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


def _run_strip_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb strip' command."""
    from kicad_tools.schema.pcb import PCB

    # Parse net names if provided
    nets = None
    if args.nets:
        nets = [n.strip() for n in args.nets.split(",")]

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
    stats = pcb.strip_traces(nets=nets, keep_zones=keep_zones)

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
        "keep_zones": keep_zones,
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
        print(f"  Keep zones: {keep_zones}")
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

    return run_sync_netlist(
        schematic_path=schematic_path,
        pcb_path=pcb_path,
        dry_run=dry_run,
        output_path=output_path,
        output_format=output_format,
        remove_orphans=remove_orphans,
        force=force,
        auto_rename=auto_rename,
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

        if abs(old_rotation - snapped) > 1e-6 or (abs(old_rotation - 360.0) < 1e-6 and snapped == 0.0):
            changes.append({
                "reference": ref,
                "old_rotation": old_rotation,
                "new_rotation": snapped,
            })
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
