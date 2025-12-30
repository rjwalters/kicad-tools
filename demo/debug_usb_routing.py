#!/usr/bin/env python3
"""
Debug the USB joystick routing issues.

This script investigates why routes from the MCU to other components fail.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kicad_tools.router import load_pcb_for_routing, DesignRules


def main():
    demo_dir = Path(__file__).parent
    pcb_path = demo_dir / "usb_joystick" / "usb_joystick.kicad_pcb"

    # Try different grid resolutions
    for grid_res in [0.5, 0.25, 0.1]:
        print(f"\n{'='*60}")
        print(f"Testing grid resolution: {grid_res}mm")
        print(f"{'='*60}")

        rules = DesignRules(
            grid_resolution=grid_res,
            trace_width=0.2,
            trace_clearance=0.15,
        )

        router, net_map = load_pcb_for_routing(
            str(pcb_path),
            skip_nets=["VCC", "GND", "VBUS"],
            rules=rules,
        )

        print(f"Grid size: {router.grid.cols}x{router.grid.rows}")
        print(f"Origin: ({router.grid.origin_x}, {router.grid.origin_y})")
        print(f"Board: {router.grid.width}x{router.grid.height}mm")

        # Print component positions
        print("\nComponent pads:")
        components = {}
        for (ref, pin), pad in router.pads.items():
            if ref not in components:
                components[ref] = []
            components[ref].append((pin, pad.x, pad.y, pad.net_name))

        for ref in sorted(components.keys()):
            pads = components[ref]
            print(f"\n  {ref}: {len(pads)} pads")
            for pin, x, y, net in pads[:3]:  # Show first 3 pads
                print(f"    Pin {pin}: ({x:.2f}, {y:.2f}) net={net}")
            if len(pads) > 3:
                print(f"    ... and {len(pads)-3} more")

        # Try routing just the crystal connection
        print("\n\nTrying to route XTAL1 (net 15)...")
        xtal_pads = [p for (ref, pin), p in router.pads.items() if p.net == 15]
        print(f"  XTAL1 pads: {len(xtal_pads)}")
        for p in xtal_pads:
            print(f"    ({p.x:.2f}, {p.y:.2f}) ref={p.ref}")

        if len(xtal_pads) >= 2:
            # Check if there's a clear path
            p1, p2 = xtal_pads[0], xtal_pads[1]
            gx1, gy1 = router.grid.world_to_grid(p1.x, p1.y)
            gx2, gy2 = router.grid.world_to_grid(p2.x, p2.y)
            print(f"\n  Grid coords: ({gx1}, {gy1}) -> ({gx2}, {gy2})")

            # Check if source/target are blocked
            from kicad_tools.router.layers import Layer
            layer = Layer.F_CU
            src_blocked = router.grid.is_blocked(gx1, gy1, layer)
            dst_blocked = router.grid.is_blocked(gx2, gy2, layer)
            print(f"  Source blocked: {src_blocked}")
            print(f"  Dest blocked: {dst_blocked}")

            # Check cells around source
            print("\n  Cells around source:")
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    nx, ny = gx1 + dx, gy1 + dy
                    if 0 <= nx < router.grid.cols and 0 <= ny < router.grid.rows:
                        blocked = router.grid.is_blocked(nx, ny, layer)
                        c = "X" if blocked else "."
                        print(c, end="")
                    else:
                        print("O", end="")  # Out of bounds
                print()

        # Try routing
        routes = router.route_all()
        stats = router.get_statistics()
        print(f"\nRouted: {stats['nets_routed']}/13 nets")


if __name__ == "__main__":
    main()
