#!/usr/bin/env python3
"""
Demonstrate autorouting on the charlieplexed LED grid PCB.

This script:
1. Loads the generated PCB file
2. Creates an Autorouter instance
3. Routes all nets
4. Saves the routed result

Usage:
    python route_demo.py [input_pcb] [output_pcb]

Example:
    python route_demo.py output/charlieplex_3x3.kicad_pcb output/charlieplex_3x3_routed.kicad_pcb
"""

import sys
from pathlib import Path

# Add src to path for development (ensures source version is used)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kicad_tools.dev import warn_if_stale
from kicad_tools.router import DesignRules, load_pcb_for_routing
from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer

# Warn if running source scripts with stale pipx install
warn_if_stale()


def main():
    """Run the routing demo."""
    # Parse arguments
    demo_dir = Path(__file__).parent
    input_pcb = sys.argv[1] if len(sys.argv) > 1 else "output/charlieplex_3x3.kicad_pcb"
    output_pcb = sys.argv[2] if len(sys.argv) > 2 else "output/charlieplex_3x3_routed.kicad_pcb"

    input_path = demo_dir / input_pcb
    output_path = demo_dir / output_pcb

    if not input_path.exists():
        print(f"Error: Input PCB not found: {input_path}")
        print("Run generate_pcb.py first to create the PCB file.")
        sys.exit(1)

    print("=" * 60)
    print("Charlieplex LED Grid Autorouting Demo")
    print("=" * 60)
    print(f"\nInput:  {input_path}")
    print(f"Output: {output_path}")

    # Configure design rules for this board
    # Note: grid_resolution should be <= clearance/2 for reliable DRC compliance
    rules = DesignRules(
        grid_resolution=0.1,  # 0.1mm grid (clearance/2 for reliable DRC)
        trace_width=0.3,  # 0.3mm traces (12mil)
        trace_clearance=0.2,  # 0.2mm clearance (8mil)
        via_drill=0.3,  # 0.3mm via drill
        via_diameter=0.6,  # 0.6mm via pad
    )

    # Skip power nets (we won't route VCC/GND in this demo)
    skip_nets = ["VCC", "GND"]

    print("\n--- Loading PCB ---")
    print(f"  Grid resolution: {rules.grid_resolution}mm")
    print(f"  Trace width: {rules.trace_width}mm")
    print(f"  Clearance: {rules.trace_clearance}mm")
    print(f"  Skipping nets: {skip_nets}")

    # Load the PCB and create autorouter
    router, net_map = load_pcb_for_routing(
        str(input_path),
        skip_nets=skip_nets,
        rules=rules,
    )

    print(f"\n  Board size: {router.grid.width}mm x {router.grid.height}mm")
    print(f"  Nets loaded: {len(net_map)}")
    print(f"  Nets to route: {len([n for n in router.nets if n > 0])}")

    # Route all nets using standard routing (DRC-safe)
    print("\n--- Routing (standard mode) ---")
    router.route_all()

    # Get statistics before optimization
    stats_before = router.get_statistics()

    print("\n--- Raw Results (before optimization) ---")
    print(f"  Routes created: {stats_before['routes']}")
    print(f"  Segments: {stats_before['segments']}")
    print(f"  Vias: {stats_before['vias']}")
    print(f"  Total length: {stats_before['total_length_mm']:.2f}mm")
    print(f"  Nets routed: {stats_before['nets_routed']}")

    # Optimize traces - merge collinear segments, eliminate zigzags, etc.
    print("\n--- Optimizing traces ---")
    opt_config = OptimizationConfig(
        merge_collinear=True,
        eliminate_zigzags=True,
        compress_staircase=True,
        convert_45_corners=True,
        minimize_vias=True,
    )
    optimizer = TraceOptimizer(config=opt_config)

    optimized_routes = []
    for route in router.routes:
        optimized_route = optimizer.optimize_route(route)
        optimized_routes.append(optimized_route)
    router.routes = optimized_routes

    # Get statistics after optimization
    stats = router.get_statistics()

    segments_before = stats_before["segments"]
    segments_after = stats["segments"]
    reduction = (1 - segments_after / segments_before) * 100 if segments_before > 0 else 0

    print(f"  Segments: {segments_before} -> {segments_after} ({reduction:.1f}% reduction)")
    print(f"  Vias: {stats_before['vias']} -> {stats['vias']}")

    print("\n--- Final Results ---")
    print(f"  Routes created: {stats['routes']}")
    print(f"  Segments: {stats['segments']}")
    print(f"  Vias: {stats['vias']}")
    print(f"  Total length: {stats['total_length_mm']:.2f}mm")
    print(f"  Nets routed: {stats['nets_routed']}")

    # Generate output PCB with routes
    print("\n--- Saving routed PCB ---")

    # Read original PCB content
    original_content = input_path.read_text()

    # Get route S-expressions
    route_sexp = router.to_sexp()

    # Insert routes before final closing parenthesis
    if route_sexp:
        output_content = original_content.rstrip().rstrip(")")
        output_content += "\n"
        output_content += f"  {route_sexp}\n"
        output_content += ")\n"
    else:
        output_content = original_content
        print("  Warning: No routes generated!")

    output_path.write_text(output_content)
    print(f"  Saved to: {output_path}")

    # Summary
    print("\n" + "=" * 60)
    total_nets = len([n for n in router.nets if n > 0])
    if stats["nets_routed"] == total_nets:
        print("SUCCESS: All nets routed!")
    else:
        print(f"PARTIAL: Routed {stats['nets_routed']}/{total_nets} nets")
        print("  Some nets may require manual routing or a different strategy.")
    print("=" * 60)

    return 0 if stats["nets_routed"] == total_nets else 1


if __name__ == "__main__":
    sys.exit(main())
