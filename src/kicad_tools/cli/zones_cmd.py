"""
CLI command for zone generation and fill.

Provides commands for adding copper pour zones to PCB files and
filling zones using kicad-cli:
    kicad-tools zones add board.kicad_pcb --net GND --layer B.Cu
    kicad-tools zones list board.kicad_pcb
    kicad-tools zones fill board.kicad_pcb
"""

import argparse
import sys
from pathlib import Path


def parse_bbox(spec: str) -> list[tuple[float, float]]:
    """Parse a ``MINX,MINY,MAXX,MAXY`` bbox string into a polygon.

    Returns the four corner points of the rectangle in counter-clockwise
    order, in sheet-absolute millimetres.

    Args:
        spec: Bounding-box specification, e.g. ``"10,10,40,30"``.

    Returns:
        List of four ``(x, y)`` corner tuples.

    Raises:
        ValueError: If the format is malformed (wrong arity, non-numeric,
            or ``min >= max`` on either axis).
    """
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 4:
        raise ValueError(
            f"Invalid --bbox '{spec}': expected 4 comma-separated numbers "
            "'MINX,MINY,MAXX,MAXY' (e.g. '10,10,40,30')"
        )
    try:
        min_x, min_y, max_x, max_y = (float(p) for p in parts)
    except ValueError as exc:
        raise ValueError(
            f"Invalid --bbox '{spec}': all values must be numeric (e.g. '10,10,40,30')"
        ) from exc

    if min_x >= max_x:
        raise ValueError(
            f"Invalid --bbox '{spec}': MINX ({min_x}) must be less than MAXX ({max_x})"
        )
    if min_y >= max_y:
        raise ValueError(
            f"Invalid --bbox '{spec}': MINY ({min_y}) must be less than MAXY ({max_y})"
        )

    return [
        (min_x, min_y),
        (max_x, min_y),
        (max_x, max_y),
        (min_x, max_y),
    ]


def parse_region(spec: str) -> list[tuple[float, float]]:
    """Parse a ``X1,Y1;X2,Y2;...`` polygon string into a list of points.

    Args:
        spec: Polygon specification, e.g. ``"10,10;40,10;40,30;10,30"``.

    Returns:
        List of ``(x, y)`` tuples in sheet-absolute millimetres.

    Raises:
        ValueError: If the format is malformed (fewer than 3 points, wrong
            arity per point, or non-numeric values).
    """
    points: list[tuple[float, float]] = []
    items = [p.strip() for p in spec.split(";") if p.strip()]
    for item in items:
        coords = [c.strip() for c in item.split(",")]
        if len(coords) != 2:
            raise ValueError(
                f"Invalid --region point '{item}': expected 'X,Y' (e.g. '10,10;40,10;40,30;10,30')"
            )
        try:
            x, y = (float(c) for c in coords)
        except ValueError as exc:
            raise ValueError(
                f"Invalid --region point '{item}': X and Y must be numeric "
                "(e.g. '10,10;40,10;40,30;10,30')"
            ) from exc
        points.append((x, y))

    if len(points) < 3:
        raise ValueError(
            f"Invalid --region '{spec}': a polygon needs at least 3 points "
            "(e.g. '10,10;40,10;40,30;10,30')"
        )

    return points


