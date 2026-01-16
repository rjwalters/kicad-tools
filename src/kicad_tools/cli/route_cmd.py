"""
PCB autorouting CLI command.

Provides command-line access to the autorouter:

    kicad-tools route board.kicad_pcb
    kicad-tools route board.kicad_pcb -o board_routed.kicad_pcb
    kicad-tools route board.kicad_pcb --skip-nets GND,VCC --strategy negotiated

Performance Profiling:

    Use --profile to measure routing performance and identify bottlenecks:

    # Profile routing and save results
    kicad-tools route board.kicad_pcb --profile

    # Specify custom output file
    kicad-tools route board.kicad_pcb --profile --profile-output my_profile.prof

    # Analyze results with pstats
    python -m pstats route_profile.prof

    # Visualize with snakeviz (pip install snakeviz)
    snakeviz route_profile.prof

Layer Stack Configuration:

    By default, the autorouter uses a 2-layer configuration (F.Cu, B.Cu).
    For multi-layer boards, use the --layers option:

    # 4-layer board with GND/PWR planes (typical for Pi HAT, Arduino shields)
    kicad-tools route board.kicad_pcb --layers 4

    # 4-layer with 2 signal layers (for high-density routing)
    kicad-tools route board.kicad_pcb --layers 4-sig

    # 6-layer with 4 signal layers
    kicad-tools route board.kicad_pcb --layers 6

    Layer stack configurations:
    - '2': F.Cu (signal), B.Cu (signal)
    - '4': F.Cu (signal), In1.Cu (GND plane), In2.Cu (PWR plane), B.Cu (signal)
    - '4-sig': F.Cu (signal), In1.Cu (signal), In2.Cu (GND plane), B.Cu (mixed)
    - '6': F.Cu, In1.Cu (GND), In2.Cu (signal), In3.Cu (signal), In4.Cu (PWR), B.Cu

    For 4-layer boards with inner planes (--layers 4), signals are routed on
    the outer layers (F.Cu and B.Cu) with vias providing layer transitions
    through the planes. This is the most common configuration for hobby/small
    production boards.
"""

import argparse
import math
import signal
import sys
from pathlib import Path

# Global state for Ctrl+C handling
_interrupt_state = {
    "interrupted": False,
    "router": None,
    "output_path": None,
    "pcb_path": None,
    "quiet": False,
}


def _handle_interrupt(signum, frame):
    """Handle Ctrl+C by setting the interrupted flag and saving partial results."""
    _interrupt_state["interrupted"] = True
    if not _interrupt_state["quiet"]:
        print("\n\n⚠ Interrupt received! Saving partial results...")
    # Save partial results immediately
    saved = _save_partial_results()
    # Exit with code 2 to indicate interruption
    sys.exit(2 if saved else 130)  # 130 = 128 + SIGINT (2)


def _save_partial_results() -> bool:
    """Save partial routing results if interrupted.

    Returns:
        True if partial results were saved, False otherwise.
    """
    router = _interrupt_state["router"]
    output_path = _interrupt_state["output_path"]
    pcb_path = _interrupt_state["pcb_path"]
    quiet = _interrupt_state["quiet"]

    if router is None or output_path is None or pcb_path is None:
        return False

    if not router.routes:
        if not quiet:
            print("  No routes to save.")
        return False

    try:
        # Read original PCB content
        original_content = pcb_path.read_text()

        # Get partial route S-expressions
        route_sexp = router.to_sexp()

        if route_sexp:
            # Create partial output filename
            partial_path = output_path.with_stem(output_path.stem + "_partial")

            # Insert routes before final closing parenthesis
            output_content = original_content.rstrip().rstrip(")")
            output_content += f"\n  {route_sexp}\n"
            output_content += ")\n"

            partial_path.write_text(output_content)

            if not quiet:
                stats = router.get_statistics()
                print(f"\n  Partial results saved to: {partial_path}")
                print(f"    Nets routed: {stats['nets_routed']}")
                print(f"    Segments: {stats['segments']}")
                print(f"    Vias: {stats['vias']}")
            return True
    except Exception as e:
        if not quiet:
            print(f"  Error saving partial results: {e}")

    return False


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


