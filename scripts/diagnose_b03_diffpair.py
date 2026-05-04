#!/usr/bin/env python3
"""Diagnostic for issue #2490 sub-task (a).

Surfaces *why* the differential-pair pre-pass fails on board 03
(USB joystick).  Loads the board the same way the CLI does, runs the
pad-pairing helper, and exercises ``CoupledPathfinder.route_coupled``
for each MST segment, printing the failure mode.

Usage:
    python scripts/diagnose_b03_diffpair.py [path/to/board.kicad_pcb]

Output covers, per pair:
- Detected pads on each side (P-net and N-net) with their layer/pos.
- Pair-up result (number of coupled segments, number of stub edges).
- For each coupled segment: start/end pad pitches in grid cells,
  configured trace + via half-widths, blocked-cell counts in the
  search corridor, via-placement probe at the four endpoint pads,
  and the route_coupled outcome (segment counts on success or a
  "no path" failure).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from kicad_tools.router import (  # noqa: E402
    DesignRules,
    DifferentialPairConfig,
    create_net_class_map,
    load_pcb_for_routing,
)
from kicad_tools.router.diffpair_routing import CoupledPathfinder  # noqa: E402


def _print_pad(p, indent: str = "      ") -> None:
    print(
        f"{indent}- ref={p.ref} pin={p.pin} net={p.net_name!r} "
        f"layer={p.layer.value} pos=({p.x:.3f}, {p.y:.3f})"
    )


def main() -> int:
    pcb_path = Path(
        sys.argv[1] if len(sys.argv) > 1 else "boards/03-usb-joystick/output/usb_joystick.kicad_pcb"
    )
    if not pcb_path.exists():
        print(f"ERROR: PCB not found: {pcb_path}")
        return 1

    print("=" * 70)
    print("Board 03 differential-pair pre-pass diagnostic")
    print(f"PCB: {pcb_path}")
    print("=" * 70)

    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
    )
    net_class_map = create_net_class_map(
        power_nets=["VCC", "VBUS", "GND"],
        high_speed_nets=["USB_D+", "USB_D-"],
        clock_nets=["XTAL1", "XTAL2"],
    )
    skip_nets = ["VCC", "GND", "VBUS"]

    router, net_map = load_pcb_for_routing(
        str(pcb_path),
        skip_nets=skip_nets,
        rules=rules,
    )
    router.net_class_map.update(net_class_map)
    print(
        f"\nGrid: {router.grid.cols} x {router.grid.rows} cells, "
        f"{router.grid.num_layers} layers, resolution={router.grid.resolution}mm"
    )
    print(f"Nets loaded: {len(net_map)}")

    diffpair_helper = router._diffpair  # noqa: SLF001
    pairs = diffpair_helper.detect_differential_pairs()
    if not pairs:
        print("\nNo differential pairs detected — nothing to diagnose.")
        return 0
    print(f"\nDetected {len(pairs)} differential pair(s):")
    for p in pairs:
        print(f"  {p}: {p.pair_type.value}")

    cfg = DifferentialPairConfig(enabled=True)
    rc = 0
    for pair in pairs:
        if pair.rules is not None:
            pair.rules = cfg.get_rules(pair.pair_type)
        spacing = (
            cfg.spacing if cfg.spacing is not None else (pair.rules.spacing if pair.rules else 0.2)
        )

        print("\n" + "-" * 70)
        print(f"Pair: {pair}")
        pad_result = diffpair_helper._get_pair_pads(pair)  # noqa: SLF001
        if pad_result is None:
            print("  ERROR: Could not find pads for this pair (missing pads or only 1 per side).")
            rc = 1
            continue
        p_pads, n_pads = pad_result
        print(f"  P-net pads ({len(p_pads)}):")
        for p in p_pads:
            _print_pad(p)
        print(f"  N-net pads ({len(n_pads)}):")
        for n in n_pads:
            _print_pad(n)

        # Pair-up step.
        if len(p_pads) == 2 and len(n_pads) == 2:
            from kicad_tools.router.diffpair_routing import CoupledSegmentSpec

            legacy = diffpair_helper._pair_pads_for_coupled_routing(p_pads, n_pads)
            coupled_specs = [
                CoupledSegmentSpec(p_start=ps, p_end=pe, n_start=ns, n_end=ne, polarity_swap=False)
                for ps, pe, ns, ne in legacy
            ]
            stub_specs = []
        else:
            coupled_specs, stub_specs = diffpair_helper._pair_pads_for_coupled_routing_npad(
                p_pads, n_pads
            )

        print(
            f"\n  Pair-up: {len(coupled_specs)} coupled segment(s), {len(stub_specs)} stub edge(s)"
        )
        if not coupled_specs:
            print(
                "  RESULT: Pad-pairing produced no coupled segments — "
                "would skip pre-pass with 'complex pad configuration'."
            )
            rc = 1
            continue

        spacing_cells = int(spacing / router.grid.resolution)
        for i, spec in enumerate(coupled_specs):
            polarity_marker = " (polarity-swap)" if spec.polarity_swap else ""
            print(f"\n  Segment {i + 1}/{len(coupled_specs)}{polarity_marker}:")
            print("    P start:")
            _print_pad(spec.p_start, indent="      ")
            print("    P end:")
            _print_pad(spec.p_end, indent="      ")
            print("    N start:")
            _print_pad(spec.n_start, indent="      ")
            print("    N end:")
            _print_pad(spec.n_end, indent="      ")

            p_start_gx, p_start_gy = router.grid.world_to_grid(spec.p_start.x, spec.p_start.y)
            p_end_gx, p_end_gy = router.grid.world_to_grid(spec.p_end.x, spec.p_end.y)
            n_start_gx, n_start_gy = router.grid.world_to_grid(spec.n_start.x, spec.n_start.y)
            n_end_gx, n_end_gy = router.grid.world_to_grid(spec.n_end.x, spec.n_end.y)
            actual_start_spacing = math.sqrt(
                (p_start_gx - n_start_gx) ** 2 + (p_start_gy - n_start_gy) ** 2
            )
            actual_end_spacing = math.sqrt((p_end_gx - n_end_gx) ** 2 + (p_end_gy - n_end_gy) ** 2)
            print(
                f"    Configured spacing_cells={spacing_cells}, "
                f"start spacing_cells={actual_start_spacing:.2f}, "
                f"end spacing_cells={actual_end_spacing:.2f}"
            )

            pf = CoupledPathfinder(
                router.grid,
                router.rules,
                spacing_cells,
                net_class_map=router.net_class_map,
                allow_swap_via=spec.polarity_swap,
            )
            print(
                f"    trace_half_width_cells={pf._trace_half_width_cells}, "
                f"via_half_cells={pf._via_half_cells}"
            )

            # Corridor occupancy by layer (helps tell tight corridor vs.
            # geometric impossibility).
            x_min = max(0, min(p_start_gx, n_start_gx, p_end_gx, n_end_gx) - 5)
            x_max = min(
                router.grid.cols - 1,
                max(p_start_gx, n_start_gx, p_end_gx, n_end_gx) + 5,
            )
            y_min = max(0, min(p_start_gy, n_start_gy, p_end_gy, n_end_gy) - 5)
            y_max = min(
                router.grid.rows - 1,
                max(p_start_gy, n_start_gy, p_end_gy, n_end_gy) + 5,
            )
            for layer in range(router.grid.num_layers):
                blocked = 0
                obstacle = 0
                total = 0
                for y in range(y_min, y_max + 1):
                    for x in range(x_min, x_max + 1):
                        total += 1
                        cell = router.grid.grid[layer][y][x]
                        if cell.blocked:
                            blocked += 1
                            if cell.is_obstacle:
                                obstacle += 1
                pct = (blocked / total * 100) if total else 0.0
                print(
                    f"    Layer {layer}: blocked {blocked}/{total} "
                    f"({pct:.1f}%), of which obstacles={obstacle} "
                    f"in corridor [({x_min},{y_min})-({x_max},{y_max})]"
                )

            # Probe whether vias can be placed at the four endpoint pads.
            via_blocked_p_start = pf._is_via_blocked(p_start_gx, p_start_gy, spec.p_start.net)
            via_blocked_n_start = pf._is_via_blocked(n_start_gx, n_start_gy, spec.n_start.net)
            via_blocked_p_end = pf._is_via_blocked(p_end_gx, p_end_gy, spec.p_end.net)
            via_blocked_n_end = pf._is_via_blocked(n_end_gx, n_end_gy, spec.n_end.net)
            print(
                f"    Via probe @ start: P={via_blocked_p_start} "
                f"N={via_blocked_n_start}; @ end: "
                f"P={via_blocked_p_end} N={via_blocked_n_end}"
            )

            result = pf.route_coupled(spec.p_start, spec.p_end, spec.n_start, spec.n_end)
            if result is None:
                print(
                    "    -> FAILED: coupled A* exhausted open set "
                    "(or hit max_iterations) without reaching both goals."
                )
                rc = 1
            else:
                p_route, n_route = result
                print(
                    f"    -> OK: P route has {len(p_route.segments)} segments, "
                    f"{len(p_route.vias)} vias; N route has "
                    f"{len(n_route.segments)} segments, {len(n_route.vias)} vias"
                )

    print("\n" + "=" * 70)
    if rc == 0:
        print("Diagnostic completed: all coupled segments routed successfully.")
    else:
        print("Diagnostic completed: at least one coupled segment FAILED.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