def main(argv: list[str] | None = None) -> int:
    """Main entry point for zones command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools zones",
        description="Copper zone generation for PCBs",
    )

    subparsers = parser.add_subparsers(dest="zones_command", help="Zone commands")

    # zones add
    add_parser = subparsers.add_parser("add", help="Add copper zones to a PCB")
    add_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    add_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input, consistent with 'zones fill')",
    )
    add_parser.add_argument(
        "--net",
        required=True,
        help="Net name for the zone (e.g., GND, +3.3V)",
    )
    add_parser.add_argument(
        "--layer",
        required=True,
        help="Copper layer (e.g., F.Cu, B.Cu, In1.Cu)",
    )
    add_parser.add_argument(
        "--priority",
        type=int,
        default=0,
        help="Zone fill priority (higher = fills later, default: 0)",
    )
    add_parser.add_argument(
        "--clearance",
        type=float,
        default=0.3,
        help="Clearance to other nets in mm (default: 0.3)",
    )
    add_parser.add_argument(
        "--thermal-gap",
        type=float,
        default=0.3,
        help="Thermal relief gap in mm (default: 0.3)",
    )
    add_parser.add_argument(
        "--thermal-bridge",
        type=float,
        default=0.4,
        help="Thermal relief spoke width in mm (default: 0.4)",
    )
    add_parser.add_argument(
        "--min-thickness",
        type=float,
        default=0.25,
        help="Minimum copper thickness in mm (default: 0.25)",
    )
    region_group = add_parser.add_mutually_exclusive_group()
    region_group.add_argument(
        "--bbox",
        metavar="MINX,MINY,MAXX,MAXY",
        help=(
            "Restrict the pour to a rectangular region (island pour) instead "
            "of the full board outline. Coordinates are sheet-absolute "
            "millimetres (same frame as `zones list` output). "
            "Example: --bbox 10,10,40,30"
        ),
    )
    region_group.add_argument(
        "--region",
        metavar="X1,Y1;X2,Y2;...",
        help=(
            "Restrict the pour to an explicit polygon region (island pour) "
            "instead of the full board outline. At least 3 points, each "
            "'X,Y' separated by ';'. Coordinates are sheet-absolute "
            "millimetres (same frame as `zones list` output). "
            "Example: --region 10,10;40,10;40,30;10,30"
        ),
    )
    add_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    add_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing output",
    )
    add_parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )

    # zones list
    list_parser = subparsers.add_parser("list", help="List existing zones in a PCB")
    list_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    list_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    # zones batch - add multiple zones at once
    batch_parser = subparsers.add_parser("batch", help="Add multiple zones from spec")
    batch_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    batch_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input, consistent with 'zones fill')",
    )
    batch_parser.add_argument(
        "--power-nets",
        required=True,
        help="Power nets specification: 'NET1:LAYER1,NET2:LAYER2,...' (e.g., 'GND:B.Cu,+3.3V:F.Cu')",
    )
    batch_parser.add_argument(
        "--clearance",
        type=float,
        default=0.3,
        help="Clearance to other nets in mm (default: 0.3)",
    )
    batch_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    batch_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing output",
    )
    batch_parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )

    # zones hv-keepout
    hv_parser = subparsers.add_parser(
        "hv-keepout",
        help="Generate plane pour-keepouts so inner pours clear HV nets",
    )
    hv_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    hv_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrite input, consistent with 'zones fill')",
    )
    hv_parser.add_argument(
        "--net-class",
        default="HV",
        help="Net class naming the HV group (default: HV)",
    )
    hv_parser.add_argument(
        "--net-class-map",
        help="Path to a net-class-map JSON sidecar classifying the HV nets "
        "(same file 'kct creepage' accepts)",
    )
    hv_parser.add_argument(
        "--clearance",
        type=float,
        required=True,
        help="Required clearance from HV copper in mm (the void distance)",
    )
    hv_parser.add_argument(
        "--plane-layers",
        help="Comma-separated copper layers whose pours must void "
        "(e.g. 'In1.Cu,In2.Cu'). Default: all layers carrying a plane pour.",
    )
    hv_parser.add_argument(
        "--refill",
        action="store_true",
        help="Run 'kicad-cli pcb drc --refill-zones' after writing the keepouts",
    )
    hv_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    hv_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned voids without writing output",
    )
    hv_parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )

    # zones fill
    fill_parser = subparsers.add_parser("fill", help="Fill all zones in a PCB")
    fill_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    fill_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: overwrites input)",
    )
    fill_parser.add_argument(
        "--net",
        help="Fill only zones for this net (e.g., GND)",
    )
    fill_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    fill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing output",
    )
    fill_parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )

    args = parser.parse_args(argv)

    if not args.zones_command:
        parser.print_help()
        return 0

    if args.zones_command == "add":
        return _run_add(args)
    elif args.zones_command == "list":
        return _run_list(args)
    elif args.zones_command == "batch":
        return _run_batch(args)
    elif args.zones_command == "hv-keepout":
        return _run_hv_keepout(args)
    elif args.zones_command == "fill":
        return _run_fill(args)

    return 0


def _run_add(args) -> int:
    """Add a single zone to a PCB."""
    from kicad_tools.zones import ZoneGenerator

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Determine output path. No -o means overwrite the input in place,
    # consistent with `zones fill` and `optimize-placement`. This makes a
    # chained `zones add ... && zones add ...` accumulate zones, since each
    # call reads its own prior output instead of the pristine input.
    output_path = Path(args.output) if args.output else pcb_path

    quiet = args.quiet

    # Parse optional region/island boundary (mutually exclusive in argparse).
    boundary = None
    try:
        if args.bbox:
            boundary = parse_bbox(args.bbox)
        elif args.region:
            boundary = parse_region(args.region)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not quiet:
        print(f"Loading PCB: {pcb_path}")

    try:
        gen = ZoneGenerator.from_pcb(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Add the zone
    try:
        zone = gen.add_zone(
            net=args.net,
            layer=args.layer,
            priority=args.priority,
            clearance=args.clearance,
            thermal_gap=args.thermal_gap,
            thermal_bridge_width=args.thermal_bridge,
            min_thickness=args.min_thickness,
            boundary=boundary,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not quiet:
        print("\nZone created:")
        print(f"  Net:         {zone.config.net}")
        print(f"  Layer:       {zone.config.layer}")
        print(f"  Priority:    {zone.config.priority}")
        print(f"  Clearance:   {zone.config.clearance}mm")
        print(f"  Boundary:    {len(zone.boundary)} points")

    # Surface overlap warnings
    if gen.warnings:
        print(f"\n{len(gen.warnings)} overlap warning(s):", file=sys.stderr)
        for w in gen.warnings:
            print(f"  WARNING: {w.message}", file=sys.stderr)

    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
            print("\nGenerated S-expression:")
            print(gen.generate_sexp())
        return 0

    # Save
    if not quiet:
        print(f"\nSaving to: {output_path}")

    try:
        gen.save(output_path)
    except Exception as e:
        print(f"Error: Write verification failed: {e}", file=sys.stderr)
        return 1

    if not quiet:
        print("Done!")

    return 0


def _run_list(args) -> int:
    """List existing zones in a PCB."""
    import json

    from kicad_tools.schema.pcb import PCB

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    zones = pcb.zones

    if args.format == "json":
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
            print(f"  Filled:    {'Yes' if zone.is_filled else 'No'}")
            print(f"  Boundary:  {len(zone.polygon)} points")
            print()

    return 0


def _run_batch(args) -> int:
    """Add multiple zones from a power-nets specification.

    Priorities and per-net boundaries are auto-derived (issue #4167):
    within each user-specified layer group, zones are prioritized by
    ascending pad-cluster bbox area (smallest area => highest priority,
    the KiCad idiom) and given carved, geometrically-disjoint outlines so
    overlapping same-layer zones do not zero each other out.  Nets that
    are sole on their layer keep the full board outline and their legacy
    priority (1 for GND-named nets, 0 otherwise) unchanged.
    """
    from kicad_tools.zones import (
        ZoneGenerator,
        ZonePartitionError,
        assign_batch_zone_priorities_and_outlines,
        parse_power_nets,
    )

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Parse power nets spec
    try:
        power_nets = parse_power_nets(args.power_nets)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not power_nets:
        print("Error: No power nets specified", file=sys.stderr)
        return 1

    # Determine output path. No -o means overwrite the input in place,
    # consistent with `zones fill` and `optimize-placement`.
    output_path = Path(args.output) if args.output else pcb_path

    quiet = args.quiet

    if not quiet:
        print(f"Loading PCB: {pcb_path}")

    try:
        gen = ZoneGenerator.from_pcb(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Auto-assign priorities (by pad-cluster bbox area) and carve outlines
    # for same-layer groups so overlapping zones receive disjoint copper
    # instead of full-board rectangles that zero each other out (#4167).
    try:
        allocation = assign_batch_zone_priorities_and_outlines(
            gen.pcb,
            gen.board_outline,
            power_nets,
        )
    except ZonePartitionError as e:
        # Carving genuinely cannot produce disjoint copper for some net
        # (fully-coincident pad clusters).  Refuse rather than silently
        # writing zero-copper zones.
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if not quiet:
        print(f"\nAdding {len(power_nets)} zone(s):")

    errors = []
    for net_name, layer in power_nets:
        # Keyed by (net, layer): a net on multiple layers must get the
        # correct per-layer outline (full board where it is sole, carved
        # where it overlaps siblings) -- issue #4167.
        priority, boundary = allocation.get((net_name, layer), (0, None))

        try:
            gen.add_zone(
                net=net_name,
                layer=layer,
                priority=priority,
                clearance=args.clearance,
                boundary=boundary,
            )
            if not quiet:
                carved = " (carved)" if boundary is not None else ""
                print(f"  {net_name} on {layer} (priority {priority}){carved}")
        except ValueError as e:
            errors.append(f"  {net_name}: {e}")

    if errors:
        print("\nErrors:", file=sys.stderr)
        for err in errors:
            print(err, file=sys.stderr)

    # Surface overlap warnings
    if gen.warnings:
        print(f"\n{len(gen.warnings)} overlap warning(s):", file=sys.stderr)
        for w in gen.warnings:
            print(f"  WARNING: {w.message}", file=sys.stderr)

    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
            _print_batch_summary(gen, quiet)
            if args.verbose:
                print("\nGenerated S-expression:")
                print(gen.generate_sexp())
        return 0 if not errors else 1

    # Save
    if not quiet:
        print(f"\nSaving to: {output_path}")

    try:
        gen.save(output_path)
    except Exception as e:
        print(f"Error: Write verification failed: {e}", file=sys.stderr)
        return 1

    if not quiet:
        _print_batch_summary(gen, quiet)

    return 0 if not errors else 1


def _print_batch_summary(gen, quiet: bool) -> None:
    """Print the batch summary, promoting any zero-copper warning count.

    Surfaces ``len(gen.warnings)`` (zones that still cede area / would get
    zero copper) into the final summary line so the failure is non-silent
    (issue #4167), rather than only being visible in scrolled-back stderr.
    """
    if quiet:
        return
    stats = gen.get_statistics()
    zone_count = stats["zone_count"]
    warn_count = len(gen.warnings)
    if warn_count:
        print(
            f"\nCreated {zone_count} zone(s) "
            f"({warn_count} with zero-copper overlap warning(s) -- see above)"
        )
    else:
        print(f"\nCreated {zone_count} zone(s)")


def _load_net_class_map(path_str: str | None):
    """Load a net-class-map JSON sidecar (shared with ``kct creepage``).

    Returns ``(net_class_map, error_message)``.  On success ``error_message``
    is ``None``; on failure ``net_class_map`` is ``None`` and the caller should
    print ``error_message`` to stderr and exit non-zero.
    """
    if not path_str:
        return None, None

    import json

    from kicad_tools.router.rules import net_class_map_from_dict

    ncm_path = Path(path_str)
    if not ncm_path.exists():
        return None, f"net-class-map file not found: {ncm_path}"
    try:
        return net_class_map_from_dict(json.loads(ncm_path.read_text())), None
    except json.JSONDecodeError as e:
        return None, f"parsing net-class-map JSON: {e}"
    except (TypeError, ValueError) as e:
        return None, f"invalid net-class-map structure: {e}"


def _run_hv_keepout(args) -> int:
    """Generate plane pour-keepouts so inner pours clear HV nets (issue #4372).

    Resolves the HV net set exactly as ``kct creepage`` does, buffers the HV
    copper by ``--clearance`` to build void regions, and appends persistent
    keepout rule areas (``copperpour not_allowed``) on the plane layers so the
    inner pours void around the mains nets.  Optionally refills afterwards.
    """
    from kicad_tools._shapely import has_shapely
    from kicad_tools.creepage.engine import resolve_hv_nets
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.sexp import parse_file
    from kicad_tools.zones.hv_keepout import build_hv_keepout_plan

    quiet = getattr(args, "quiet", False)

    if not has_shapely():
        print(
            "Error: hv-keepout requires shapely (a core dependency); "
            "it is not importable in this environment.",
            file=sys.stderr,
        )
        return 1

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if args.clearance <= 0:
        print(
            f"Error: --clearance must be positive (got {args.clearance}).",
            file=sys.stderr,
        )
        return 1

    net_class_map, ncm_error = _load_net_class_map(getattr(args, "net_class_map", None))
    if ncm_error is not None:
        print(f"Error: {ncm_error}", file=sys.stderr)
        return 1

    # No -o means overwrite the input in place, consistent with 'zones fill'.
    output_path = Path(args.output) if args.output else pcb_path

    plane_layers = None
    if getattr(args, "plane_layers", None):
        plane_layers = [layer.strip() for layer in args.plane_layers.split(",") if layer.strip()]

    if not quiet:
        print(f"Loading PCB: {pcb_path}")

    try:
        pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    net_class = getattr(args, "net_class", "HV") or "HV"
    hv_nets = resolve_hv_nets(pcb, net_class, net_class_map)

    if not hv_nets:
        # Clean no-op mirroring `kct creepage`'s guidance (issue #4372 edge a).
        print(
            f"No '{net_class}' nets found "
            "(supply --net-class-map to classify HV nets, or check --net-class)."
        )
        print("No keepouts generated -- nothing to void.")
        return 0

    plan = build_hv_keepout_plan(
        pcb,
        hv_nets,
        clearance_mm=args.clearance,
        plane_layers=plane_layers,
    )

    if not quiet or args.dry_run:
        print(f"\nHV nets ({len(plan.hv_nets)}): {', '.join(sorted(plan.hv_nets.values()))}")
        print(f"Clearance: {plan.clearance_mm} mm")
        print(
            "Target plane layers: "
            f"{', '.join(plan.plane_layers) if plan.plane_layers else '(none)'}"
        )
        if plan.excluded_layers:
            print(f"Excluded (HV net has its own pour there): {', '.join(plan.excluded_layers)}")
        print(f"Planned keepout voids: {plan.keepout_count}")
        if args.verbose:
            for i, void in enumerate(plan.voids, 1):
                print(f"  Void {i}: {len(void.points)} pts on {', '.join(void.layers)}")

    if plan.keepout_count == 0:
        if not plan.plane_layers:
            print(
                "No target plane layers to void (supply --plane-layers, or add plane pours first)."
            )
        else:
            print("No HV copper found on the board -- no keepouts generated.")
        return 0

    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    # Append the keepout zones to the parsed document and save.
    try:
        doc = parse_file(pcb_path)
    except Exception as e:
        print(f"Error parsing PCB for write: {e}", file=sys.stderr)
        return 1

    for node in plan.zone_nodes():
        doc.append(node)

    if not quiet:
        print(f"\nSaving to: {output_path}")

    try:
        from kicad_tools.core.sexp_file import save_pcb

        save_pcb(doc, output_path)
    except Exception as e:
        print(f"Error: Write failed: {e}", file=sys.stderr)
        return 1

    if not quiet:
        print(f"Wrote {plan.keepout_count} keepout zone(s).")

    if args.refill:
        rc = _refill_after_keepout(output_path, quiet)
        if rc != 0:
            return rc

    if not quiet:
        print("Done!")

    return 0


def _refill_after_keepout(path: Path, quiet: bool) -> int:
    """Run kicad-cli refill so the new pour-keepouts take effect."""
    from .runner import find_kicad_cli, run_fill_zones

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print(
            "Error: --refill requested but kicad-cli not found. "
            "Install KiCad from https://www.kicad.org/download/",
            file=sys.stderr,
        )
        return 1

    if not quiet:
        print(f"Refilling zones in: {path}")

    result = run_fill_zones(path, None, kicad_cli=kicad_cli)
    if not result.success:
        print(f"Error refilling zones: {result.stderr}", file=sys.stderr)
        return 1

    if not quiet:
        print("Zones refilled.")
    return 0


def _run_fill(args) -> int:
    """Fill zones in a PCB using kicad-cli."""
    from .runner import find_kicad_cli, run_fill_zones

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print(
            "Error: kicad-cli not found. Install KiCad 8 from https://www.kicad.org/download/",
            file=sys.stderr,
        )
        return 1

    output_path = Path(args.output) if args.output else None

    quiet = getattr(args, "quiet", False)

    if args.dry_run:
        print(f"Would fill zones in: {pcb_path}")
        if args.net:
            print(f"  Net filter: {args.net}")
        if output_path:
            print(f"  Output: {output_path}")
        else:
            print(f"  Output: {pcb_path} (in-place)")
        return 0

    if args.net:
        # kicad-cli fill-zones does not support per-net filtering.
        # Document the limitation and fill all zones.
        if not quiet:
            print(
                f"Note: --net filter is not supported by kicad-cli. "
                f"All zones will be filled (requested net: {args.net}).",
            )

    if not quiet:
        print(f"Filling zones in: {pcb_path}")

    result = run_fill_zones(pcb_path, output_path, kicad_cli=kicad_cli)

    if not result.success:
        print(f"Error: {result.stderr}", file=sys.stderr)
        return 1

    if not quiet:
        target = output_path if output_path else pcb_path
        print(f"Zones filled: {target}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
