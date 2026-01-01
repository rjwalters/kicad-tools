"""
CLI command for zone generation.

Provides commands for adding copper pour zones to PCB files:
    kicad-tools zones add board.kicad_pcb --net GND --layer B.Cu
    kicad-tools zones list board.kicad_pcb
"""

import argparse
import sys
from pathlib import Path


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
        help="Output file path (default: <input>_zones.kicad_pcb)",
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
        help="Output file path",
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

    return 0


def _run_add(args) -> int:
    """Add a single zone to a PCB."""
    from kicad_tools.zones import ZoneGenerator

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_zones")

    quiet = args.quiet

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

    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
            print("\nGenerated S-expression:")
            print(gen.generate_sexp())
        return 0

    # Save
    if not quiet:
        print(f"\nSaving to: {output_path}")

    gen.save(output_path)

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
                    "boundary_points": len(zone.polygon),
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
    """Add multiple zones from a power-nets specification."""
    from kicad_tools.zones import ZoneGenerator, parse_power_nets

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

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_zones")

    quiet = args.quiet

    if not quiet:
        print(f"Loading PCB: {pcb_path}")

    try:
        gen = ZoneGenerator.from_pcb(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Add zones with priorities (GND gets highest priority)
    if not quiet:
        print(f"\nAdding {len(power_nets)} zone(s):")

    errors = []
    for net_name, layer in power_nets:
        # GND typically gets higher priority (fills last, on top)
        priority = 1 if net_name.upper() in ("GND", "GNDA", "GNDD") else 0

        try:
            gen.add_zone(
                net=net_name,
                layer=layer,
                priority=priority,
                clearance=args.clearance,
            )
            if not quiet:
                print(f"  {net_name} on {layer} (priority {priority})")
        except ValueError as e:
            errors.append(f"  {net_name}: {e}")

    if errors:
        print("\nErrors:", file=sys.stderr)
        for err in errors:
            print(err, file=sys.stderr)

    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
            if args.verbose:
                print("\nGenerated S-expression:")
                print(gen.generate_sexp())
        return 0 if not errors else 1

    # Save
    if not quiet:
        print(f"\nSaving to: {output_path}")

    gen.save(output_path)

    if not quiet:
        stats = gen.get_statistics()
        print(f"\nCreated {stats['zone_count']} zone(s)")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
