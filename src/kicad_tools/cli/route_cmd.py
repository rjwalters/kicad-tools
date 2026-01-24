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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.router import Autorouter, LayerStack

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


@dataclass
class LayerEscalationResult:
    """Result of a layer escalation routing attempt."""

    layer_count: int
    layer_stack: "LayerStack"
    router: "Autorouter"
    net_map: dict
    nets_routed: int
    nets_to_route: int
    completion: float
    success: bool


@dataclass
class RuleRelaxationResult:
    """Result of a rule relaxation routing attempt."""

    tier: int
    trace_width: float
    clearance: float
    via_drill: float
    via_diameter: float
    tier_description: str
    router: "Autorouter"
    net_map: dict
    nets_routed: int
    nets_to_route: int
    completion: float
    success: bool
    layer_count: int = 2  # May be set by layer escalation integration


def update_pcb_layer_stackup(pcb_content: str, target_layers: int) -> str:
    """Update PCB content to have the specified number of copper layers.

    Args:
        pcb_content: Original PCB file content
        target_layers: Target number of copper layers (2, 4, or 6)

    Returns:
        Updated PCB content with correct layer definitions
    """
    import re

    # Layer definitions for different stackups
    layer_defs = {
        2: [
            '(0 "F.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
        4: [
            '(0 "F.Cu" signal)',
            '(1 "In1.Cu" signal)',
            '(2 "In2.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
        6: [
            '(0 "F.Cu" signal)',
            '(1 "In1.Cu" signal)',
            '(2 "In2.Cu" signal)',
            '(3 "In3.Cu" signal)',
            '(4 "In4.Cu" signal)',
            '(31 "B.Cu" signal)',
        ],
    }

    if target_layers not in layer_defs:
        return pcb_content

    # Find and replace the layers section
    # KiCad format: (layers (0 "F.Cu" signal) ... )
    layers_pattern = re.compile(
        r'(\(layers\s+)([^)]*"(?:F\.Cu|B\.Cu|In\d+\.Cu)"[^)]*\s*)+(\))',
        re.DOTALL,
    )

    def replace_layers(match):
        # Build new layers content
        new_layers = "\n    ".join(layer_defs[target_layers])
        return f"(layers\n    {new_layers}\n  )"

    # Check if we need to update
    current_layers = pcb_content.count('.Cu" signal')
    if current_layers >= target_layers:
        return pcb_content

    # Try to find and replace the layers section
    new_content = layers_pattern.sub(replace_layers, pcb_content)

    # If the pattern didn't match, try a more permissive pattern
    if new_content == pcb_content:
        # Alternative pattern for different KiCad versions
        alt_pattern = re.compile(
            r'\(layers\s*\n(\s+\(\d+\s+"[^"]+"\s+\w+[^)]*\)\s*\n)+\s*\)',
            re.MULTILINE,
        )
        new_layers_content = "\n    ".join(layer_defs[target_layers])
        new_content = alt_pattern.sub(
            f"(layers\n    {new_layers_content}\n  )",
            pcb_content,
        )

    return new_content


def route_with_layer_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with automatic layer escalation.

    Tries routing at 2, 4, and 6 layers until success or max is reached.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        is_cpp_available,
        load_pcb_for_routing,
        show_routing_summary,
    )

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

    # Configure design rules
    rules = DesignRules(
        grid_resolution=args.grid,
        trace_width=args.trace_width,
        trace_clearance=args.clearance,
        via_drill=args.via_drill,
        via_diameter=args.via_diameter,
    )

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Layer stacks to try (in escalation order)
    layer_configs = [
        (2, LayerStack.two_layer()),
        (4, LayerStack.four_layer_sig_gnd_pwr_sig()),
        (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
    ]

    # Filter by max_layers
    layer_configs = [(n, s) for n, s in layer_configs if n <= args.max_layers]

    if not quiet:
        print("=" * 60)
        print("KiCad PCB Autorouter - Layer Escalation Mode")
        print("=" * 60)
        print(f"Input:          {pcb_path}")
        print(f"Output:         {output_path}")
        print(f"Strategy:       {args.strategy}")
        print(f"Max layers:     {args.max_layers}")
        print(f"Min completion: {args.min_completion * 100:.0f}%")
        if skip_nets:
            print(f"Skip:           {', '.join(skip_nets)}")
        print()

    best_result: LayerEscalationResult | None = None
    successful_result: LayerEscalationResult | None = None

    for attempt_num, (layer_count, layer_stack) in enumerate(layer_configs, 1):
        if not quiet:
            print("=" * 60)
            print(f"Attempt {attempt_num}: {layer_count} layers")
            print("=" * 60)

        # Load PCB with this layer stack
        try:
            with spinner(f"Loading PCB ({layer_count} layers)...", quiet=quiet):
                router, net_map = load_pcb_for_routing(
                    str(pcb_path),
                    skip_nets=skip_nets,
                    rules=rules,
                    edge_clearance=args.edge_clearance,
                    layer_stack=layer_stack,
                    force_python=force_python,
                    validate_drc=not args.force,
                    strict_drc=False,
                )
        except Exception as e:
            if not quiet:
                print(f"  Error loading PCB: {e}")
            continue

        # Count nets to route
        multi_pad_nets = [
            net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) >= 2
        ]
        nets_to_route = len(multi_pad_nets)

        if not quiet:
            print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
            print(f"  Nets to route: {nets_to_route}")

        # Route
        if not quiet:
            print(f"\n  Routing ({args.strategy})...")

        try:
            if args.strategy == "negotiated":
                router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=args.timeout,
                )
            elif args.strategy == "basic":
                router.route_all()
            elif args.strategy == "monte-carlo":
                router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
        except Exception as e:
            if not quiet:
                print(f"  Routing error: {e}")
            continue

        # Calculate completion
        stats = router.get_statistics()
        nets_routed = stats["nets_routed"]
        completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0

        # Create result
        result = LayerEscalationResult(
            layer_count=layer_count,
            layer_stack=layer_stack,
            router=router,
            net_map=net_map,
            nets_routed=nets_routed,
            nets_to_route=nets_to_route,
            completion=completion,
            success=completion >= args.min_completion,
        )

        # Track best result
        if best_result is None or completion > best_result.completion:
            best_result = result

        # Report attempt result
        status = "SUCCESS" if result.success else "INSUFFICIENT - escalating"
        if not quiet:
            print(f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)")
            print(f"  Status: {status}")

        # Check for success
        if result.success:
            successful_result = result
            break

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("LAYER ESCALATION SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Design routed successfully on {final_result.layer_count} layers "
                f"({final_result.completion * 100:.0f}% completion)"
            )
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result on {final_result.layer_count} layers "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"on any layer count (max: {args.max_layers})"
            )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
        return 1

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer

        if not quiet:
            print("\n--- Optimizing traces ---")

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
            for route in final_result.router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            final_result.router.routes = optimized_routes

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        # Read original PCB content
        original_content = pcb_path.read_text()

        # Update layer stackup if we escalated
        if final_result.layer_count > 2:
            original_content = update_pcb_layer_stackup(original_content, final_result.layer_count)

        # Get route S-expressions
        route_sexp = final_result.router.to_sexp()

        # Insert routes before final closing parenthesis
        if route_sexp:
            output_content = original_content.rstrip().rstrip(")")
            output_content += f"\n  {route_sexp}\n"
            output_content += ")\n"
        else:
            output_content = original_content
            if not quiet:
                print("  Warning: No routes generated!")

        # Update output filename to include layer count
        if final_result.layer_count > 2:
            output_path = output_path.with_stem(
                output_path.stem + f"_{final_result.layer_count}layer"
            )

        output_path.write_text(output_content)

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Layer count: {final_result.layer_count}")

    # Run DRC validation unless skipped
    if not args.skip_drc and final_result.nets_routed > 0:
        run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
        )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print(f"SUCCESS: Design requires minimum {final_result.layer_count} layers")
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"on {final_result.layer_count} layers"
            )
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
            )

    return 0 if final_result.success else 1


