#!/usr/bin/env python3
"""Isolated reach measurement for the board-07 DDR data byte (Issue #4084).

Reproduces the "11-net DDR bundle alone on an empty 4-layer board"
scenario from #3438's isolation matrix so the monotonic-certificate
(Issue #4084, Phase 1) reach delta is re-runnable, not just quoted in
issue prose.

The bundle is the DDR_DATA_BYTE_0 group on board 07: nine single-ended
nets (DQ0-7 + DM0) plus the DQS_P/DQS_N diff pair, connecting a facing
QFN-48 pin column on U1 (right side, pins 25-35) to its mirror on U2
(left side, pins 1-11), at 0.8 mm pitch across a ~30 mm channel.  Both
columns declare the byte in the SAME net order, so along the row long
axis (y) the two facing columns are CO-ORIENTED, not reversed — which is
exactly what the certificate reports.

Usage:
    uv run python boards/07-matchgroup-test/ddr_bundle_isolation_repro.py

Prints, for the certificate flag OFF (identity baseline) and ON:
  * the monotonic-certificate classification (feasible? witness?),
  * the routing order the escape scheduler used,
  * the measured reach (X/11 nets routed to completion).

Pure in-process; no CLI subprocess (the flag is not exposed on ``kct
route``).  Uses the negotiated router with the C++ backend when built.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the DDR pin declarations importable from the sibling generator.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kicad_tools.router.core import Autorouter  # noqa: E402
from kicad_tools.router.layers import LayerStack  # noqa: E402
from kicad_tools.router.rules import NetClassRouting  # noqa: E402

PITCH = 0.8

# The DDR byte's row order on both facing columns (U1 pins 25-35 == U2
# pins 1-11), matching generate_pcb.py's pin_nets declarations.  DQS_P /
# DQS_N are the interleaved diff pair.
ROW_NETS = [
    "DQ0",
    "DQ1",
    "DQ2",
    "DQ3",
    "DM0",
    "DQS_P",
    "DQS_N",
    "DQ4",
    "DQ5",
    "DQ6",
    "DQ7",
]


def build_isolated_router(
    *,
    enable_certificate: bool,
    channel_mm: float = 30.0,
) -> tuple[Autorouter, list[int]]:
    """Build a router with ONLY the 11-net DDR byte on an empty board.

    U1's facing column sits at x=20, U2's mirror at x=20+channel; each net
    connects one pad on each column at the same y (co-oriented), exactly
    as board 07 declares them.
    """
    cls = NetClassRouting(
        name="DDR_DATA_BYTE_0",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        length_critical=True,
        length_match_group="DDR_DATA_BYTE_0",
        length_match_reference=None,
        length_match_tolerance_mm=0.1,
    )
    net_class_map: dict[str, NetClassRouting] = {}
    router = Autorouter(
        width=80.0,
        height=40.0,
        net_class_map=net_class_map,
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
    )
    router.enable_monotone_certificate_order = enable_certificate

    u1_x = 20.0
    u2_x = u1_x + channel_mm
    centre_y = 20.0
    base_y = centre_y - (len(ROW_NETS) - 1) * PITCH / 2.0

    net_ids: list[int] = []
    for i, name in enumerate(ROW_NETS):
        net_id = i + 1
        net_ids.append(net_id)
        y = base_y + i * PITCH
        router.add_component(
            "U1",
            [{"number": str(25 + i), "x": u1_x, "y": y, "net": net_id, "net_name": name}],
        )
        router.add_component(
            "U2",
            [{"number": str(1 + i), "x": u2_x, "y": y, "net": net_id, "net_name": name}],
        )
        net_class_map[name] = cls
    router.net_class_map = net_class_map
    return router, net_ids


def measure_reach(*, enable_certificate: bool) -> None:
    router, net_ids = build_isolated_router(enable_certificate=enable_certificate)

    label = "ON (certificate)" if enable_certificate else "OFF (identity baseline)"
    print(f"\n=== enable_monotone_certificate_order = {label} ===")

    # Show the ordering the escape scheduler will use for the group.
    ordered = router._apply_byte_lane_inner_priority(list(net_ids))
    print(f"routing order (net ids): {ordered}")
    for grp, cert in router._last_monotone_certificates.items():
        print(
            f"certificate[{grp}]: feasible={cert.feasible} "
            f"mirrored={cert.mirrored} inversions={cert.inversion_count}"
        )
        if not cert.feasible:
            pairs = ", ".join(f"({p.net_a},{p.net_b})" for p in cert.witness)
            print(f"  witness (forced crossings): {pairs}")

    routes = router.route_all_negotiated(seed=42)
    # A net is "reached" when it has at least one non-escape (full) route.
    routed_nets = {
        r.net
        for r in routes
        if getattr(r, "net", None) is not None and not getattr(r, "is_escape", False)
    }
    reached = len(routed_nets & set(net_ids))
    print(f"reach: {reached}/{len(net_ids)} nets routed")


def main() -> int:
    print("Board-07 DDR data byte isolation reach measurement (Issue #4084)")
    print(f"bundle: {len(ROW_NETS)} nets, 0.8mm pitch, facing QFN-48 columns")
    measure_reach(enable_certificate=False)
    measure_reach(enable_certificate=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
