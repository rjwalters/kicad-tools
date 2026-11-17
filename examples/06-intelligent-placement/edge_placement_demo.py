#!/usr/bin/env python3
"""
Edge Placement Demo

Demonstrates how kicad-tools v0.6.0 auto-detects components that should
be placed at board edges (connectors, mounting holes, test points, switches).
"""

from pathlib import Path

from kicad_tools.optim import (
    detect_edge_components,
    get_board_edges,
)
from kicad_tools.schema.pcb import PCB


def main():
    """Run the edge placement demo."""
    # Load the example PCB
    pcb_path = Path(__file__).parent / "fixtures" / "mcu_board.kicad_pcb"
    print(f"Loading PCB: {pcb_path.name}")
    pcb = PCB.load(str(pcb_path))

    print(f"Found {len(pcb.footprints)} components")
    print()

    # Get board edge geometry
    print("=" * 60)
    print("Board Edge Geometry")
    print("=" * 60)
    print()

    edges = get_board_edges(pcb)

    print(f"Top edge:    {edges.top.length:.1f}mm")
    print(f"Bottom edge: {edges.bottom.length:.1f}mm")
    print(f"Left edge:   {edges.left.length:.1f}mm")
    print(f"Right edge:  {edges.right.length:.1f}mm")
    print()

    # Show corners
    corners = edges.corners()
    print("Corners (ideal for mounting holes):")
    for i, corner in enumerate(corners):
        labels = ["Top-Left", "Top-Right", "Bottom-Right", "Bottom-Left"]
        print(f"  {labels[i]}: ({corner.x:.1f}, {corner.y:.1f})")
    print()

    # Detect edge components
    print("=" * 60)
    print("Edge Component Detection")
    print("=" * 60)
    print()

    constraints = detect_edge_components(
        pcb,
        include_connectors=True,
        include_mounting_holes=True,
        include_test_points=True,
        include_switches=True,
        include_leds=False,  # LEDs disabled by default
    )

    if not constraints:
        print("No edge components detected.")
        return

    # Group by component type
    connectors = []
    mounting_holes = []
    test_points = []
    switches = []
    other = []

    for c in constraints:
        ref = c.reference.upper()
        if ref.startswith(("J", "P", "USB", "DC", "CON")):
            connectors.append(c)
        elif ref.startswith(("MH", "H")):
            mounting_holes.append(c)
        elif ref.startswith(("TP", "TEST")):
            test_points.append(c)
        elif ref.startswith(("SW", "BTN", "S")):
            switches.append(c)
        else:
            other.append(c)

    # Display by category
    if connectors:
        print("CONNECTORS (should be at board edge for accessibility)")
        print("-" * 50)
        for c in connectors:
            print(f"  {c.reference:8s} edge={c.edge:6s} slide={c.slide}")
        print()

    if mounting_holes:
        print("MOUNTING HOLES (prefer corners for mechanical stability)")
        print("-" * 50)
        for c in mounting_holes:
            corner_str = " [corner priority]" if c.corner_priority else ""
            print(f"  {c.reference:8s} edge={c.edge:6s} slide={c.slide}{corner_str}")
        print()

    if test_points:
        print("TEST POINTS (edge accessible for probing)")
        print("-" * 50)
        for c in test_points:
            print(f"  {c.reference:8s} edge={c.edge:6s} slide={c.slide}")
        print()

    if switches:
        print("SWITCHES (user-accessible edges)")
        print("-" * 50)
        for c in switches:
            print(f"  {c.reference:8s} edge={c.edge:6s} slide={c.slide}")
        print()

    if other:
        print("OTHER EDGE COMPONENTS")
        print("-" * 50)
        for c in other:
            print(f"  {c.reference:8s} edge={c.edge:6s} slide={c.slide}")
        print()

    # Summary
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print()
    print(f"Total edge components detected: {len(constraints)}")
    print(f"  - Connectors: {len(connectors)}")
    print(f"  - Mounting holes: {len(mounting_holes)}")
    print(f"  - Test points: {len(test_points)}")
    print(f"  - Switches: {len(switches)}")
    print()
    print("Use edge detection during placement optimization with:")
    print("  optimizer = PlacementOptimizer.from_pcb(pcb, edge_detect=True)")
    print()
    print("Or via CLI:")
    print("  kicad-tools placement optimize board.kicad_pcb --edge-detect")


if __name__ == "__main__":
    main()