def route_with_rule_relaxation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with automatic design rule relaxation.

    Tries routing with progressively relaxed design rules (trace width,
    clearance) until success or manufacturer minimum limits are reached.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        get_relaxation_tiers,
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

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Get relaxation tiers
    tiers = get_relaxation_tiers(
        initial_trace_width=args.trace_width,
        initial_clearance=args.clearance,
        initial_via_drill=args.via_drill,
        initial_via_diameter=args.via_diameter,
        manufacturer=args.manufacturer,
        min_trace_floor=args.min_trace,
        min_clearance_floor=args.min_clearance_floor,
    )

    # Determine layer stack
    if args.layers == "auto":
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

    if not quiet:
        print("=" * 60)
        print("KiCad PCB Autorouter - Adaptive Rules Mode")
        print("=" * 60)
        print(f"Input:          {pcb_path}")
        print(f"Output:         {output_path}")
        print(f"Strategy:       {args.strategy}")
        print(f"Manufacturer:   {args.manufacturer}")
        print(f"Min completion: {args.min_completion * 100:.0f}%")
        print(f"Relaxation tiers: {len(tiers)}")
        if skip_nets:
            print(f"Skip:           {', '.join(skip_nets)}")
        print()

    best_result: RuleRelaxationResult | None = None
    successful_result: RuleRelaxationResult | None = None

    for tier in tiers:
        if not quiet:
            print("=" * 60)
            print(f"Attempt {tier.tier + 1}: {tier.description}")
            print(f"  trace={tier.trace_width:.3f}mm, clearance={tier.clearance:.3f}mm")
            print("=" * 60)

        # Configure design rules for this tier
        rules = DesignRules(
            grid_resolution=args.grid,
            trace_width=tier.trace_width,
            trace_clearance=tier.clearance,
            via_drill=tier.via_drill,
            via_diameter=tier.via_diameter,
        )

        # Load PCB
        try:
            with spinner(f"Loading PCB (tier {tier.tier})...", quiet=quiet):
                router, net_map = load_pcb_for_routing(
                    str(pcb_path),
                    skip_nets=skip_nets,
                    rules=rules,
                    edge_clearance=args.edge_clearance,
                    layer_stack=layer_stack,
                    force_python=force_python,
                    validate_drc=not args.force,
                    strict_drc=False,
                )
        except Exception as e:
            if not quiet:
                print(f"  Error loading PCB: {e}")
            continue

        # Count nets to route
        multi_pad_nets = [
            net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) >= 2
        ]
        nets_to_route = len(multi_pad_nets)

        if not quiet:
            print(f"  Board size: {router.grid.width}mm x {router.grid.height}mm")
            print(f"  Nets to route: {nets_to_route}")

        # Route
        if not quiet:
            print(f"\n  Routing ({args.strategy})...")

        try:
            if args.strategy == "negotiated":
                router.route_all_negotiated(
                    max_iterations=args.iterations,
                    timeout=args.timeout,
                )
            elif args.strategy == "basic":
                router.route_all()
            elif args.strategy == "monte-carlo":
                router.route_all_monte_carlo(
                    num_trials=args.mc_trials,
                    verbose=args.verbose and not quiet,
                )
        except Exception as e:
            if not quiet:
                print(f"  Routing error: {e}")
            continue

        # Calculate completion
        stats = router.get_statistics()
        nets_routed = stats["nets_routed"]
        completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0

        # Create result
        result = RuleRelaxationResult(
            tier=tier.tier,
            trace_width=tier.trace_width,
            clearance=tier.clearance,
            via_drill=tier.via_drill,
            via_diameter=tier.via_diameter,
            tier_description=tier.description,
            router=router,
            net_map=net_map,
            nets_routed=nets_routed,
            nets_to_route=nets_to_route,
            completion=completion,
            success=completion >= args.min_completion,
            layer_count=layer_stack.num_layers,
        )

        # Track best result
        if best_result is None or completion > best_result.completion:
            best_result = result

        # Report attempt result
        status = "SUCCESS" if result.success else "INSUFFICIENT - relaxing rules"
        if not quiet:
            print(f"\n  Routed: {nets_routed}/{nets_to_route} nets ({completion * 100:.0f}%)")
            print(f"  Status: {status}")

        # Check for success
        if result.success:
            successful_result = result
            break

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("ADAPTIVE RULES SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Design routed successfully with relaxed rules "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print("\nFinal design rules:")
            print(f"  Trace width: {final_result.trace_width:.3f}mm (was {args.trace_width}mm)")
            print(f"  Clearance:   {final_result.clearance:.3f}mm (was {args.clearance}mm)")
            if final_result.tier > 0:
                print(f"\n  Note: Rules were relaxed ({final_result.tier_description})")
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result at tier {final_result.tier} "
                f"({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"even at manufacturer minimum tolerances"
            )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
        return 1

    # Check if at manufacturer minimum
    from kicad_tools.router import get_mfr_limits

    mfr = get_mfr_limits(args.manufacturer)
    at_minimum = (
        final_result.trace_width <= mfr.min_trace + 0.001
        and final_result.clearance <= mfr.min_clearance + 0.001
    )
    if at_minimum and not quiet:
        print(f"\nWARNING: Design uses {args.manufacturer.upper()} minimum tolerances.")
        print("Consider adding layers for more manufacturing margin.")

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer

        if not quiet:
            print("\n--- Optimizing traces ---")

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
            for route in final_result.router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            final_result.router.routes = optimized_routes

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        # Read original PCB content
        original_content = pcb_path.read_text()

        # Get route S-expressions
        route_sexp = final_result.router.to_sexp()

        # Insert routes before final closing parenthesis
        if route_sexp:
            output_content = original_content.rstrip().rstrip(")")
            output_content += f"\n  {route_sexp}\n"
            output_content += ")\n"
        else:
            output_content = original_content
            if not quiet:
                print("  Warning: No routes generated!")

        output_path.write_text(output_content)

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Final trace width: {final_result.trace_width:.3f}mm")
        print(f"  Final clearance: {final_result.clearance:.3f}mm")

    # Run DRC validation unless skipped
    if not args.skip_drc and final_result.nets_routed > 0:
        run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
        )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print("SUCCESS: Routing complete with adaptive rules")
            if final_result.tier > 0:
                print(
                    f"  Note: Relaxed from tier 0 to tier {final_result.tier} "
                    f"({final_result.tier_description})"
                )
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"at tier {final_result.tier}"
            )
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
            )

    return 0 if final_result.success else 1


