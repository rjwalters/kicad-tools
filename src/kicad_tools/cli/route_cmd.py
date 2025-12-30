"""
PCB autorouting CLI command.

Provides command-line access to the autorouter:

    kicad-tools route board.kicad_pcb
    kicad-tools route board.kicad_pcb -o board_routed.kicad_pcb
    kicad-tools route board.kicad_pcb --skip-nets GND,VCC --strategy negotiated
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point for route command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools route",
        description="Autoroute a KiCad PCB file",
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "-o", "--output",
        help="Output file path (default: <input>_routed.kicad_pcb)",
    )
    parser.add_argument(
        "--strategy",
        choices=["basic", "negotiated", "monte-carlo"],
        default="negotiated",
        help="Routing strategy (default: negotiated)",
    )
    parser.add_argument(
        "--skip-nets",
        help="Comma-separated nets to skip (e.g., GND,VCC,VBUS)",
    )
    parser.add_argument(
        "--grid",
        type=float,
        default=0.25,
        help="Grid resolution in mm (default: 0.25, use 0.1 for dense QFP)",
    )
    parser.add_argument(
        "--trace-width",
        type=float,
        default=0.2,
        help="Trace width in mm (default: 0.2)",
    )
    parser.add_argument(
        "--clearance",
        type=float,
        default=0.15,
        help="Trace clearance in mm (default: 0.15)",
    )
    parser.add_argument(
        "--via-drill",
        type=float,
        default=0.3,
        help="Via drill size in mm (default: 0.3)",
    )
    parser.add_argument(
        "--via-diameter",
        type=float,
        default=0.6,
        help="Via pad diameter in mm (default: 0.6)",
    )
    parser.add_argument(
        "--mc-trials",
        type=int,
        default=10,
        help="Number of Monte Carlo trials (default: 10)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=15,
        help="Max iterations for negotiated routing (default: 15)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing output",
    )

    args = parser.parse_args(argv)

    # Validate input
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if not pcb_path.suffix == ".kicad_pcb":
        print(f"Warning: Expected .kicad_pcb file, got {pcb_path.suffix}")

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_routed")

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Import router modules
    from kicad_tools.router import DesignRules, load_pcb_for_routing

    # Configure design rules
    rules = DesignRules(
        grid_resolution=args.grid,
        trace_width=args.trace_width,
        trace_clearance=args.clearance,
        via_drill=args.via_drill,
        via_diameter=args.via_diameter,
    )

    # Print header
    print("=" * 60)
    print("KiCad PCB Autorouter")
    print("=" * 60)
    print(f"Input:    {pcb_path}")
    print(f"Output:   {output_path}")
    print(f"Strategy: {args.strategy}")
    if skip_nets:
        print(f"Skip:     {', '.join(skip_nets)}")

    if args.verbose:
        print(f"\nDesign Rules:")
        print(f"  Grid resolution: {rules.grid_resolution}mm")
        print(f"  Trace width:     {rules.trace_width}mm")
        print(f"  Clearance:       {rules.trace_clearance}mm")
        print(f"  Via drill:       {rules.via_drill}mm")
        print(f"  Via diameter:    {rules.via_diameter}mm")

    # Load PCB
    print("\n--- Loading PCB ---")
    try:
        router, net_map = load_pcb_for_routing(
            str(pcb_path),
            skip_nets=skip_nets,
            rules=rules,
        )
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
    print(f"  Total nets: {len(net_map)}")

    nets_to_route = len([n for n in router.nets if n > 0])
    print(f"  Nets to route: {nets_to_route}")

    if args.verbose:
        print("\n  Net breakdown:")
        for net_name, net_num in sorted(net_map.items(), key=lambda x: x[1]):
            if net_name and net_name not in skip_nets:
                pad_count = len(router.nets.get(net_num, []))
                print(f"    {net_name}: {pad_count} pads")

    # Route
    print(f"\n--- Routing ({args.strategy}) ---")
    try:
        if args.strategy == "basic":
            routes = router.route_all()
        elif args.strategy == "negotiated":
            routes = router.route_all_negotiated(max_iterations=args.iterations)
        elif args.strategy == "monte-carlo":
            routes = router.route_all_monte_carlo(
                num_trials=args.mc_trials,
                verbose=args.verbose,
            )
    except Exception as e:
        print(f"Error during routing: {e}", file=sys.stderr)
        return 1

    # Get statistics
    stats = router.get_statistics()

    print("\n--- Results ---")
    print(f"  Routes created:  {stats['routes']}")
    print(f"  Segments:        {stats['segments']}")
    print(f"  Vias:            {stats['vias']}")
    print(f"  Total length:    {stats['total_length_mm']:.2f}mm")
    print(f"  Nets routed:     {stats['nets_routed']}/{nets_to_route}")

    # Save output
    if args.dry_run:
        print("\n--- Dry run - not saving ---")
    else:
        print("\n--- Saving routed PCB ---")

        # Read original PCB content
        original_content = pcb_path.read_text()

        # Get route S-expressions
        route_sexp = router.to_sexp()

        # Insert routes before final closing parenthesis
        if route_sexp:
            output_content = original_content.rstrip().rstrip(")")
            output_content += "\n  ; === AUTOROUTED TRACES ===\n"
            output_content += f"  {route_sexp}\n"
            output_content += ")\n"
        else:
            output_content = original_content
            print("  Warning: No routes generated!")

        output_path.write_text(output_content)
        print(f"  Saved to: {output_path}")

    # Summary
    print("\n" + "=" * 60)
    if stats['nets_routed'] == nets_to_route:
        print("SUCCESS: All nets routed!")
        return 0
    else:
        print(f"PARTIAL: Routed {stats['nets_routed']}/{nets_to_route} nets")
        print("  Some nets may require manual routing or a different strategy.")
        return 1 if stats['nets_routed'] < nets_to_route else 0


if __name__ == "__main__":
    sys.exit(main())
