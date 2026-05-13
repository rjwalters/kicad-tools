#!/usr/bin/env python3
"""
Differential Pair Test Board - Complete Design Generation

Epic #2556 Phase 4L (issue #2658) regression testbench.

This script orchestrates the full pipeline for board 06:
    1. Create the project file (.kicad_pro)
    2. Generate the schematic (.kicad_sch)
    3. Generate the unrouted PCB (.kicad_pcb)
    4. Route the PCB (...routed.kicad_pcb)
    5. Run DRC via ``kct check --mfr jlcpcb``

The board is a 4-layer JLCPCB tier-1 stackup
(F.Cu / In1.Cu GND / In2.Cu PWR / B.Cu) carrying 9 differential pairs
across 4 protocol families (USB 2.0, USB 3.0, PCIe Gen1, MIPI D-PHY).

The router is configured with custom ``NetClassRouting`` instances per
protocol that opt into each Phase 1-3 feature:

    - intra_pair_clearance (Phase 1A/1C)
    - coupled_routing (Phase 2E)
    - coupled_continuity_threshold (Phase 2G)
    - target_diff_impedance (Phase 3K)
    - target_single_impedance (Phase 3K)
    - skew_tolerance_mm (Phase 3H)

Usage:
    python generate_design.py [output_dir]

If no output directory is specified, files are written to ./output/.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED, NET_CLASS_POWER, NetClassRouting

# Re-export net definitions and footprint generators from generate_pcb.
sys.path.insert(0, str(Path(__file__).parent))
import generate_pcb  # noqa: E402
import generate_schematic  # noqa: E402

warn_if_stale()


# =============================================================================
# Per-Protocol Net Class Declarations
# =============================================================================
# These NetClassRouting instances are the authoritative "scenario" data the
# board exercises.  ``build_net_class_map()`` below assembles them into a
# net-name -> NetClassRouting dict that ``generate_design.create_net_class_map``
# consumes during routing.
#
# Each protocol class explicitly opts into Phase 1-3 features.  AC#6 of issue
# #2658 asserts that at least one pair engages each feature; this dict is
# the single source of truth for that audit.
# =============================================================================


def usb2_net_class() -> NetClassRouting:
    """USB 2.0 High-Speed net class (1 pair).

    Reuses NET_CLASS_HIGH_SPEED as the template (intra_pair_clearance=0.075
    from Phase 1C, coupled_routing=True from Phase 2.5a) and adds the
    DRC-side coupled_continuity_threshold (Phase 2G) and target_diff_impedance
    (Phase 3K).
    """
    return NetClassRouting(
        name="USB2",
        priority=2,
        trace_width=NET_CLASS_HIGH_SPEED.trace_width,
        clearance=NET_CLASS_HIGH_SPEED.clearance,
        intra_pair_clearance=0.075,  # Phase 1C: tight intra-pair separation
        coupled_routing=True,  # Phase 2E: opt into coupled engagement
        coupled_continuity_threshold=0.7,  # Phase 2G: relax for short pair
        target_diff_impedance=90.0,  # Phase 3K: USB 2.0 90 Ohm diff
        impedance_tolerance_percent=15.0,
        skew_tolerance_mm=3.0,  # Phase 3H: USB 2.0 HS budget
        length_critical=True,
    )


def usb3_net_class() -> NetClassRouting:
    """USB 3.0 SuperSpeed net class (4 pairs).

    Tighter than USB 2.0: target_diff_impedance=90, coupled_continuity_threshold
    bumped to 0.9 (HSDI demands tight coupling), skew tolerance dropped to
    0.5mm (USB 3.0 spec ~0.4 mm).
    """
    return NetClassRouting(
        name="USB3",
        priority=2,
        trace_width=0.2,
        clearance=0.15,
        intra_pair_clearance=0.10,  # Phase 1C
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.9,  # Phase 2G: HSDI tight coupling
        target_diff_impedance=90.0,  # Phase 3K
        impedance_tolerance_percent=10.0,
        skew_tolerance_mm=0.5,  # Phase 3H: USB 3.0 budget
        length_critical=True,
    )


def pcie_net_class() -> NetClassRouting:
    """PCIe Gen1 net class (2 pairs).

    Phase 3I/3J focal point.  100 Ohm differential, 0.5mm skew is the
    tightest constraint that engages Phase 3I serpentine insertion.
    """
    return NetClassRouting(
        name="PCIe",
        priority=2,
        trace_width=0.2,
        clearance=0.15,
        intra_pair_clearance=0.10,  # Phase 1C
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.85,  # Phase 2G
        target_diff_impedance=100.0,  # Phase 3K: PCIe 100 Ohm
        impedance_tolerance_percent=10.0,
        skew_tolerance_mm=0.5,  # Phase 3H: PCIe Gen1 budget
        length_critical=True,
    )


def mipi_net_class() -> NetClassRouting:
    """MIPI D-PHY net class (2 lanes: CLK + D0).

    Tight skew (0.3mm) and 100 Ohm differential.  Exercises Phase 3I
    serpentine for the CLK pair (which is typically shortest and least
    matched).
    """
    return NetClassRouting(
        name="MIPI",
        priority=2,
        trace_width=0.2,
        clearance=0.15,
        intra_pair_clearance=0.10,  # Phase 1C
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.85,  # Phase 2G
        target_diff_impedance=100.0,  # Phase 3K: MIPI 100 Ohm
        impedance_tolerance_percent=10.0,
        skew_tolerance_mm=0.3,  # Phase 3H: tight MIPI lane budget
        length_critical=True,
    )


def sideband_net_class() -> NetClassRouting:
    """Single-ended sideband (USB_CC1, USB_CC2, MIPI_RST).

    Exercises target_single_impedance (Phase 3K) on a non-diff-pair net,
    which is the orthogonal axis to target_diff_impedance.
    """
    return NetClassRouting(
        name="Sideband",
        priority=4,
        trace_width=0.2,
        clearance=0.15,
        target_single_impedance=50.0,  # Phase 3K: 50 Ohm SE
        impedance_tolerance_percent=15.0,
    )


def build_net_class_map() -> dict[str, NetClassRouting]:
    """Build the canonical net-name -> NetClassRouting mapping.

    This is the single source of truth for both the router (consumed in
    ``route_pcb`` below) and the regression test
    (``tests/test_board_06_diffpair_test.py::test_phase_features_exercised``).
    Importing this function from the test guarantees test/implementation
    parity --- the test cannot drift from the routing config.
    """
    usb2 = usb2_net_class()
    usb3 = usb3_net_class()
    pcie = pcie_net_class()
    mipi = mipi_net_class()
    sideband = sideband_net_class()

    return {
        # USB 2.0
        "USB2_D+": usb2,
        "USB2_D-": usb2,
        # USB 3.0 (4 pairs)
        "USB3_TX1+": usb3,
        "USB3_TX1-": usb3,
        "USB3_RX1+": usb3,
        "USB3_RX1-": usb3,
        "USB3_TX2+": usb3,
        "USB3_TX2-": usb3,
        "USB3_RX2+": usb3,
        "USB3_RX2-": usb3,
        # PCIe (2 pairs)
        "PCIE_TX+": pcie,
        "PCIE_TX-": pcie,
        "PCIE_RX+": pcie,
        "PCIE_RX-": pcie,
        # MIPI (2 lanes)
        "MIPI_CLK+": mipi,
        "MIPI_CLK-": mipi,
        "MIPI_D0+": mipi,
        "MIPI_D0-": mipi,
        # Single-ended sideband
        "USB_CC1": sideband,
        "USB_CC2": sideband,
        "MIPI_RST": sideband,
        # Power
        "VBUS_USB": NET_CLASS_POWER,
        "+3V3": NET_CLASS_POWER,
        "+1V8": NET_CLASS_POWER,
        "+1V2": NET_CLASS_POWER,
        "GND": NET_CLASS_POWER,
    }


# =============================================================================
# Pipeline Steps
# =============================================================================


def create_project(output_dir: Path, project_name: str) -> Path:
    """Create the .kicad_pro file."""
    print("\n" + "=" * 60)
    print("Creating Project File...")
    print("=" * 60)

    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{project_name}.kicad_pro"
    project_data = create_minimal_project(filename)

    project_path = output_dir / filename
    save_project(project_data, project_path)
    print(f"   Project: {project_path}")
    return project_path


def create_schematic(output_dir: Path) -> Path:
    """Generate the schematic."""
    output_path = output_dir / "diffpair_test.kicad_sch"
    generate_schematic.create_diffpair_schematic(output_path)
    return output_path


def create_pcb(output_dir: Path) -> Path:
    """Generate the unrouted PCB."""
    print("\n" + "=" * 60)
    print("Creating PCB...")
    print("=" * 60)
    output_path = output_dir / "diffpair_test.kicad_pcb"
    pcb_content = generate_pcb.generate_pcb()
    output_path.write_text(pcb_content)
    print(f"   PCB: {output_path}")
    print(f"   Nets: {len([n for n in generate_pcb.NETS.values() if n > 0])}")
    print(f"   Diff pairs: {len(generate_pcb.DIFFPAIRS)}")
    return output_path


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """Route the PCB with per-protocol net-class engagement.

    Wires the protocol-specific NetClassRouting instances from
    ``build_net_class_map()`` into the autorouter so each Phase 1-3
    feature is exercised on the appropriate pair set.
    """
    from kicad_tools.router import DesignRules, load_pcb_for_routing
    from kicad_tools.router.optimizer import (
        GridCollisionChecker,
        OptimizationConfig,
        TraceOptimizer,
    )

    print("\n" + "=" * 60)
    print("Routing PCB...")
    print("=" * 60)

    # JLCPCB tier-1 design rules: 0.15mm trace / 0.15mm space / 0.3mm via.
    # Grid must be <= clearance/2 for DRC compliance (0.05 <= 0.15/2 = 0.075 OK).
    # Via diameter chosen tight (0.45mm) so escape vias fit between
    # the 1.0mm-pitch BGA pads without blocking adjacent pad access.
    # This is the same tier the Phase 3K impedance formulas were calibrated
    # against, so the router consumes the same stackup the DRC will check.
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.25,
        via_diameter=0.45,
    )

    # Power and ground nets are handled via copper pours on the inner planes
    # (In1.Cu = GND, In2.Cu = PWR).  Skip them at the trace router so they
    # don't fight for outer-layer corridors.
    skip_nets = ["GND", "VBUS_USB", "+3V3", "+1V8", "+1V2"]

    print(f"\n1. Loading PCB: {input_path}")
    print(
        f"   Grid: {rules.grid_resolution}mm  Trace: {rules.trace_width}mm  Clearance: {rules.trace_clearance}mm"
    )
    print(f"   Skipping pour nets: {skip_nets}")

    router, net_map = load_pcb_for_routing(
        str(input_path),
        skip_nets=skip_nets,
        rules=rules,
    )

    # Install per-protocol net classes.  The router consumes:
    #   - intra_pair_clearance via effective_intra_pair_clearance()
    #     in pathfinder.py / cpp_backend.py
    #   - coupled_routing as the opt-in gate for CoupledPathfinder
    #   - target_diff_impedance via apply_impedance_driven_sizing()
    #   - skew_tolerance_mm via DiffPairLengthTracker / Phase 3I
    #     serpentine (when #2648 lands)
    #   - coupled_continuity_threshold via the DRC rule (passed to
    #     DiffPairRoutingContinuityRule.threshold_map)
    net_class_map = build_net_class_map()

    # Apply impedance-driven sizing (Phase 3K integration point).
    # ``resolve_impedance_for_net_classes`` walks the net-class map and
    # replaces each class whose ``target_diff_impedance`` or
    # ``target_single_impedance`` is set with a copy whose ``trace_width``
    # / ``intra_pair_clearance`` reflect the impedance solver's output for
    # the configured stackup.
    #
    # NOTE for the scaffold: the impedance solver produces wide traces
    # (~0.39mm for 50 Ohm single-ended on F.Cu with JLCPCB tier-1 stackup)
    # which do not fit through the dense pad-pitch corridors at the BGA,
    # QFN, and FFC connectors.  Routing succeeds only on 3/21 nets when
    # the resolved widths are applied.  Trade-off: with the resolved
    # widths most nets fail to route; without them the impedance DRC rule
    # fires on 25 routed traces.  The scaffold ships with the impedance
    # call WIRED but bypassed (declared via the net-class attribute) so:
    #   (a) ``build_net_class_map`` still emits the target_diff_impedance /
    #       target_single_impedance values for AC#6 assertions
    #   (b) ``kct check --mfr jlcpcb`` reports impedance mismatches that
    #       Phase 4N's CI gate will track as the routed-DRC tolerance
    #       baseline
    #   (c) once Phase 3I serpentine + the impedance-aware pathfinder
    #       widening lands, this flag can be flipped to True and the
    #       routed PCB regenerated for a tighter DRC bound
    APPLY_IMPEDANCE_DRIVEN_SIZING = False
    if APPLY_IMPEDANCE_DRIVEN_SIZING:
        try:
            from kicad_tools.manufacturers import get_profile
            from kicad_tools.physics.stackup import Stackup
            from kicad_tools.router.diffpair_impedance import (
                resolve_impedance_for_net_classes,
            )

            stackup = Stackup.jlcpcb_4layer()
            mfr_profile = get_profile("jlcpcb")
            mfr_rules = mfr_profile.get_design_rules(layers=4, copper_oz=1.0)

            resolved_map, mismatch_warnings, clamp_errors = resolve_impedance_for_net_classes(
                net_class_map,
                stackup=stackup,
                design_rules=mfr_rules,
                layer="F.Cu",
            )
            net_class_map = resolved_map
            print("   Impedance sizing applied (stackup: jlcpcb_4layer)")
            if mismatch_warnings:
                print(f"   Stackup mismatch warnings: {len(mismatch_warnings)}")
            if clamp_errors:
                print(f"   Impedance clamp diagnostics: {len(clamp_errors)}")
        except Exception as exc:  # pragma: no cover - degrade gracefully
            print(f"   Impedance sizing skipped: {exc}")
    else:
        print("   Impedance sizing: declared on net classes but not applied to trace widths")
        print("   (resolved widths exceed pad-pitch corridors; see generate_design.py for details)")

    router.net_class_map.update(net_class_map)

    # Engaged pairs --- the diff pair detector (#2558) uses this list as
    # the AUTHORITATIVE pair declarations (overrides suffix inference
    # and KiCad DiffPair group annotations).
    print(f"\n2. Net classes installed: {len(net_class_map)} entries")
    print(f"   Diff pairs declared: {len(generate_pcb.DIFFPAIRS)}")

    print(f"\n3. Board: {router.grid.width}mm x {router.grid.height}mm")
    print(f"   Nets loaded: {len(net_map)}")

    print("\n4. Routing nets...")
    # Issue #2835: pass per-net + outer wall-clock budgets so dense
    # diff-pair pin-access on the BGA-49 sink cannot hang in A*
    # heap-key churn.  These values mirror the recommendation in the
    # Router.route_all() #2794 warning message and the documented
    # default in route_all_negotiated.  See PR #2779 / #2775 for the
    # bracket semantics that make this enforceable.
    router.route_all(per_net_timeout=30.0, timeout=240.0)

    stats_raw = router.get_statistics()
    print(
        f"   Raw: {stats_raw['routes']} routes / {stats_raw['segments']} segments / {stats_raw['vias']} vias"
    )

    print("\n5. Optimizing traces...")
    opt_config = OptimizationConfig(
        merge_collinear=True,
        eliminate_zigzags=True,
        compress_staircase=True,
        convert_45_corners=True,
        minimize_vias=True,
    )
    collision_checker = GridCollisionChecker(router.grid)
    optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

    optimized_routes = []
    for route in router.routes:
        optimized_routes.append(optimizer.optimize_route(route))
    router.routes = optimized_routes

    # Issue #2757: Run the DRC verify-and-nudge pass after trace optimisation.
    # The optimiser can produce chamfered diagonals that graze BGA / QFN /
    # USB-C pads on skipped pour nets (GND, +3V3, +1V2); the in-memory
    # ``drc_verify_and_nudge`` pass surfaces those as ``clearance_pad_segment``
    # candidates and nudges segments perpendicular to repair them.  Without
    # this call the post-route ``kct check`` is the first thing that sees
    # the violations -- by which point the routed PCB is already serialised.
    # See also the equivalent invocations in ``kct route`` (route_cmd.py:1985
    # and 2511) and ``kct optimize`` (route_cmd.py:5184).
    from kicad_tools.router.drc_nudge import drc_verify_and_nudge

    print("\n6. DRC verify-and-nudge pass...")
    nudge_result = drc_verify_and_nudge(router)
    if nudge_result.initial_violations:
        print(f"   {nudge_result.summary()}")
    else:
        print("   No in-router DRC violations detected")

    stats = router.get_statistics()
    print(
        f"\n7. Final: {stats['routes']} routes / {stats['segments']} segments / {stats['vias']} vias"
    )
    print(f"   Total length: {stats['total_length_mm']:.2f}mm")
    print(f"   Nets routed: {stats['nets_routed']}")

    # Stitch routes back into the unrouted PCB.
    original_content = input_path.read_text()
    route_sexp = router.to_sexp()

    if route_sexp:
        output_content = original_content.rstrip().rstrip(")")
        output_content += "\n"
        output_content += f"  {route_sexp}\n"
        output_content += ")\n"
    else:
        output_content = original_content
        print("   Warning: No routes generated!")

    output_path.write_text(output_content)
    print(f"\n8. Routed PCB: {output_path}")

    # Issue #2835: emit copper-pour zones for GND + power nets so the
    # net-status report doesn't flag pour-net pads as "incomplete".
    # Without zones, PR #2777's per-net bounding-box partitioning never
    # runs on this board.  We invoke auto_create_zones_for_pour_nets on
    # the routed PCB so the zones land on the same file kct check / kct
    # export consume.  Layer assignment is stackup-aware (4-layer here):
    # GND -> In1.Cu (full board outline, plane continuity), power nets
    # -> In2.Cu / F.Cu with per-net bounding outlines.
    #
    # We use the board's authoritative ``skip_nets`` declaration (rather
    # than the heuristic ``classify_pour_candidates``) because
    # ``VBUS_USB`` matches both the USB high-speed pattern and the VBUS
    # power pattern and the classifier picks high_speed.  The board's
    # designer intent is that VBUS_USB is a pour net, so we honour the
    # explicit declaration.
    print("\n9. Generating copper-pour zones...")
    try:
        from kicad_tools.router.net_class import NetClass
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        # GND is the sole ground net on this board; the rest of
        # ``skip_nets`` are power rails.
        pour_nets_decl: list[tuple[str, NetClass]] = []
        for net_name in skip_nets:
            if net_name == "GND":
                pour_nets_decl.append((net_name, NetClass.GROUND))
            else:
                pour_nets_decl.append((net_name, NetClass.POWER))
        # JLCPCB tier-1 minimum mask-to-copper clearance is ~0.2mm;
        # inset by 0.5mm for a conservative margin.
        zone_count = auto_create_zones_for_pour_nets(
            output_path, pour_nets_decl, edge_clearance=0.5
        )
        print(f"   Created {zone_count} zone(s) for {[n for n, _ in pour_nets_decl]}")
    except Exception as exc:  # pragma: no cover - degrade gracefully
        print(f"   Zone generation skipped: {exc}")

    total_signal_nets = len([n for n in router.nets if n > 0])
    success = stats["nets_routed"] == total_signal_nets
    if success:
        print(f"   SUCCESS: all {total_signal_nets} signal nets routed")
    else:
        print(f"   PARTIAL: {stats['nets_routed']}/{total_signal_nets} signal nets routed")

    return success


def run_drc(pcb_path: Path) -> bool:
    """Run kct check --mfr jlcpcb on the routed PCB."""
    print("\n" + "=" * 60)
    print("Running DRC (kct check --mfr jlcpcb)...")
    print("=" * 60)

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "check",
                str(pcb_path),
                "--mfr",
                "jlcpcb",
                "--errors-only",
            ],
            capture_output=True,
            text=True,
        )
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"   {line}")
        if result.returncode != 0 and result.stderr:
            print(f"\n   stderr: {result.stderr}")
        return result.returncode == 0
    except Exception as e:
        print(f"\n   Error running DRC: {e}")
        return False


def main() -> int:
    """Entry point.

    Supports the following invocations:

    .. code-block:: bash

        # Default: run all steps (schematic + PCB + route + DRC) into ./output/
        python generate_design.py

        # Custom output dir (positional, backwards compatible)
        python generate_design.py /tmp/my-output

        # Phase 4N (#2660): re-route only for the CI regression gate.
        # ``--step route`` skips schematic + PCB regeneration and re-routes
        # the existing committed unrouted PCB into a new ``*_routed.kicad_pcb``.
        # ``--seed`` is forwarded to ``random.seed()`` before routing for
        # deterministic CI runs (Issue #2589 / Phase 3X.2).
        python generate_design.py --step route --seed 42
    """
    import argparse
    import random

    parser = argparse.ArgumentParser(
        prog="generate_design",
        description="Board 06 (diffpair-test) design generator + Phase 4N CI re-route hook.",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help=(
            "Output directory (default: ./output relative to this script).  "
            "Positional for backwards compatibility with pre-#2660 callers."
        ),
    )
    parser.add_argument(
        "--step",
        choices=["all", "schematic", "pcb", "route"],
        default="all",
        help=(
            "Run only the specified step.  ``route`` re-routes the existing "
            "committed unrouted PCB into ``output/diffpair_test_routed.kicad_pcb``  "
            "without regenerating the schematic or unrouted PCB; used by the "
            "Phase 4N (#2660) CI gate to detect routing-algorithm regressions."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Seed the global ``random`` module with N before routing for "
            "reproducible output (Issue #2589 / Phase 3X.2).  Required by "
            "the Phase 4N CI gate so re-routes are deterministic across "
            "PRs."
        ),
    )
    args = parser.parse_args()

    if args.output_dir is not None:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path(__file__).parent / "output"

    output_dir = output_dir.resolve()

    # Apply seed before any router call so all downstream ``random.shuffle``
    # / ``random.sample`` consumers (escape strategies, MST trial ordering)
    # are deterministic.  See ``kct route --seed`` (#2589) for the same
    # pattern.
    if args.seed is not None:
        random.seed(args.seed)
        print(f"[seed] Seeded global random with --seed {args.seed}")

    try:
        if args.step == "all":
            project_path = create_project(output_dir, "diffpair_test")
            sch_path = create_schematic(output_dir)
            pcb_path = create_pcb(output_dir)
            routed_path = output_dir / "diffpair_test_routed.kicad_pcb"
            route_success = route_pcb(pcb_path, routed_path)
            drc_ok = run_drc(routed_path)

            print("\n" + "=" * 60)
            print("SUMMARY")
            print("=" * 60)
            print(f"\nOutput dir: {output_dir}")
            print(f"  Project:   {project_path.name}")
            print(f"  Schematic: {sch_path.name}")
            print(f"  PCB:       {pcb_path.name}")
            print(f"  Routed:    {routed_path.name}")
            print("\nResults:")
            print(f"  Routing: {'SUCCESS' if route_success else 'PARTIAL'}")
            print(f"  DRC:     {'PASS' if drc_ok else 'FAIL (see above)'}")

            return 0 if route_success else 1

        if args.step == "schematic":
            create_schematic(output_dir)
            return 0

        if args.step == "pcb":
            create_pcb(output_dir)
            return 0

        if args.step == "route":
            # Phase 4N (#2660): the CI gate calls this path to re-route the
            # *committed* unrouted PCB.  Do NOT regenerate the unrouted PCB
            # here -- if the unrouted PCB has drifted from the committed
            # one, that's a separate issue (board scaffolding bug, caught
            # by tests/test_board_06_diffpair_test.py).
            pcb_path = output_dir / "diffpair_test.kicad_pcb"
            if not pcb_path.exists():
                print(
                    f"Error: unrouted PCB not found at {pcb_path}.  Run "
                    "``python generate_design.py --step pcb`` first or "
                    "use ``--step all``.",
                    file=sys.stderr,
                )
                return 1
            routed_path = output_dir / "diffpair_test_routed.kicad_pcb"
            # PARTIAL is the expected outcome today (USB3_TX1+/- blocked by
            # the BGA partner-via escape, tracked in #2677).  As long as the
            # routed PCB was written, the CI gate's DRC check determines
            # pass/fail -- not the route_pcb() "all-or-nothing" boolean.
            # Verify routed_path exists to confirm route_pcb() didn't crash.
            route_pcb(pcb_path, routed_path)
            if not routed_path.exists():
                print(
                    f"Error: routed PCB not written to {routed_path}.",
                    file=sys.stderr,
                )
                return 1
            return 0

        # argparse choices already constrains this, but be explicit.
        print(f"Error: unknown step {args.step!r}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