def route_with_combined_escalation(
    pcb_path: Path,
    output_path: Path,
    args,
    quiet: bool = False,
) -> int:
    """Route a PCB with combined layer and rule escalation (2D search).

    Implements a 2D search across both layer counts and design rule tiers
    to find the minimum viable configuration.

    Args:
        pcb_path: Path to input PCB file
        output_path: Path for output routed PCB file
        args: Parsed command-line arguments
        quiet: Suppress output

    Returns:
        Exit code (0 = success, 1 = failure)
    """
    from kicad_tools.cli.progress import spinner
    from kicad_tools.router import (
        DesignRules,
        LayerStack,
        get_relaxation_tiers,
        is_cpp_available,
        load_pcb_for_routing,
        show_routing_summary,
    )

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

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Get relaxation tiers
    tiers = get_relaxation_tiers(
        initial_trace_width=args.trace_width,
        initial_clearance=args.clearance,
        initial_via_drill=args.via_drill,
        initial_via_diameter=args.via_diameter,
        manufacturer=args.manufacturer,
        min_trace_floor=args.min_trace,
        min_clearance_floor=args.min_clearance_floor,
    )

    # Layer stacks to try (in escalation order)
    layer_configs = [
        (2, LayerStack.two_layer()),
        (4, LayerStack.four_layer_sig_gnd_pwr_sig()),
        (6, LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()),
    ]

    # Filter by max_layers
    layer_configs = [(n, s) for n, s in layer_configs if n <= args.max_layers]

    if not quiet:
        print("=" * 60)
        print("KiCad PCB Autorouter - Combined Escalation Mode")
        print("=" * 60)
        print(f"Input:          {pcb_path}")
        print(f"Output:         {output_path}")
        print(f"Strategy:       {args.strategy}")
        print(f"Manufacturer:   {args.manufacturer}")
        print(f"Max layers:     {args.max_layers}")
        print(f"Min completion: {args.min_completion * 100:.0f}%")
        print(f"Rule tiers:     {len(tiers)}")
        print(f"Layer configs:  {[n for n, _ in layer_configs]}")
        if skip_nets:
            print(f"Skip:           {', '.join(skip_nets)}")
        print()
        print("Search matrix:")
        print("         ", end="")
        for n, _ in layer_configs:
            print(f" {n}L    ", end="")
        print()

    best_result: RuleRelaxationResult | None = None
    successful_result: RuleRelaxationResult | None = None
    results_matrix: dict[tuple[int, int], float] = {}  # (tier, layers) -> completion

    # 2D search: prioritize fewer layers first, then stricter rules
    for layer_count, layer_stack in layer_configs:
        for tier in tiers:
            if not quiet:
                print(
                    f"\nTrying: {layer_count} layers, tier {tier.tier} "
                    f"(trace={tier.trace_width:.2f}mm, clearance={tier.clearance:.2f}mm)"
                )

            # Configure design rules for this tier
            rules = DesignRules(
                grid_resolution=args.grid,
                trace_width=tier.trace_width,
                trace_clearance=tier.clearance,
                via_drill=tier.via_drill,
                via_diameter=tier.via_diameter,
            )

            # Load PCB
            try:
                with spinner(f"Loading PCB ({layer_count}L, tier {tier.tier})...", quiet=quiet):
                    router, net_map = load_pcb_for_routing(
                        str(pcb_path),
                        skip_nets=skip_nets,
                        rules=rules,
                        edge_clearance=args.edge_clearance,
                        layer_stack=layer_stack,
                        force_python=force_python,
                        validate_drc=not args.force,
                        strict_drc=False,
                    )
            except Exception as e:
                if not quiet:
                    print(f"  Error loading PCB: {e}")
                results_matrix[(tier.tier, layer_count)] = 0.0
                continue

            # Count nets to route
            multi_pad_nets = [
                net_num for net_num, pads in router.nets.items() if net_num > 0 and len(pads) >= 2
            ]
            nets_to_route = len(multi_pad_nets)

            # Route
            try:
                if args.strategy == "negotiated":
                    router.route_all_negotiated(
                        max_iterations=args.iterations,
                        timeout=args.timeout,
                    )
                elif args.strategy == "basic":
                    router.route_all()
                elif args.strategy == "monte-carlo":
                    router.route_all_monte_carlo(
                        num_trials=args.mc_trials,
                        verbose=args.verbose and not quiet,
                    )
            except Exception as e:
                if not quiet:
                    print(f"  Routing error: {e}")
                results_matrix[(tier.tier, layer_count)] = 0.0
                continue

            # Calculate completion
            stats = router.get_statistics()
            nets_routed = stats["nets_routed"]
            completion = nets_routed / nets_to_route if nets_to_route > 0 else 1.0
            results_matrix[(tier.tier, layer_count)] = completion

            if not quiet:
                print(f"  Routed: {nets_routed}/{nets_to_route} ({completion * 100:.0f}%)")

            # Create result
            result = RuleRelaxationResult(
                tier=tier.tier,
                trace_width=tier.trace_width,
                clearance=tier.clearance,
                via_drill=tier.via_drill,
                via_diameter=tier.via_diameter,
                tier_description=tier.description,
                router=router,
                net_map=net_map,
                nets_routed=nets_routed,
                nets_to_route=nets_to_route,
                completion=completion,
                success=completion >= args.min_completion,
                layer_count=layer_count,
            )

            # Track best result
            if best_result is None or completion > best_result.completion:
                best_result = result

            # Check for success (first success wins - minimum config)
            if result.success:
                successful_result = result
                break

        # If we found a successful config at this layer count, stop
        if successful_result:
            break

    # Print results matrix
    if not quiet:
        print("\n" + "=" * 60)
        print("SEARCH MATRIX RESULTS")
        print("=" * 60)
        print("         ", end="")
        for n, _ in layer_configs:
            print(f" {n}L     ", end="")
        print()
        for tier in tiers:
            print(f"Tier {tier.tier}:  ", end="")
            for n, _ in layer_configs:
                comp = results_matrix.get((tier.tier, n), 0.0)
                if comp >= args.min_completion:
                    print(f" {comp * 100:3.0f}%✓  ", end="")
                else:
                    print(f" {comp * 100:3.0f}%   ", end="")
            print()

    # Handle results
    if not quiet:
        print("\n" + "=" * 60)
        print("COMBINED ESCALATION SUMMARY")
        print("=" * 60)

    if successful_result:
        final_result = successful_result
        if not quiet:
            print(
                f"Result: Minimum viable configuration found\n"
                f"  Layers: {final_result.layer_count}\n"
                f"  Tier: {final_result.tier} ({final_result.tier_description})\n"
                f"  Completion: {final_result.completion * 100:.0f}%"
            )
            print("\nFinal design rules:")
            print(f"  Trace width: {final_result.trace_width:.3f}mm")
            print(f"  Clearance:   {final_result.clearance:.3f}mm")
    elif best_result:
        final_result = best_result
        if not quiet:
            print(
                f"Result: Best result at {final_result.layer_count} layers, "
                f"tier {final_result.tier} ({final_result.completion * 100:.0f}% completion)"
            )
            print(
                f"Warning: Did not achieve {args.min_completion * 100:.0f}% completion "
                f"in any configuration"
            )
    else:
        if not quiet:
            print("Error: No routing attempts succeeded")
        return 1

    # Check if at manufacturer minimum
    from kicad_tools.router import get_mfr_limits

    mfr = get_mfr_limits(args.manufacturer)
    at_minimum = (
        final_result.trace_width <= mfr.min_trace + 0.001
        and final_result.clearance <= mfr.min_clearance + 0.001
    )
    if at_minimum and not quiet:
        print(f"\nWARNING: Design uses {args.manufacturer.upper()} minimum tolerances.")
        print("Consider redesigning placement for more margin.")

    # Optimize traces
    if not args.no_optimize and final_result.router.routes:
        from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer

        if not quiet:
            print("\n--- Optimizing traces ---")

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
            for route in final_result.router.routes:
                optimized_route = optimizer.optimize_route(route)
                optimized_routes.append(optimized_route)
            final_result.router.routes = optimized_routes

    # Save output
    if args.dry_run:
        if not quiet:
            print("\n--- Dry run - not saving ---")
        return 0

    if not quiet:
        print("\n--- Saving routed PCB ---")

    with spinner("Saving routed PCB...", quiet=quiet):
        # Read original PCB content
        original_content = pcb_path.read_text()

        # Update layer stackup if we escalated
        if final_result.layer_count > 2:
            original_content = update_pcb_layer_stackup(original_content, final_result.layer_count)

        # Get route S-expressions
        route_sexp = final_result.router.to_sexp()

        # Insert routes before final closing parenthesis
        if route_sexp:
            output_content = original_content.rstrip().rstrip(")")
            output_content += f"\n  {route_sexp}\n"
            output_content += ")\n"
        else:
            output_content = original_content
            if not quiet:
                print("  Warning: No routes generated!")

        # Update output filename to include layer count and tier
        if final_result.layer_count > 2 or final_result.tier > 0:
            suffix = ""
            if final_result.layer_count > 2:
                suffix += f"_{final_result.layer_count}layer"
            output_path = output_path.with_stem(output_path.stem + suffix)

        output_path.write_text(output_content)

    if not quiet:
        print(f"  Saved to: {output_path}")
        print(f"  Layer count: {final_result.layer_count}")
        print(f"  Final trace width: {final_result.trace_width:.3f}mm")
        print(f"  Final clearance: {final_result.clearance:.3f}mm")

    # Run DRC validation unless skipped
    if not args.skip_drc and final_result.nets_routed > 0:
        run_post_route_drc(
            output_path=output_path,
            manufacturer=args.manufacturer,
            layers=final_result.layer_count,
            quiet=quiet,
        )

    # Final summary
    if not quiet:
        print("\n" + "=" * 60)
        if final_result.success:
            print(
                f"SUCCESS: Minimum viable config = {final_result.layer_count} layers + "
                f"tier {final_result.tier} rules"
            )
        else:
            print(
                f"PARTIAL: Best result {final_result.completion * 100:.0f}% "
                f"at {final_result.layer_count} layers, tier {final_result.tier}"
            )
            show_routing_summary(
                final_result.router,
                final_result.net_map,
                final_result.nets_to_route,
                quiet=quiet,
            )

    return 0 if final_result.success else 1


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
        type=str,
        default="0.25",
        help=(
            "Grid resolution in mm or 'auto' for automatic selection "
            "(default: 0.25, use 0.1 for dense QFP, or 'auto' to analyze pads)"
        ),
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
    parser.add_argument(
        "--auto-layers",
        action="store_true",
        help=(
            "Automatically escalate layer count on routing failure. "
            "Tries 2 → 4 → 6 layers until routing succeeds or max is reached. "
            "Reports minimum viable layer count for the design."
        ),
    )
    parser.add_argument(
        "--max-layers",
        type=int,
        default=6,
        choices=[2, 4, 6],
        help=(
            "Maximum layer count for auto-escalation (default: 6). Only used with --auto-layers."
        ),
    )
    parser.add_argument(
        "--min-completion",
        type=float,
        default=0.95,
        help=(
            "Minimum routing completion rate for success (default: 0.95 = 95%%). "
            "Only used with --auto-layers. If no layer count achieves this, "
            "the best result is saved."
        ),
    )
    parser.add_argument(
        "--adaptive-rules",
        action="store_true",
        help=(
            "Automatically relax design rules on routing failure. "
            "Tries progressively relaxed trace widths and clearances "
            "until routing succeeds or manufacturer limits are reached. "
            "Reports which rules were relaxed and warns if minimum tolerances used."
        ),
    )
    parser.add_argument(
        "--min-trace",
        type=float,
        help=(
            "Minimum trace width floor for adaptive rules (mm). "
            "Prevents relaxation below this value. "
            "Default: manufacturer minimum (e.g., 0.127mm for JLCPCB)."
        ),
    )
    parser.add_argument(
        "--min-clearance-floor",
        type=float,
        help=(
            "Minimum clearance floor for adaptive rules (mm). "
            "Prevents relaxation below this value. "
            "Default: manufacturer minimum (e.g., 0.127mm for JLCPCB)."
        ),
    )
    parser.add_argument(
        "--progressive-clearance",
        action="store_true",
        help=(
            "Enable progressive clearance relaxation for failed nets. "
            "Routes all nets with standard clearance first, then retries "
            "failed nets with progressively relaxed clearance (up to --min-clearance). "
            "Unlike --adaptive-rules which globally relaxes all rules, this only "
            "relaxes clearance for specific failed nets. Reports which nets needed "
            "relaxation and the clearance used."
        ),
    )
    parser.add_argument(
        "--min-clearance",
        type=float,
        help=(
            "Minimum clearance for progressive relaxation (mm). "
            "Used with --progressive-clearance to set the floor for relaxation. "
            "Default: 50%% of --clearance value."
        ),
    )
    parser.add_argument(
        "--relaxation-levels",
        type=int,
        default=3,
        help=(
            "Number of progressive relaxation levels (default: 3). "
            "More levels = finer-grained relaxation steps."
        ),
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help=(
            "Show detailed routing failure diagnostics. For each failed net, "
            "reports the specific failure reason, blocking obstacles, coordinates, "
            "and actionable suggestions. Failures are grouped by cause for analysis."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help=(
            "Output format for routing diagnostics: "
            "'text' = human-readable output (default); "
            "'json' = JSON output for tooling and automation."
        ),
    )
    parser.add_argument(
        "--high-performance",
        action="store_true",
        help=(
            "Use high-performance mode with aggressive parallelization and more trials. "
            "Uses calibrated settings if available (run 'kicad-tools calibrate' first)."
        ),
    )

    # Cache arguments
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable routing cache (force fresh routing)",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Only use cached results (fail if cache miss)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear routing cache before routing",
    )
    parser.add_argument(
        "--cache-stats",
        action="store_true",
        help="Show routing cache statistics and exit",
    )

    args = parser.parse_args(argv)

    # Validate input
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Warning: Expected .kicad_pcb file, got {pcb_path.suffix}")

    # Validate --auto-layers is not used with explicit --layers
    if args.auto_layers and args.layers != "auto":
        print(
            f"Error: --auto-layers cannot be used with --layers {args.layers}.\n"
            "Use --auto-layers alone, or use --layers to specify a fixed layer count.",
            file=sys.stderr,
        )
        return 1

    # Validate --adaptive-rules is not used with explicit --layers (unless also using --auto-layers)
    if args.adaptive_rules and args.layers != "auto" and not args.auto_layers:
        print(
            f"Error: --adaptive-rules cannot be used with --layers {args.layers}.\n"
            "Use --adaptive-rules alone, with --auto-layers, or use --layers for fixed config.",
            file=sys.stderr,
        )
        return 1

    # Validate min-completion is between 0 and 1
    if args.min_completion < 0 or args.min_completion > 1:
        print(
            f"Error: --min-completion must be between 0 and 1 (got {args.min_completion}).",
            file=sys.stderr,
        )
        return 1

    # Apply high-performance settings if requested
    if getattr(args, "high_performance", False):
        from kicad_tools.performance import get_performance_config

        perf_config = get_performance_config(high_performance=True)

        # Override defaults with high-performance settings
        if not args.quiet:
            print("\n--- High-Performance Mode ---")
            print(f"  CPU cores:         {perf_config.cpu_cores}")
            print(f"  Monte Carlo trials: {perf_config.monte_carlo_trials}")
            print(f"  Parallel workers:   {perf_config.parallel_workers}")
            print(f"  Max iterations:     {perf_config.negotiated_iterations}")
            if perf_config.calibrated:
                print(f"  (Using calibrated settings from {perf_config.calibration_date})")
            print()

        # Apply to routing parameters
        args.mc_trials = perf_config.monte_carlo_trials
        args.iterations = perf_config.negotiated_iterations

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_routed")

    # Resolve grid value: "auto" or numeric
    # We need to resolve this early, before sub-functions are called
    grid_auto_result = None
    if args.grid.lower() == "auto":
        from kicad_tools.router.io import (
            auto_select_grid_resolution,
            extract_pad_positions,
        )

        if not args.quiet:
            print("\n--- Auto-selecting grid resolution ---")
        pad_positions = extract_pad_positions(pcb_path)
        grid_auto_result = auto_select_grid_resolution(
            pads=pad_positions,
            clearance=args.clearance,
        )
        # Replace args.grid with resolved float for downstream code
        args.grid = grid_auto_result.resolution
        if not args.quiet:
            print(grid_auto_result.summary())
            print()
    else:
        try:
            args.grid = float(args.grid)
        except ValueError:
            print(
                f"Error: Invalid grid value '{args.grid}'. Use a number (e.g., 0.25) or 'auto'.",
                file=sys.stderr,
            )
            return 1

    # Handle cache-related commands early
    if args.cache_stats:
        from kicad_tools.router import RoutingCache

        cache = RoutingCache()
        stats = cache.stats()
        print("\n--- Routing Cache Statistics ---")
        print(f"  Cache directory:     {stats['cache_dir']}")
        print(f"  Routing results:     {stats['routing_results_count']}")
        print(f"  Partial net routes:  {stats['partial_routes_count']}")
        print(f"  Total size:          {stats['total_size_mb']:.2f} MB")
        print(f"  Valid results:       {stats['valid_results']}")
        print(f"  Expired results:     {stats['expired_results']}")
        print(f"  TTL:                 {stats['ttl_days']} days")
        print(f"  Max size:            {stats['max_size_mb']:.0f} MB")
        if stats["oldest"]:
            print(f"  Oldest entry:        {stats['oldest']}")
        if stats["newest"]:
            print(f"  Newest entry:        {stats['newest']}")
        return 0

    if args.clear_cache:
        from kicad_tools.router import RoutingCache

        cache = RoutingCache()
        count = cache.clear()
        if not args.quiet:
            print(f"Cleared {count} entries from routing cache")

    # Handle auto-layers mode (separate code path)
    if args.auto_layers and args.adaptive_rules:
        # Combined 2D search: layers + rules
        return route_with_combined_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )
    elif args.auto_layers:
        return route_with_layer_escalation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )
    elif args.adaptive_rules:
        # Adaptive rules only (fixed layer count)
        return route_with_rule_relaxation(
            pcb_path=pcb_path,
            output_path=output_path,
            args=args,
            quiet=args.quiet,
        )

    # Parse skip nets
    skip_nets = []
    if args.skip_nets:
        skip_nets = [n.strip() for n in args.skip_nets.split(",")]

    # Import router modules
    from kicad_tools.analysis import ComplexityAnalyzer, ComplexityRating
    from kicad_tools.router import (
        BusRoutingConfig,
        BusRoutingMode,
        DesignRules,
        DifferentialPairConfig,
        LayerStack,
        RoutabilityAnalyzer,
        is_cpp_available,
        load_pcb_for_routing,
        print_routing_diagnostics_json,
        show_routing_summary,
    )
    from kicad_tools.router.io import detect_layer_stack
    from kicad_tools.schema.pcb import PCB

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

    # Grid resolution already resolved early in main()
    # (args.grid is now a float, grid_auto_result set if "auto" was used)

    # Validate grid resolution vs clearance (prevents DRC violations)
    # Skip validation for auto mode since auto_select_grid_resolution ensures DRC compliance
    if grid_auto_result is None and args.grid > args.clearance:
        recommended_grid = args.clearance / 2
        if not args.force:
            print(
                f"Error: Grid resolution {args.grid}mm exceeds clearance {args.clearance}mm.\n"
                f"This WILL cause DRC violations.\n\n"
                f"Options:\n"
                f"  1. Use a finer grid: --grid {recommended_grid}\n"
                f"  2. Use --grid auto for automatic selection\n"
                f"  3. Use --force to override (not recommended)\n",
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
    from kicad_tools.cli.progress import flush_print, spinner

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
            grid_mode = " (auto)" if grid_auto_result else ""
            print(f"  Grid resolution: {rules.grid_resolution}mm{grid_mode}")
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
        flush_print("\n--- Loading PCB ---")
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

    # Analyze fine-pitch components for grid compatibility warnings
    # This runs automatically to warn users about potential routing issues
    if not quiet:
        from kicad_tools.router.fine_pitch import analyze_fine_pitch_components
        from kicad_tools.router.output import show_fine_pitch_warnings

        fine_pitch_report = analyze_fine_pitch_components(
            pads=router.pads,
            grid_resolution=args.grid,
            trace_width=args.trace_width,
            clearance=args.clearance,
        )
        if fine_pitch_report.has_warnings:
            print("\n--- Fine-Pitch Component Analysis ---")
            show_fine_pitch_warnings(fine_pitch_report, quiet=quiet, verbose=args.verbose)

    # Analyze routability if requested
    if args.analyze:
        # Run pre-routing complexity analysis first
        if not quiet:
            print("\n--- Pre-Routing Complexity Analysis ---")
        try:
            pcb_for_analysis = PCB.load(str(pcb_path))
            complexity_analyzer = ComplexityAnalyzer()
            complexity = complexity_analyzer.analyze(pcb_for_analysis)

            # Show complexity summary
            print(f"\n{'=' * 60}")
            print("COMPLEXITY ANALYSIS")
            print(f"{'=' * 60}")
            print(f"Board: {complexity.board_width_mm:.1f}mm x {complexity.board_height_mm:.1f}mm")
            print(f"Pads: {complexity.total_pads}, Nets: {complexity.total_nets}")

            # Show complexity rating with color
            rating_symbols = {
                ComplexityRating.TRIVIAL: "[TRIVIAL]",
                ComplexityRating.SIMPLE: "[SIMPLE]",
                ComplexityRating.MODERATE: "[MODERATE]",
                ComplexityRating.COMPLEX: "[COMPLEX]",
                ComplexityRating.EXTREME: "[EXTREME]",
            }
            print(
                f"Complexity: {complexity.overall_score:.0f}/100 - "
                f"{rating_symbols[complexity.complexity_rating]}"
            )

            # Show layer predictions
            print("\nLayer Predictions:")
            for pred in complexity.layer_predictions:
                rec_str = " (recommended)" if pred.recommended else ""
                print(
                    f"  {pred.layer_count} layers: {pred.success_probability * 100:.0f}% success{rec_str}"
                )

            # Show bottlenecks
            if complexity.bottlenecks:
                print(f"\nBottlenecks ({len(complexity.bottlenecks)}):")
                for bottleneck in complexity.bottlenecks[:3]:
                    print(f"  - {bottleneck.component_ref}: {bottleneck.description}")

            print(f"{'=' * 60}")
        except Exception as e:
            print(f"Warning: Complexity analysis failed: {e}", file=sys.stderr)

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

    # Check cache for existing routing result (unless --no-cache)
    cache_key = None
    cached_result = None
    use_cache = not args.no_cache

    if use_cache:
        from kicad_tools.router import CacheKey, RoutingCache

        try:
            # Compute cache key from PCB content and rules
            pcb_content = pcb_path.read_bytes()
            cache_key = CacheKey.compute(pcb_content, rules, args.grid)

            cache = RoutingCache()

            if not quiet:
                flush_print("\n--- Checking routing cache ---")

            cached_result = cache.get(cache_key)
            if cached_result is not None:
                if not quiet:
                    print(f"  Cache HIT: {cached_result.success_count} nets routed")
                    print(f"  Segments: {cached_result.total_segments}, Vias: {cached_result.total_vias}")
                    print(f"  Original compute time: {cached_result.compute_time_ms}ms")

                # Deserialize and apply cached routes
                cached_routes = cache.deserialize_routes(cached_result.routes_data)

                # Apply cached routes to router
                router.routes = cached_routes

                if not quiet:
                    print("  Using cached routing result")
            else:
                if not quiet:
                    print(f"  Cache MISS (key: {cache_key.full_key[:32]}...)")
                if args.cache_only:
                    print("Error: --cache-only specified but no cached result found", file=sys.stderr)
                    return 1
        except Exception as e:
            if not quiet:
                print(f"  Cache error: {e}")
            cached_result = None
            if args.cache_only:
                print("Error: --cache-only specified but cache lookup failed", file=sys.stderr)
                return 1

    # Track nets that needed clearance relaxation (for --progressive-clearance)
    relaxed_nets_report: dict[int, float] = {}
    routing_start_time = None

    # Route (skip if using cached result)
    if cached_result is not None:
        # Skip routing - using cached result
        if not quiet:
            flush_print(f"\n--- Using cached result (skipping routing) ---")
    else:
        # Route
        if not quiet:
            flush_print(f"\n--- Routing ({args.strategy}) ---")
            if args.timeout:
                flush_print(f"  Timeout: {args.timeout}s")
            if args.profile:
                profile_output = args.profile_output or "route_profile.prof"
                flush_print(f"  Profiling enabled: {profile_output}")

        import time
        routing_start_time = time.time()

        # Define routing function for profiling
        def do_routing():
            nonlocal diffpair_warnings, relaxed_nets_report
            # Progressive clearance relaxation mode
            if getattr(args, "progressive_clearance", False):
                routes, relaxed_nets_report = router.route_with_progressive_clearance(
                    min_clearance=getattr(args, "min_clearance", None),
                    num_relaxation_levels=getattr(args, "relaxation_levels", 3),
                    max_iterations=args.iterations,
                    timeout=args.timeout,
                )
                return routes
            elif args.strategy == "negotiated":
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

        # Cache the routing result (if caching enabled and routing succeeded)
        if use_cache and cache_key is not None and router.routes:
            import time
            try:
                routing_time_ms = int((time.time() - routing_start_time) * 1000) if routing_start_time else 0
                stats = router.get_statistics()
                cache.put(cache_key, router.routes, stats, routing_time_ms)
                if not quiet:
                    print(f"  Cached routing result ({routing_time_ms}ms compute time)")
            except Exception as e:
                if not quiet:
                    print(f"  Warning: Failed to cache result: {e}")

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

    # Report nets that needed clearance relaxation (--progressive-clearance mode)
    if relaxed_nets_report and not quiet:
        original_clearance = rules.trace_clearance
        print(f"\n--- Clearance Relaxation Report ({len(relaxed_nets_report)} nets) ---")
        print(f"  Original clearance: {original_clearance:.3f}mm")
        for net_id, clearance in sorted(relaxed_nets_report.items(), key=lambda x: x[1]):
            net_name = router.net_names.get(net_id, f"Net {net_id}")
            reduction = (1 - clearance / original_clearance) * 100
            print(f"  {net_name}: {clearance:.3f}mm ({reduction:.0f}% relaxation)")

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
            # Use JSON format if requested
            if args.format == "json":
                print_routing_diagnostics_json(router, net_map, nets_to_route)
            else:
                # Verbose mode shows detailed path analysis for each failure
                verbose = args.verbose or args.diagnostics
                show_routing_summary(router, net_map, nets_to_route, quiet=quiet, verbose=verbose)

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
