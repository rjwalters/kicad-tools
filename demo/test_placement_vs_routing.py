#!/usr/bin/env python3
"""
Compare agentic placement optimization vs routing strategies.

This script demonstrates:
1. Force-directed physics placement optimization
2. Multiple routing strategies (Basic, Negotiated, Monte Carlo)
3. How placement quality affects routing success

Usage:
    python test_placement_vs_routing.py
"""

import sys
import random
from pathlib import Path

# Add src to path for development
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kicad_tools.optim import PlacementOptimizer, PlacementConfig
from kicad_tools.schema.pcb import PCB
from kicad_tools.router import load_pcb_for_routing, DesignRules


def test_placement_optimization(pcb_path: Path, name: str):
    """Test placement optimization on a PCB."""
    print(f"\n{'='*60}")
    print(f"PLACEMENT OPTIMIZATION: {name}")
    print(f"{'='*60}")

    # Load PCB
    pcb = PCB.load(str(pcb_path))
    print(f"\nLoaded {len(pcb.footprints)} components")

    # Create optimizer
    optimizer = PlacementOptimizer.from_pcb(pcb)
    print(f"Created {len(optimizer.springs)} springs (net connections)")

    # Initial state
    initial_length = optimizer.total_wire_length()
    print(f"\n--- Initial Placement ---")
    print(f"Wire length estimate: {initial_length:.2f}mm")

    # Store original positions
    original_positions = {
        comp.ref: (comp.x, comp.y, comp.rotation)
        for comp in optimizer.components
    }

    # Randomize positions (within board bounds)
    print(f"\n--- Randomizing Positions ---")
    # Get board bounds from outline
    xs = [v.x for v in optimizer.board_outline.vertices]
    ys = [v.y for v in optimizer.board_outline.vertices]
    board_x1, board_x2 = min(xs), max(xs)
    board_y1, board_y2 = min(ys), max(ys)
    margin = 10  # Keep away from edges

    random.seed(42)  # Reproducible
    for comp in optimizer.components:
        comp.x = random.uniform(board_x1 + margin, board_x2 - margin)
        comp.y = random.uniform(board_y1 + margin, board_y2 - margin)
        comp.rotation = random.choice([0, 90, 180, 270])
        comp.vx = 0
        comp.vy = 0
        comp.omega = 0

    random_length = optimizer.total_wire_length()
    print(f"Randomized wire length: {random_length:.2f}mm")

    # Run optimization
    print(f"\n--- Running Physics Optimization (1000 iterations) ---")
    optimizer.run(iterations=1000, dt=0.02)
    optimizer.snap_to_grid(position_grid=0.25, rotation_grid=90.0)

    optimized_length = optimizer.total_wire_length()
    print(f"Optimized wire length: {optimized_length:.2f}mm")
    print(f"Improvement from random: {(1 - optimized_length/random_length)*100:.1f}%")
    print(f"vs Original manual: {(1 - optimized_length/initial_length)*100:+.1f}%")

    # Show positions
    print(f"\n--- Component Positions ---")
    print(f"{'Component':<10} {'Original':^25} {'Optimized':^25} {'Delta':^10}")
    print("-" * 70)
    for comp in optimizer.components:
        ox, oy, orot = original_positions[comp.ref]
        dx = comp.x - ox
        dy = comp.y - oy
        delta = (dx*dx + dy*dy) ** 0.5
        print(f"{comp.ref:<10} ({ox:6.1f}, {oy:6.1f}) @ {orot:3.0f}°  "
              f"({comp.x:6.1f}, {comp.y:6.1f}) @ {comp.rotation:3.0f}°  "
              f"{delta:6.1f}mm")

    return {
        "initial_length": initial_length,
        "random_length": random_length,
        "optimized_length": optimized_length,
    }


