"""
PCB autorouting CLI command.

Provides command-line access to the autorouter:

    kicad-tools route board.kicad_pcb
    kicad-tools route board.kicad_pcb -o board_routed.kicad_pcb
    kicad-tools route board.kicad_pcb --skip-nets GND,VCC --strategy negotiated
"""

import argparse
import math
import sys
from pathlib import Path


def show_preview(router, net_map: dict[str, int], nets_to_route: int, quiet: bool = False) -> str:
    """Display routing preview with per-net breakdown.

    Args:
        router: The Autorouter instance with completed routes
        net_map: Mapping of net names to net IDs
        nets_to_route: Total number of nets expected to be routed
        quiet: If True, skip interactive prompt and return 'n'

    Returns:
        User response: 'y' (apply), 'n' (reject), or 'e' (edit - future)
    """
    # Build reverse mapping: net_id -> net_name
    reverse_net = {v: k for k, v in net_map.items()}

    # Collect per-net statistics
    net_stats: dict[int, dict] = {}
    for route in router.routes:
        net_id = route.net
        if net_id not in net_stats:
            net_stats[net_id] = {
                "net_name": route.net_name or reverse_net.get(net_id, f"Net {net_id}"),
                "segments": 0,
                "vias": 0,
                "length": 0.0,
                "layers": set(),
            }
        stats = net_stats[net_id]
        stats["segments"] += len(route.segments)
        stats["vias"] += len(route.vias)
        for seg in route.segments:
            dx = seg.x2 - seg.x1
            dy = seg.y2 - seg.y1
            stats["length"] += math.sqrt(dx * dx + dy * dy)
            stats["layers"].add(seg.layer.kicad_name)

    # Identify unrouted nets
    routed_net_ids = set(net_stats.keys())
    all_net_ids = {v for k, v in net_map.items() if v > 0}
    unrouted_ids = all_net_ids - routed_net_ids

    # Print header
    print("\n" + "=" * 60)
    print("ROUTING PREVIEW")
    print("=" * 60)

    # Print per-net breakdown
    for net_id in sorted(net_stats.keys()):
        stats = net_stats[net_id]
        net_name = stats["net_name"]
        layers = " -> ".join(sorted(stats["layers"]))
        via_info = f", {stats['vias']} via(s)" if stats["vias"] > 0 else ""

        print(f"\nNet: {net_name}")
        print(f"  Layers:   {layers}")
        print(f"  Length:   {stats['length']:.2f}mm")
        print(f"  Segments: {stats['segments']}{via_info}")
        print("  Status:   \u2713 Routed")

    # Show unrouted nets
    if unrouted_ids:
        print("\n" + "-" * 40)
        for net_id in sorted(unrouted_ids):
            net_name = reverse_net.get(net_id, f"Net {net_id}")
            if net_name:  # Skip empty net names
                print(f"\nNet: {net_name}")
                print("  Status:   \u2717 No path found")

    # Summary statistics
    overall_stats = router.get_statistics()
    nets_routed = overall_stats["nets_routed"]
    success_rate = (nets_routed / nets_to_route * 100) if nets_to_route > 0 else 0

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Nets routed:  {nets_routed}/{nets_to_route} ({success_rate:.0f}%)")
    print(f"  Total length: {overall_stats['total_length_mm']:.2f}mm")
    print(f"  Total vias:   {overall_stats['vias']}")
    print(f"  Segments:     {overall_stats['segments']}")

    # Layer usage summary
    all_layers: dict[str, int] = {}
    for route in router.routes:
        for seg in route.segments:
            layer_name = seg.layer.kicad_name
            all_layers[layer_name] = all_layers.get(layer_name, 0) + 1

    if all_layers:
        print("\n  Layer usage:")
        for layer_name, count in sorted(all_layers.items()):
            print(f"    {layer_name}: {count} segments")

    print("=" * 60)

    # Interactive prompt (unless quiet mode)
    if quiet:
        return "n"

    print("\nApply routes? [y/N/e(dit)]:", end=" ")
    try:
        response = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return "n"

    if response in ("y", "yes"):
        return "y"
    elif response in ("e", "edit"):
        print("  (Edit mode not yet implemented - treating as reject)")
        return "n"
    else:
        return "n"


