#!/usr/bin/env python3
"""
Test different routing strategies on our demo PCBs.

This script compares:
1. Basic routing (route_all)
2. Negotiated congestion routing (route_all_negotiated)
3. Monte Carlo multi-start (route_all_monte_carlo)
4. Adaptive layer routing (AdaptiveAutorouter)
"""

import sys
from pathlib import Path

# Add src to path for development
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kicad_tools.router import (
    load_pcb_for_routing,
    DesignRules,
    AdaptiveAutorouter,
)


def test_basic_routing(pcb_path: str, skip_nets: list, rules: DesignRules):
    """Test basic route_all()."""
    router, net_map = load_pcb_for_routing(str(pcb_path), skip_nets=skip_nets, rules=rules)

    total_nets = len([n for n in router.nets if n > 0])
    routes = router.route_all()
    stats = router.get_statistics()

    return {
        "method": "Basic (route_all)",
        "total_nets": total_nets,
        "routed": stats["nets_routed"],
        "segments": stats["segments"],
        "vias": stats["vias"],
        "length_mm": stats["total_length_mm"],
    }


def test_negotiated_routing(pcb_path: str, skip_nets: list, rules: DesignRules):
    """Test negotiated congestion routing."""
    router, net_map = load_pcb_for_routing(str(pcb_path), skip_nets=skip_nets, rules=rules)

    total_nets = len([n for n in router.nets if n > 0])
    routes = router.route_all_negotiated(max_iterations=5)
    stats = router.get_statistics()

    return {
        "method": "Negotiated Congestion",
        "total_nets": total_nets,
        "routed": stats["nets_routed"],
        "segments": stats["segments"],
        "vias": stats["vias"],
        "length_mm": stats["total_length_mm"],
    }


def test_monte_carlo_routing(pcb_path: str, skip_nets: list, rules: DesignRules, trials: int = 5):
    """Test Monte Carlo multi-start routing."""
    router, net_map = load_pcb_for_routing(str(pcb_path), skip_nets=skip_nets, rules=rules)

    total_nets = len([n for n in router.nets if n > 0])
    routes = router.route_all_monte_carlo(num_trials=trials, verbose=False)
    stats = router.get_statistics()

    return {
        "method": f"Monte Carlo ({trials} trials)",
        "total_nets": total_nets,
        "routed": stats["nets_routed"],
        "segments": stats["segments"],
        "vias": stats["vias"],
        "length_mm": stats["total_length_mm"],
    }


def test_demo(name: str, pcb_path: Path, skip_nets: list, rules: DesignRules):
    """Test all routing strategies on a demo PCB."""
    print(f"\n{'='*60}")
    print(f"Testing: {name}")
    print(f"PCB: {pcb_path.name}")
    print(f"Skip nets: {skip_nets}")
    print(f"{'='*60}")

    if not pcb_path.exists():
        print(f"  ERROR: PCB file not found!")
        return

    results = []

    # Test basic routing
    print("\n1. Testing basic routing...")
    try:
        result = test_basic_routing(pcb_path, skip_nets, rules)
        results.append(result)
        print(f"   Routed: {result['routed']}/{result['total_nets']} nets")
    except Exception as e:
        print(f"   ERROR: {e}")

    # Test negotiated routing
    print("\n2. Testing negotiated congestion routing...")
    try:
        result = test_negotiated_routing(pcb_path, skip_nets, rules)
        results.append(result)
        print(f"   Routed: {result['routed']}/{result['total_nets']} nets")
    except Exception as e:
        print(f"   ERROR: {e}")

    # Test Monte Carlo routing
    print("\n3. Testing Monte Carlo routing (5 trials)...")
    try:
        result = test_monte_carlo_routing(pcb_path, skip_nets, rules, trials=5)
        results.append(result)
        print(f"   Routed: {result['routed']}/{result['total_nets']} nets")
    except Exception as e:
        print(f"   ERROR: {e}")

    # Test Monte Carlo with more trials
    print("\n4. Testing Monte Carlo routing (10 trials)...")
    try:
        result = test_monte_carlo_routing(pcb_path, skip_nets, rules, trials=10)
        results.append(result)
        print(f"   Routed: {result['routed']}/{result['total_nets']} nets")
    except Exception as e:
        print(f"   ERROR: {e}")

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"{'Method':<30} {'Routed':<10} {'Segments':<10} {'Vias':<8} {'Length':<10}")
    print("-" * 68)
    for r in results:
        routed_str = f"{r['routed']}/{r['total_nets']}"
        print(f"{r['method']:<30} {routed_str:<10} {r['segments']:<10} {r['vias']:<8} {r['length_mm']:.1f}mm")

    # Find best result
    if results:
        best = max(results, key=lambda x: (x['routed'], -x['vias']))
        print(f"\nBest strategy: {best['method']} ({best['routed']}/{best['total_nets']} nets)")

    return results


def main():
    demo_dir = Path(__file__).parent

    # Test Charlieplex LED Grid
    charlieplex_pcb = demo_dir / "charlieplex_led_grid" / "charlieplex_3x3.kicad_pcb"
    charlieplex_rules = DesignRules(
        grid_resolution=0.25,
        trace_width=0.3,
        trace_clearance=0.2,
    )
    test_demo(
        "Charlieplex LED Grid",
        charlieplex_pcb,
        skip_nets=["VCC", "GND"],
        rules=charlieplex_rules,
    )

    # Test USB Joystick
    # NOTE: TQFP-32 has 0.8mm pin pitch, so we need a finer grid (0.1mm)
    # to route between pins. A coarser grid won't find paths through the dense QFP.
    usb_pcb = demo_dir / "usb_joystick" / "usb_joystick.kicad_pcb"
    usb_rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.15,
    )
    test_demo(
        "USB Joystick",
        usb_pcb,
        skip_nets=["VCC", "GND", "VBUS"],
        rules=usb_rules,
    )

    print("\n" + "=" * 60)
    print("ROUTING STRATEGY COMPARISON COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