def run_post_route_drc(
    output_path: Path,
    manufacturer: str,
    layers: int,
    quiet: bool = False,
) -> tuple[int, int]:
    """Run DRC validation on the routed PCB.

    Args:
        output_path: Path to the routed PCB file
        manufacturer: Manufacturer profile for DRC rules (e.g., "jlcpcb")
        layers: Number of PCB layers
        quiet: If True, suppress output

    Returns:
        Tuple of (error_count, warning_count)
    """
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    try:
        # Load the routed PCB
        pcb = PCB.load(str(output_path))

        # Run DRC
        checker = DRCChecker(pcb, manufacturer=manufacturer, layers=layers)
        results = checker.check_all()

        error_count = results.error_count
        warning_count = results.warning_count

        if not quiet:
            print("\n--- DRC Validation ---")
            if error_count == 0 and warning_count == 0:
                print(f"  DRC PASSED ({manufacturer} profile, {layers} layers)")
            else:
                if error_count > 0:
                    print(f"  Errors:   {error_count}")
                if warning_count > 0:
                    print(f"  Warnings: {warning_count}")

                # Show first few violations
                shown = 0
                for v in results.errors[:5]:
                    location = (
                        f" at ({v.location[0]:.2f}, {v.location[1]:.2f})" if v.location else ""
                    )
                    print(f"    - {v.rule_id}: {v.message}{location}")
                    shown += 1
                if error_count > 5:
                    print(f"    ... and {error_count - 5} more errors")

                if warning_count > 0 and shown < 5:
                    for v in results.warnings[: 5 - shown]:
                        location = (
                            f" at ({v.location[0]:.2f}, {v.location[1]:.2f})" if v.location else ""
                        )
                        print(f"    - {v.rule_id}: {v.message}{location}")
                    if warning_count > (5 - shown):
                        print(f"    ... and {warning_count - (5 - shown)} more warnings")

                print(f"\n  Run 'kct check {output_path} --mfr {manufacturer}' for full details")

        return error_count, warning_count

    except Exception as e:
        if not quiet:
            print("\n--- DRC Validation ---")
            print(f"  Warning: DRC check failed: {e}")
        return -1, -1  # Indicate failure to run DRC


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
        "--timeout",
        type=float,
        default=None,
        help="Timeout in seconds for routing (default: no timeout). Returns best partial result if reached.",
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
        "--analyze",
        action="store_true",
        help="Analyze routability before routing and show diagnostic report",
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
        "--power-nets",
        help=(
            "Generate copper zones for power nets: 'NET1:LAYER1,NET2:LAYER2,...' "
            "(e.g., 'GND:B.Cu,+3.3V:F.Cu')"
        ),
    )
    parser.add_argument(
        "--edge-clearance",
        type=float,
        help=(
            "Copper-to-edge clearance in mm. Blocks routing within this distance "
            "of the board edge. Common values: 0.25-0.5mm (default: no clearance)"
        ),
    )
    parser.add_argument(
        "--layers",
        choices=["auto", "2", "4", "4-sig", "6"],
        default="auto",
        help=(
            "Layer stack configuration for routing: "
            "'auto' = auto-detect from PCB file (default); "
            "'2' = 2-layer (F.Cu, B.Cu); "
            "'4' = 4-layer with GND/PWR planes (F.Cu, In1=GND, In2=PWR, B.Cu); "
            "'4-sig' = 4-layer with 2 signal layers (F.Cu, In1=signal, In2=GND, B.Cu); "
            "'6' = 6-layer with 4 signal layers. "
            "Auto-detection parses the PCB's layer definitions and zones to "
            "determine the appropriate layer stack."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Force routing even when grid resolution exceeds clearance. "
            "Without this flag, routing will fail if grid > clearance to "
            "prevent DRC violations. Use with caution."
        ),
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help=(
            "Enable profiling to measure performance. Outputs a cProfile "
            "stats file that can be analyzed with pstats or visualization tools."
        ),
    )
    parser.add_argument(
        "--profile-output",
        metavar="FILE",
        help=(
            "Output file for profile data (default: route_profile.prof). "
            "Analyze with: python -m pstats route_profile.prof, or "
            "visualize with: snakeviz route_profile.prof"
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "cpp", "python"],
        default="auto",
        help=(
            "Router backend to use: "
            "'auto' = use C++ if available, fall back to Python (default); "
            "'cpp' = require C++ backend (fails if not available); "
            "'python' = force Python backend (for testing/debugging). "
            "C++ backend provides 10-100x speedup for fine-grid routing."
        ),
    )
    parser.add_argument(
        "--skip-drc",
        action="store_true",
        help=(
            "Skip post-routing DRC validation. By default, the router runs "
            "a DRC check after routing and warns about violations. Use this "
            "flag for performance-critical use or when running separate validation."
        ),
    )
    parser.add_argument(
        "--manufacturer",
        "--mfr",
        default="jlcpcb",
        help=(
            "Manufacturer profile for DRC validation (default: jlcpcb). "
            "Determines minimum clearances, trace widths, and other design rules."
        ),
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help=(
            "Skip trace optimization after routing. By default, traces are "
            "optimized to merge collinear segments, eliminate zigzags, and "
            "convert corners to 45 degrees. Use this flag to keep raw "
            "grid-step segments for debugging."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        dest="no_optimize",
        help="Alias for --no-optimize (keep raw grid-step segments for debugging)",
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

    # Validate grid resolution vs clearance (prevents DRC violations)
    if args.grid > args.clearance:
        recommended_grid = args.clearance / 2
        if not args.force:
            print(
                f"Error: Grid resolution {args.grid}mm exceeds clearance {args.clearance}mm.\n"
                f"This WILL cause DRC violations.\n\n"
                f"Options:\n"
                f"  1. Use a finer grid: --grid {recommended_grid}\n"
                f"  2. Use --force to override (not recommended)\n",
                file=sys.stderr,
            )
            return 1
        else:
            # User forced, continue with warning
            print(
                f"Warning: Grid resolution {args.grid}mm exceeds clearance {args.clearance}mm.\n"
                f"Proceeding anyway due to --force flag. Expect DRC violations.",
                file=sys.stderr,
            )

    # Import router modules
    from kicad_tools.router import (
        BusRoutingConfig,
        BusRoutingMode,
        DesignRules,
        DifferentialPairConfig,
        LayerStack,
        RoutabilityAnalyzer,
        is_cpp_available,
        load_pcb_for_routing,
        show_routing_summary,
    )
    from kicad_tools.router.io import detect_layer_stack

    # Handle backend selection
    force_python = False
    if args.backend == "cpp":
        if not is_cpp_available():
            print(
                "Error: C++ backend requested but not available.\n"
                "Build the C++ extension or use --backend auto/python.\n"
                "See README for build instructions.",
                file=sys.stderr,
            )
            return 1
    elif args.backend == "python":
        force_python = True

    # Create layer stack from --layers argument (or auto-detect)
    if args.layers == "auto":
        # Auto-detect layer stack from PCB file
        pcb_text = pcb_path.read_text()
        layer_stack = detect_layer_stack(pcb_text)
    else:
        layer_stack_map = {
            "2": LayerStack.two_layer(),
            "4": LayerStack.four_layer_sig_gnd_pwr_sig(),
            "4-sig": LayerStack.four_layer_sig_sig_gnd_pwr(),
            "6": LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig(),
        }
        layer_stack = layer_stack_map[args.layers]

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

    quiet = args.quiet

    # Print header (unless quiet)
    if not quiet:
        print("=" * 60)
        print("KiCad PCB Autorouter")
        print("=" * 60)
        print(f"Input:    {pcb_path}")
        print(f"Output:   {output_path}")
        print(f"Strategy: {args.strategy}")
        print(f"Layers:   {layer_stack.name} ({layer_stack.num_layers} layers)")
        if skip_nets:
            print(f"Skip:     {', '.join(skip_nets)}")
        if args.bus_routing:
            print(f"Bus:      enabled ({args.bus_mode} mode)")
        if args.differential_pairs:
            print("DiffPair: enabled")

        if args.edge_clearance:
            print(f"Edge:     {args.edge_clearance}mm clearance")
        if args.verbose:
            print("\nDesign Rules:")
            print(f"  Grid resolution: {rules.grid_resolution}mm")
            print(f"  Trace width:     {rules.trace_width}mm")
            print(f"  Clearance:       {rules.trace_clearance}mm")
            print(f"  Via drill:       {rules.via_drill}mm")
            print(f"  Via diameter:    {rules.via_diameter}mm")
            if args.edge_clearance:
                print(f"  Edge clearance:  {args.edge_clearance}mm")

            print(f"\nLayer Stack ({layer_stack.name}):")
            signal_layers = [lyr.name for lyr in layer_stack.signal_layers]
            plane_layers = [f"{lyr.name} ({lyr.plane_net})" for lyr in layer_stack.plane_layers]
            print(f"  Signal layers:  {', '.join(signal_layers)}")
            if plane_layers:
                print(f"  Plane layers:   {', '.join(plane_layers)}")

    # Load PCB
    if not quiet:
        print("\n--- Loading PCB ---")
    try:
        with spinner("Loading PCB...", quiet=quiet):
            router, net_map = load_pcb_for_routing(
                str(pcb_path),
                skip_nets=skip_nets,
                rules=rules,
                edge_clearance=args.edge_clearance,
                layer_stack=layer_stack,
                force_python=force_python,
                validate_drc=not args.force,
                strict_drc=False,  # Only fail on hard constraint (grid > clearance)
            )
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Set up Ctrl+C handling to save partial results
    _interrupt_state["router"] = router
    _interrupt_state["output_path"] = output_path
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["interrupted"] = False
    signal.signal(signal.SIGINT, _handle_interrupt)

    # Count nets by category for accurate status reporting (Issue #812)
    # - Multi-pad nets: 2+ pads, need actual routing
    # - Single-pad nets: 1 pad, trivially complete (no routing needed)
    # - Power nets: skipped via skip_nets, handled by copper pours
    multi_pad_nets = []
    single_pad_nets = []
    for net_num, pads in router.nets.items():
        if net_num > 0:  # Skip net 0 (unconnected)
            if len(pads) >= 2:
                multi_pad_nets.append(net_num)
            elif len(pads) == 1:
                single_pad_nets.append(net_num)
    nets_to_route = len(multi_pad_nets)  # Only multi-pad nets need routing
    power_nets_skipped = len(skip_nets)

    if not quiet:
        print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
        backend_info = router.backend_info
        print(
            f"  Backend:    {backend_info['active']} (C++ available: {backend_info['available']})"
        )
        print(f"  Total nets: {len(net_map)}")
        print(f"  Nets to route: {nets_to_route} (multi-pad signal nets)")

        if args.verbose:
            print("\n  Net breakdown:")
            for net_name, net_num in sorted(net_map.items(), key=lambda x: x[1]):
                if net_name and net_name not in skip_nets:
                    pad_count = len(router.nets.get(net_num, []))
                    print(f"    {net_name}: {pad_count} pads")

    # Analyze routability if requested
    if args.analyze:
        if not quiet:
            print("\n--- Routability Analysis ---")
        try:
            analyzer = RoutabilityAnalyzer(router)
            report = analyzer.analyze()

            # Print analysis report
            print(f"\n{'=' * 60}")
            print("ROUTABILITY ANALYSIS")
            print(f"{'=' * 60}")
            print(
                f"Estimated completion: {report.estimated_success_rate * 100:.0f}% "
                f"({report.expected_routable}/{report.total_nets} nets)"
            )

            # Show layer utilization
            if report.layer_utilization:
                print("\nLayer Utilization:")
                for layer_name, util in report.layer_utilization.items():
                    bar = "#" * int(util * 20)
                    print(f"  {layer_name:10s}: [{bar:20s}] {util * 100:.0f}%")

            # Show problem nets
            if report.problem_nets:
                print(f"\nProblem Nets ({len(report.problem_nets)}):")
                for net_report in report.problem_nets[:10]:  # Show first 10
                    print(f"\n  {net_report.net_name} ({net_report.pad_count} pads):")
                    print(f"    Severity: {net_report.severity.name}")
                    print(f"    Difficulty: {net_report.difficulty_score:.0f}/100")
                    if net_report.blocking_obstacles:
                        print("    Blocked by:")
                        for obs in net_report.blocking_obstacles[:5]:
                            print(f"      - {obs}")
                    if net_report.alternatives:
                        print("    Alternatives:")
                        for alt in net_report.alternatives[:3]:
                            print(f"      {alt}")
                    if net_report.suggestions:
                        print("    Suggestions:")
                        for sug in net_report.suggestions:
                            print(f"      - {sug}")

            # Show recommendations
            if report.recommendations:
                print("\nRecommendations:")
                for i, rec in enumerate(report.recommendations, 1):
                    print(f"  {i}. {rec}")

            print(f"{'=' * 60}")

            # If just analyzing, exit here
            if args.dry_run:
                return 0

        except Exception as e:
            print(f"Warning: Analysis failed: {e}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()

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
        if args.timeout:
            print(f"  Timeout: {args.timeout}s")
        if args.profile:
            profile_output = args.profile_output or "route_profile.prof"
            print(f"  Profiling enabled: {profile_output}")

    # Define routing function for profiling
    def do_routing():
        nonlocal diffpair_warnings
        if args.strategy == "negotiated":
            return router.route_all_negotiated(
                max_iterations=args.iterations,
                timeout=args.timeout,
            )
        elif args.differential_pairs and args.strategy == "basic":
            result, diffpair_warnings = router.route_all_with_diffpairs(diffpair_config)
            return result
        elif args.bus_routing and args.strategy == "basic":
            return router.route_all_with_buses(bus_config)
        elif args.strategy == "basic":
            return router.route_all()
        elif args.strategy == "monte-carlo":
            return router.route_all_monte_carlo(
                num_trials=args.mc_trials,
                verbose=args.verbose and not quiet,
            )
        return None

    try:
        if args.profile:
            # Profile the routing operation
            import cProfile
            import pstats

            profile_output = args.profile_output or "route_profile.prof"
            profiler = cProfile.Profile()
            profiler.enable()
            try:
                _ = do_routing()
            finally:
                profiler.disable()
                # Save profile data
                profiler.dump_stats(profile_output)
                if not quiet:
                    print(f"\n  Profile saved to: {profile_output}")
                    # Print top 20 functions by cumulative time
                    print("\n--- Profile Summary (top 20 by cumulative time) ---")
                    stats = pstats.Stats(profiler)
                    stats.strip_dirs().sort_stats("cumulative").print_stats(20)
        else:
            # Normal routing without profiling
            if args.strategy == "negotiated":
                # Negotiated routing has its own progress output - don't use spinner
                _ = do_routing()
            else:
                with spinner(f"Routing {nets_to_route} nets...", quiet=quiet):
                    _ = do_routing()
    except KeyboardInterrupt:
        # Handle any KeyboardInterrupt that wasn't caught by signal handler
        _interrupt_state["interrupted"] = True
        if not quiet:
            print("\n\n⚠ Routing interrupted!")
    except Exception as e:
        print(f"Error during routing: {e}", file=sys.stderr)
        # Still try to save partial results on error
        if router.routes:
            _save_partial_results()
        return 1

    # Check if interrupted and save partial results
    if _interrupt_state["interrupted"]:
        _save_partial_results()
        return 2  # Exit code 2 indicates interruption with partial results saved

    # Optimize traces (unless --no-optimize/--raw flag is set)
    if not args.no_optimize and router.routes:
        from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer

        if not quiet:
            print("\n--- Optimizing traces ---")

        # Get pre-optimization statistics
        pre_segments = sum(len(r.segments) for r in router.routes)
        pre_vias = sum(len(r.vias) for r in router.routes)

        # Configure and run optimizer
        opt_config = OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        )
        optimizer = TraceOptimizer(config=opt_config)

        with spinner("Optimizing traces...", quiet=quiet):
            optimized_routes = []
            for route in router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            router.routes = optimized_routes

        # Get post-optimization statistics
        post_segments = sum(len(r.segments) for r in router.routes)
        post_vias = sum(len(r.vias) for r in router.routes)

        if not quiet:
            segment_reduction = (
                ((pre_segments - post_segments) / pre_segments * 100) if pre_segments > 0 else 0
            )
            via_reduction = ((pre_vias - post_vias) / pre_vias * 100) if pre_vias > 0 else 0
            print(f"  Segments: {pre_segments} -> {post_segments} ({-segment_reduction:+.1f}%)")
            if pre_vias > 0:
                print(f"  Vias:     {pre_vias} -> {post_vias} ({-via_reduction:+.1f}%)")

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
            # Note: KiCad's S-expression format doesn't support ; comments
            if route_sexp or zone_sexp:
                output_content = original_content.rstrip().rstrip(")")
                if zone_sexp:
                    output_content += f"\n  {zone_sexp}\n"
                if route_sexp:
                    output_content += f"\n  {route_sexp}\n"
                output_content += ")\n"
            else:
                output_content = original_content
                if not quiet:
                    print("  Warning: No routes generated!")

            output_path.write_text(output_content)

        if not quiet:
            print(f"  Saved to: {output_path}")

    # Run DRC validation unless skipped or dry-run
    drc_errors = 0
    drc_warnings = 0
    drc_ran = False

    if not args.dry_run and not args.skip_drc and stats["nets_routed"] > 0:
        drc_ran = True
        drc_errors, drc_warnings = run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=layer_stack.num_layers,
            quiet=quiet,
        )

    # Summary
    all_nets_routed = stats["nets_routed"] == nets_to_route
    drc_passed = drc_errors <= 0  # -1 means DRC failed to run, treat as passed

    # Build summary suffix for net breakdown (Issue #812)
    summary_parts = []
    if len(single_pad_nets) > 0:
        summary_parts.append(f"{len(single_pad_nets)} single-pad")
    if power_nets_skipped > 0:
        summary_parts.append(f"{power_nets_skipped} power skipped")
    summary_suffix = f" ({', '.join(summary_parts)})" if summary_parts else ""

    if not quiet:
        print("\n" + "=" * 60)
        if all_nets_routed and drc_passed:
            if drc_ran and drc_errors == 0:
                print(f"SUCCESS: All signal nets routed, DRC passed!{summary_suffix}")
            else:
                print(f"SUCCESS: All signal nets routed!{summary_suffix}")
                if not drc_ran and not args.skip_drc and not args.dry_run:
                    print("  Note: Run 'kct check' to validate before manufacturing")
        elif all_nets_routed and not drc_passed:
            print("ROUTING FAILED: DRC violations detected")
            print("=" * 60)
            print()
            print("Net Statistics:")
            print(f"  Multi-pad nets:  {nets_to_route}")
            print(f"  Nets connected:  {stats['nets_routed']} (topologically complete)")
            print("  Nets DRC-clean:  0 (manufacturing blocked)")
            if len(single_pad_nets) > 0 or power_nets_skipped > 0:
                print(f"  Also:{summary_suffix}")
            print()
            print("DRC Summary:")
            print(f"  Violations: {drc_errors}")
            print()
            print("The autorouter connected all nets but violated design rules.")
            print("This board cannot be manufactured without fixing DRC errors.")
            print()
            print("Suggestions:")
            print("  - Try Monte Carlo routing: kct route --trials 10")
            print("  - Increase board area")
            print("  - Reduce component density")
            print("  - Try 4-layer routing: kct route --layers 4")
            print()
            print(f"  Run 'kct check {output_path} --mfr {args.manufacturer}' for full details")
        else:
            print(
                f"PARTIAL: Routed {stats['nets_routed']}/{nets_to_route} signal nets{summary_suffix}"
            )
            if drc_ran and drc_errors > 0:
                print(f"  Additionally, {drc_errors} DRC violation(s) detected.")

            # Show comprehensive routing summary with successes, failures, and suggestions
            show_routing_summary(router, net_map, nets_to_route, quiet=quiet)

    # Exit codes:
    # 0 = All nets routed AND (DRC passed OR DRC not run)
    # 1 = Not all nets routed OR DRC errors detected
    if all_nets_routed and drc_passed:
        return 0
    elif not all_nets_routed:
        return 1
    else:
        # All nets routed but DRC failed
        return 1


if __name__ == "__main__":
    sys.exit(main())