def test_routing_on_placements(pcb_path: Path, name: str, skip_nets: list, rules: DesignRules):
    """Test routing strategies on a PCB."""
    print(f"\n{'='*60}")
    print(f"ROUTING STRATEGIES: {name}")
    print(f"{'='*60}")

    # Test basic routing
    print("\n1. Basic Routing...")
    router, net_map = load_pcb_for_routing(str(pcb_path), skip_nets=skip_nets, rules=rules)
    total_nets = len([n for n in router.nets if n > 0])
    router.route_all()
    stats = router.get_statistics()
    basic_result = {
        "routed": stats["nets_routed"],
        "total": total_nets,
        "vias": stats["vias"],
        "length": stats["total_length_mm"],
    }
    print(f"   Routed: {basic_result['routed']}/{basic_result['total']} nets, "
          f"{basic_result['vias']} vias, {basic_result['length']:.1f}mm")

    # Test Monte Carlo (best performer)
    print("\n2. Monte Carlo (5 trials)...")
    router, net_map = load_pcb_for_routing(str(pcb_path), skip_nets=skip_nets, rules=rules)
    router.route_all_monte_carlo(num_trials=5, verbose=False)
    stats = router.get_statistics()
    mc_result = {
        "routed": stats["nets_routed"],
        "total": total_nets,
        "vias": stats["vias"],
        "length": stats["total_length_mm"],
    }
    print(f"   Routed: {mc_result['routed']}/{mc_result['total']} nets, "
          f"{mc_result['vias']} vias, {mc_result['length']:.1f}mm")

    return {"basic": basic_result, "monte_carlo": mc_result}


def main():
    demo_dir = Path(__file__).parent

    print("=" * 60)
    print("KICAD-TOOLS: PLACEMENT & ROUTING COMPARISON")
    print("=" * 60)

    results = {}

    # Test Charlieplex LED Grid
    charlieplex_pcb = demo_dir / "charlieplex_led_grid" / "charlieplex_3x3.kicad_pcb"
    if charlieplex_pcb.exists():
        placement = test_placement_optimization(charlieplex_pcb, "Charlieplex LED Grid")
        routing = test_routing_on_placements(
            charlieplex_pcb,
            "Charlieplex LED Grid",
            skip_nets=["VCC", "GND"],
            rules=DesignRules(grid_resolution=0.25, trace_width=0.3, trace_clearance=0.2),
        )
        results["charlieplex"] = {"placement": placement, "routing": routing}

    # Test USB Joystick
    usb_pcb = demo_dir / "usb_joystick" / "usb_joystick.kicad_pcb"
    if usb_pcb.exists():
        placement = test_placement_optimization(usb_pcb, "USB Joystick")
        routing = test_routing_on_placements(
            usb_pcb,
            "USB Joystick",
            skip_nets=["VCC", "GND", "VBUS"],
            rules=DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15),
        )
        results["usb_joystick"] = {"placement": placement, "routing": routing}

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY: PLACEMENT & ROUTING CAPABILITIES")
    print("=" * 60)

    print("""
PLACEMENT OPTIMIZATION (Force-Directed Physics):
- Uses electrostatic repulsion between component outlines
- Springs between connected pins minimize wire length
- Rotation potential aligns components to 90° grid
- Can recover good placements from random starting points

ROUTING STRATEGIES:
- Basic (A*): Simple shortest-path routing
- Negotiated Congestion: Rip-up and reroute with history costs
- Monte Carlo: Multiple random orderings, pick best result

KEY FINDINGS:
""")

    for name, data in results.items():
        p = data["placement"]
        r = data["routing"]
        print(f"\n{name.upper()}:")
        print(f"  Placement:")
        print(f"    - Random start wire length: {p['random_length']:.1f}mm")
        print(f"    - Optimized wire length:    {p['optimized_length']:.1f}mm")
        print(f"    - Improvement:              {(1-p['optimized_length']/p['random_length'])*100:.1f}%")
        print(f"  Routing (Monte Carlo):")
        print(f"    - Nets routed: {r['monte_carlo']['routed']}/{r['monte_carlo']['total']}")
        print(f"    - Vias: {r['monte_carlo']['vias']}")
        print(f"    - Trace length: {r['monte_carlo']['length']:.1f}mm")


if __name__ == "__main__":
    main()