def main(argv: list[str] | None = None) -> int:
    """Main entry point for route command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools route",
        description="Autoroute a KiCad PCB file",
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "-o",
        "--output",
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
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show routing preview with per-net details before saving (interactive)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing output",
    )
    parser.add_argument(
        "--bus-routing",
        action="store_true",
        help="Enable bus-aware routing (routes bus signals together)",
    )
    parser.add_argument(
        "--bus-mode",
        choices=["parallel", "stacked", "bundled"],
        default="parallel",
        help="Bus routing mode (default: parallel)",
    )
    parser.add_argument(
        "--bus-spacing",
        type=float,
        help="Spacing between bus signals in mm (default: trace_width + clearance)",
    )
    parser.add_argument(
        "--bus-min-width",
        type=int,
        default=2,
        help="Minimum signals to form a bus group (default: 2)",
    )
    parser.add_argument(
        "--differential-pairs",
        action="store_true",
        help="Enable differential pair routing (routes paired signals together)",
    )
    parser.add_argument(
        "--diffpair-spacing",
        type=float,
        help="Spacing between differential pair traces in mm (default: auto based on type)",
    )
    parser.add_argument(
        "--diffpair-max-delta",
        type=float,
        help="Maximum length mismatch for differential pairs in mm (default: auto based on type)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output (for scripting)",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Show progress bar during routing",
    )
    parser.add_argument(
        "--progress-json",
        action="store_true",
        help="Output JSON progress events (for agent/IDE integration)",
    )
    parser.add_argument(
        "--power-nets",
        help=(
            "Generate copper zones for power nets: 'NET1:LAYER1,NET2:LAYER2,...' "
            "(e.g., 'GND:B.Cu,+3.3V:F.Cu')"
        ),
    )

    args = parser.parse_args(argv)

    # Validate input
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
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
    from kicad_tools.router import (
        BusRoutingConfig,
        BusRoutingMode,
        DesignRules,
        DifferentialPairConfig,
        load_pcb_for_routing,
    )

    # Configure design rules
    rules = DesignRules(
        grid_resolution=args.grid,
        trace_width=args.trace_width,
        trace_clearance=args.clearance,
        via_drill=args.via_drill,
        via_diameter=args.via_diameter,
    )

    # Import progress helpers
    from kicad_tools.cli.progress import spinner

    # Import progress callback support
    from kicad_tools.progress import ProgressCallback, create_json_callback

    quiet = args.quiet

    # Build progress callback based on flags
    progress_callback: ProgressCallback | None = None
    if args.progress_json:
        progress_callback = create_json_callback()
    elif args.progress:
        # Will create Rich progress below
        pass

    # Print header (unless quiet)
    if not quiet:
        print("=" * 60)
        print("KiCad PCB Autorouter")
        print("=" * 60)
        print(f"Input:    {pcb_path}")
        print(f"Output:   {output_path}")
        print(f"Strategy: {args.strategy}")
        if skip_nets:
            print(f"Skip:     {', '.join(skip_nets)}")
        if args.bus_routing:
            print(f"Bus:      enabled ({args.bus_mode} mode)")
        if args.differential_pairs:
            print("DiffPair: enabled")

        if args.verbose:
            print("\nDesign Rules:")
            print(f"  Grid resolution: {rules.grid_resolution}mm")
            print(f"  Trace width:     {rules.trace_width}mm")
            print(f"  Clearance:       {rules.trace_clearance}mm")
            print(f"  Via drill:       {rules.via_drill}mm")
            print(f"  Via diameter:    {rules.via_diameter}mm")

    # Load PCB
    if not quiet:
        print("\n--- Loading PCB ---")
    try:
        with spinner("Loading PCB...", quiet=quiet):
            router, net_map = load_pcb_for_routing(
                str(pcb_path),
                skip_nets=skip_nets,
                rules=rules,
            )
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    nets_to_route = len([n for n in router.nets if n > 0])

    if not quiet:
        print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
        print(f"  Total nets: {len(net_map)}")
        print(f"  Nets to route: {nets_to_route}")

        if args.verbose:
            print("\n  Net breakdown:")
            for net_name, net_num in sorted(net_map.items(), key=lambda x: x[1]):
                if net_name and net_name not in skip_nets:
                    pad_count = len(router.nets.get(net_num, []))
                    print(f"    {net_name}: {pad_count} pads")

    # Configure bus routing if enabled
    bus_config = None
    if args.bus_routing:
        bus_mode_map = {
            "parallel": BusRoutingMode.PARALLEL,
            "stacked": BusRoutingMode.STACKED,
            "bundled": BusRoutingMode.BUNDLED,
        }
        bus_config = BusRoutingConfig(
            enabled=True,
            mode=bus_mode_map[args.bus_mode],
            spacing=args.bus_spacing,
            min_bus_width=args.bus_min_width,
        )

        # Show detected buses
        if args.verbose and not quiet:
            analysis = router.get_bus_analysis()
            if analysis["total_groups"] > 0:
                print(f"\n  Detected {analysis['total_groups']} bus groups:")
                for group in analysis["groups"]:
                    status = "complete" if group["complete"] else "partial"
                    print(f"    - {group['name']}: {group['width']} bits ({status})")
            else:
                print("\n  No bus signals detected")

    # Configure differential pair routing if enabled
    diffpair_config = None
    diffpair_warnings = []
    if args.differential_pairs:
        diffpair_config = DifferentialPairConfig(
            enabled=True,
            spacing=args.diffpair_spacing,
            max_length_delta=args.diffpair_max_delta,
        )

        # Show detected differential pairs
        if args.verbose and not quiet:
            analysis = router.analyze_differential_pairs()
            if analysis["total_pairs"] > 0:
                print(f"\n  Detected {analysis['total_pairs']} differential pairs:")
                for pair in analysis["pairs"]:
                    print(
                        f"    - {pair['name']}: {pair['type']} "
                        f"(spacing={pair['spacing']}mm, max_delta={pair['max_delta']}mm)"
                    )
                if analysis["unpaired"]:
                    print(f"\n  Unpaired differential signals: {analysis['unpaired_signals']}")
                    for sig in analysis["unpaired"]:
                        print(f"    - {sig['net_name']} ({sig['polarity']})")
            else:
                print("\n  No differential pairs detected")

    # Route
    if not quiet:
        print(f"\n--- Routing ({args.strategy}) ---")

    # Create Rich progress callback if --progress flag used
    if args.progress and not args.progress_json:
        from kicad_tools.cli.progress import create_progress

        def make_rich_progress_callback(progress_bar, task_id):
            """Create callback that updates Rich progress bar."""

            def callback(prog: float, message: str, cancelable: bool) -> bool:
                if prog >= 0:
                    progress_bar.update(task_id, completed=int(prog * 100), description=message)
                else:
                    progress_bar.update(task_id, description=message)
                return True

            return callback

        try:
            with create_progress(quiet=quiet) as progress_bar:
                task_id = progress_bar.add_task(f"Routing {nets_to_route} nets...", total=100)
                progress_callback = make_rich_progress_callback(progress_bar, task_id)

                if args.differential_pairs and args.strategy == "basic":
                    _, diffpair_warnings = router.route_all_with_diffpairs(diffpair_config)
                elif args.bus_routing and args.strategy == "basic":
                    _ = router.route_all_with_buses(bus_config)
                elif args.strategy == "basic":
                    _ = router.route_all(progress_callback=progress_callback)
                elif args.strategy == "negotiated":
                    _ = router.route_all_negotiated(
                        max_iterations=args.iterations,
                        progress_callback=progress_callback,
                    )
                elif args.strategy == "monte-carlo":
                    _ = router.route_all_monte_carlo(
                        num_trials=args.mc_trials,
                        verbose=args.verbose and not quiet,
                        progress_callback=progress_callback,
                    )
        except Exception as e:
            print(f"Error during routing: {e}", file=sys.stderr)
            return 1
    else:
        try:
            with spinner(f"Routing {nets_to_route} nets...", quiet=quiet or args.progress_json):
                if args.differential_pairs and args.strategy == "basic":
                    _, diffpair_warnings = router.route_all_with_diffpairs(diffpair_config)
                elif args.bus_routing and args.strategy == "basic":
                    _ = router.route_all_with_buses(bus_config)
                elif args.strategy == "basic":
                    _ = router.route_all(progress_callback=progress_callback)
                elif args.strategy == "negotiated":
                    _ = router.route_all_negotiated(
                        max_iterations=args.iterations,
                        progress_callback=progress_callback,
                    )
                elif args.strategy == "monte-carlo":
                    _ = router.route_all_monte_carlo(
                        num_trials=args.mc_trials,
                        verbose=args.verbose and not quiet,
                        progress_callback=progress_callback,
                    )
        except Exception as e:
            print(f"Error during routing: {e}", file=sys.stderr)
            return 1

    # Get statistics
    stats = router.get_statistics()

    if not quiet:
        print("\n--- Results ---")
        print(f"  Routes created:  {stats['routes']}")
        print(f"  Segments:        {stats['segments']}")
        print(f"  Vias:            {stats['vias']}")
        print(f"  Total length:    {stats['total_length_mm']:.2f}mm")
        print(f"  Nets routed:     {stats['nets_routed']}/{nets_to_route}")

    # Report differential pair length mismatch warnings
    if diffpair_warnings and not quiet:
        print(f"\n--- Differential Pair Warnings ({len(diffpair_warnings)}) ---")
        for warning in diffpair_warnings:
            print(f"  {warning}")

    # Show preview if requested
    if args.preview:
        response = show_preview(router, net_map, nets_to_route, quiet=quiet)
        if response != "y":
            if not quiet:
                print("\nRouting cancelled. No changes saved.")
            return 0

    # Generate power zones if requested
    zone_sexp = ""
    if args.power_nets:
        from kicad_tools.zones import ZoneGenerator, parse_power_nets

        try:
            power_nets = parse_power_nets(args.power_nets)
        except ValueError as e:
            print(f"Error parsing power-nets: {e}", file=sys.stderr)
            return 1

        if power_nets and not quiet:
            print("\n--- Generating copper zones ---")
            print(f"  Power nets: {', '.join(f'{n}:{l}' for n, l in power_nets)}")

        if power_nets:
            try:
                gen = ZoneGenerator.from_pcb(str(pcb_path))
                for net_name, layer in power_nets:
                    # GND gets higher priority (fills last, on top)
                    priority = 1 if net_name.upper() in ("GND", "GNDA", "GNDD") else 0
                    try:
                        gen.add_zone(
                            net=net_name,
                            layer=layer,
                            priority=priority,
                        )
                        if not quiet:
                            print(f"    Added zone: {net_name} on {layer} (priority {priority})")
                    except ValueError as e:
                        print(f"  Warning: Could not add zone for {net_name}: {e}")

                zone_sexp = gen.generate_sexp()
            except Exception as e:
                print(f"  Warning: Zone generation failed: {e}")

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
    else:
        if not quiet:
            print("\n--- Saving routed PCB ---")

        with spinner("Saving routed PCB...", quiet=quiet):
            # Read original PCB content
            original_content = pcb_path.read_text()

            # Get route S-expressions
            route_sexp = router.to_sexp()

            # Insert routes and zones before final closing parenthesis
            if route_sexp or zone_sexp:
                output_content = original_content.rstrip().rstrip(")")
                if zone_sexp:
                    output_content += "\n  ; === COPPER ZONES ===\n"
                    output_content += f"  {zone_sexp}\n"
                if route_sexp:
                    output_content += "\n  ; === AUTOROUTED TRACES ===\n"
                    output_content += f"  {route_sexp}\n"
                output_content += ")\n"
            else:
                output_content = original_content
                if not quiet:
                    print("  Warning: No routes generated!")

            output_path.write_text(output_content)

        if not quiet:
            print(f"  Saved to: {output_path}")

    # Summary
    if not quiet:
        print("\n" + "=" * 60)
        if stats["nets_routed"] == nets_to_route:
            print("SUCCESS: All nets routed!")
        else:
            print(f"PARTIAL: Routed {stats['nets_routed']}/{nets_to_route} nets")
            print("  Some nets may require manual routing or a different strategy.")

    if stats["nets_routed"] == nets_to_route:
        return 0
    else:
        return 1 if stats["nets_routed"] < nets_to_route else 0


if __name__ == "__main__":
    sys.exit(main())
