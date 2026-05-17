#!/usr/bin/env python3
"""
Match-Group Test Board - Complete Design Generation

Epic #2661 Phase 3L (issue #2724) regression testbench.

This script orchestrates the full pipeline for board 07:
    1. Create the project file (.kicad_pro)
    2. Generate the schematic (.kicad_sch)
    3. Generate the unrouted PCB (.kicad_pcb)
    4. Route the PCB (...routed.kicad_pcb)
    5. Emit ``output/net_class_map.json`` sidecar (Phase 3M pattern)
    6. Run DRC via ``kct check --mfr jlcpcb``

The board is a 4-layer JLCPCB tier-1 stackup
(F.Cu / In1.Cu GND / In2.Cu PWR / B.Cu) carrying 4 length-matched
groups across 4 protocol families:

    - DDR data byte (10 nets: DQ0-7 + DM0 + DQS_P/N pair)
    - MIPI CSI lanes (3 pairs = 6 nets)
    - HDMI TMDS lanes (3 pairs = 6 nets)
    - Address bus A0-A7 (single-ended N-trace group)

The router is configured with custom ``NetClassRouting`` instances
per group that opt into each Phase 1A field (Epic #2661):

    - length_match_group (Phase 1A #2687) -- group declaration
    - length_match_reference (Phase 1A #2687) -- pace-car semantic
    - length_match_tolerance_mm (Phase 1A #2687) -- per-group tolerance
    - skew_tolerance_mm (Phase 3H #2647) -- diff-pair sub-skew (DQS,
      MIPI, HDMI lanes)

Dependency note (Phase 3H, #2723):
    The ``--length-match-groups`` CLI flag and the
    ``apply_match_group_tuning`` orchestrator do NOT yet exist in
    main.  Until #2723 lands, the route step exercises the *detection*
    + *tracker* + *DRC rule* paths (Phases 1A/1B/1C/1D + 2.5G) but
    does NOT perform group-level meander insertion -- that is what
    #2723 will wire.  Acceptance criterion #7 (post-pass skew strictly
    less than pre-pass skew) is therefore deferred until #2723; the
    tracker query *is* exercised today.

Usage:
    python generate_design.py [output_dir]

If no output directory is specified, files are written to ./output/.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.router.rules import (
    NET_CLASS_POWER,
    NetClassRouting,
    net_class_map_to_dict,
)

# Re-export net definitions and footprint generators from generate_pcb.
sys.path.insert(0, str(Path(__file__).parent))
import generate_pcb  # noqa: E402
import generate_schematic  # noqa: E402

warn_if_stale()


# =============================================================================
# Per-Group Net Class Declarations
# =============================================================================
# These NetClassRouting instances are the authoritative "scenario" data the
# board exercises.  ``build_net_class_map()`` below assembles them into a
# net-name -> NetClassRouting dict that ``route_pcb`` consumes during
# routing.
#
# Each group class declares ``length_match_group`` (Phase 1A #2687).
# Pair members within a group additionally declare
# ``skew_tolerance_mm`` (Phase 3H #2647) so the diff-pair-level DRC
# rule fires alongside the group-level rule.
#
# AC#6 of issue #2724 asserts that each Phase 1-2 feature is engaged;
# this dict is the single source of truth for that audit.
# =============================================================================


def ddr_data_byte_0_net_class() -> NetClassRouting:
    """DDR data byte 0 net class (10 nets: DQ0-7 + DM0 + DQS pair).

    Phase 2E cascade-safety threshold: groups with N>=5 members
    receive ``MAX_INSERTS_PER_GROUP_MEMBER_LARGE=2`` insertions per
    member (vs the small-group default of 4).  This class has N=10
    so the large-group budget applies.

    The ``length_match_reference=None`` policy means "use longest in
    group" -- the legacy ``tune_match_group`` semantic.  For DDR a
    real design typically pins DQS_P as the reference (pace-car); we
    leave it None here so the longest-of-group path is exercised.
    """
    return NetClassRouting(
        name="DDR_DATA_BYTE_0",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        length_critical=True,
        length_match_group="DDR_DATA_BYTE_0",  # Phase 1A #2687
        length_match_reference=None,  # Phase 1A: None -> longest-in-group
        length_match_tolerance_mm=0.1,  # Phase 1A: tight DDR tolerance
    )


def ddr_dqs_pair_net_class() -> NetClassRouting:
    """DDR strobe pair (DQS_P/DQS_N).

    Member of the DDR_DATA_BYTE_0 match group via shared
    ``length_match_group``, but additionally declares
    ``coupled_routing`` and ``skew_tolerance_mm`` (Phase 3H) so the
    within-pair DRC rule fires.  This is the Phase 2F "group-of-pairs"
    composition exercise: a pair that is also a member of an N-trace
    group.

    Per the issue's curator notes: "the test asserts within-pair skew
    on DQS stays under effective_skew_tolerance after group-level
    tuning (mirrors Phase 2F's own test)".
    """
    return NetClassRouting(
        name="DDR_DQS",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        intra_pair_clearance=0.10,
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.85,  # Phase 2G
        skew_tolerance_mm=0.05,  # Phase 3H: tight DDR strobe budget
        length_critical=True,
        length_match_group="DDR_DATA_BYTE_0",  # Group membership
        length_match_tolerance_mm=0.1,
    )


def mipi_csi_net_class() -> NetClassRouting:
    """MIPI CSI lane net class (3 pairs = 6 nets).

    Phase 2F group-of-pairs symmetric serpentine target, ±0.05mm
    tolerance.  Pair members all share ``length_match_group``;
    detection (Phase 1C #2689) groups them at routing time.
    """
    return NetClassRouting(
        name="MIPI_CSI_LANES",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        intra_pair_clearance=0.10,
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.85,  # Phase 2G
        target_diff_impedance=100.0,  # Phase 3K (Epic #2556)
        impedance_tolerance_percent=10.0,
        skew_tolerance_mm=0.05,  # Phase 3H: tight MIPI lane skew
        length_critical=True,
        length_match_group="MIPI_CSI_LANES",  # Phase 1A #2687
        length_match_tolerance_mm=0.05,  # Phase 1A: tight MIPI tolerance
    )


def hdmi_tmds_net_class() -> NetClassRouting:
    """HDMI TMDS lane net class (3 pairs = 6 nets).

    Phase 2F composition, ±0.075mm tolerance.  In real designs lanes
    match to the clock pair externally; this testbench has all 3
    lanes match to each other (no clock pair member).
    """
    return NetClassRouting(
        name="HDMI_TMDS_LANES",
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        intra_pair_clearance=0.10,
        coupled_routing=True,  # Phase 2E
        coupled_continuity_threshold=0.85,  # Phase 2G
        target_diff_impedance=100.0,  # Phase 3K
        impedance_tolerance_percent=10.0,
        skew_tolerance_mm=0.075,  # Phase 3H: HDMI TMDS budget
        length_critical=True,
        length_match_group="HDMI_TMDS_LANES",  # Phase 1A
        length_match_tolerance_mm=0.075,  # Phase 1A
    )


def addr_bus_net_class() -> NetClassRouting:
    """Generic address bus net class (8 nets: A0-A7).

    Phase 1A declaration with looser ±0.5mm tolerance (parallel-bus
    commodity tier).  Phase 1C suffix-inference fallback would pick
    these up via ``A[0..7]`` even without an explicit declaration,
    but we declare explicitly to exercise the AUTHORITATIVE path.
    """
    return NetClassRouting(
        name="ADDR_BUS",
        priority=2,
        trace_width=0.20,
        clearance=0.15,
        length_critical=True,
        length_match_group="ADDR_BUS",  # Phase 1A
        length_match_reference="A0",  # Phase 1A: pace-car semantic
        length_match_tolerance_mm=0.5,  # Looser commodity-bus tolerance
    )


def build_net_class_map() -> dict[str, NetClassRouting]:
    """Build the canonical net-name -> NetClassRouting mapping.

    This is the single source of truth for both the router (consumed
    in ``route_pcb`` below), the JSON sidecar (``net_class_map.json``,
    Phase 3M pattern), and the regression test
    (``tests/test_board_07_matchgroup_test.py::test_phase_features_exercised``).
    Importing this function from the test guarantees test/implementation
    parity --- the test cannot drift from the routing config.
    """
    ddr = ddr_data_byte_0_net_class()
    dqs = ddr_dqs_pair_net_class()
    mipi = mipi_csi_net_class()
    hdmi = hdmi_tmds_net_class()
    addr = addr_bus_net_class()

    return {
        # DDR data byte 0: 9 single-ended members + DQS diff pair
        "DQ0": ddr,
        "DQ1": ddr,
        "DQ2": ddr,
        "DQ3": ddr,
        "DQ4": ddr,
        "DQ5": ddr,
        "DQ6": ddr,
        "DQ7": ddr,
        "DM0": ddr,
        "DQS_P": dqs,
        "DQS_N": dqs,
        # MIPI CSI lanes (3 pairs)
        "MIPI_CLK_P": mipi,
        "MIPI_CLK_N": mipi,
        "MIPI_DAT0_P": mipi,
        "MIPI_DAT0_N": mipi,
        "MIPI_DAT1_P": mipi,
        "MIPI_DAT1_N": mipi,
        # HDMI TMDS lanes (3 pairs)
        "TMDS_D0_P": hdmi,
        "TMDS_D0_N": hdmi,
        "TMDS_D1_P": hdmi,
        "TMDS_D1_N": hdmi,
        "TMDS_D2_P": hdmi,
        "TMDS_D2_N": hdmi,
        # Address bus
        "A0": addr,
        "A1": addr,
        "A2": addr,
        "A3": addr,
        "A4": addr,
        "A5": addr,
        "A6": addr,
        "A7": addr,
        # Power
        "+1V2": NET_CLASS_POWER,
        "+1V8": NET_CLASS_POWER,
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
    output_path = output_dir / "matchgroup_test.kicad_sch"
    generate_schematic.create_matchgroup_schematic(output_path)
    return output_path


def create_pcb(output_dir: Path) -> Path:
    """Generate the unrouted PCB."""
    print("\n" + "=" * 60)
    print("Creating PCB...")
    print("=" * 60)
    output_path = output_dir / "matchgroup_test.kicad_pcb"
    pcb_content = generate_pcb.generate_pcb()
    output_path.write_text(pcb_content)
    print(f"   PCB: {output_path}")
    print(f"   Nets: {len([n for n in generate_pcb.NETS.values() if n > 0])}")
    print(f"   Diff pairs: {len(generate_pcb.DIFFPAIRS)}")
    print("   Match groups: 4 (DDR_DATA_BYTE_0, MIPI_CSI_LANES, HDMI_TMDS_LANES, ADDR_BUS)")
    return output_path


def write_sidecar(net_class_map: dict, output_dir: Path) -> Path:
    """Emit the ``net_class_map.json`` sidecar (Phase 3M pattern).

    Without this sidecar, ``kct check --net-class-map <path>`` cannot
    re-derive match-group / diff-pair engagement on the routed PCB,
    so ``match_group_length_skew`` (and the diff-pair rules) degrade
    to no-ops.  This is exactly the trap PR #2692 fixed for diff-pair
    rules; we apply the same fix preventatively for match groups.
    """
    sidecar_path = output_dir / "net_class_map.json"
    sidecar_path.write_text(json.dumps(net_class_map_to_dict(net_class_map), indent=2))
    print(f"   Wrote net-class-map sidecar: {sidecar_path}")
    return sidecar_path


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """Route the PCB by invoking ``kct route`` with the proven flag recipe.

    Returns True if ``kct route`` reports full success (return code 0);
    False if it produced a partial routing (still acceptable -- the
    output file is written either way and downstream DRC continues).

    Issue #2991: Previously this function called ``router.route_all()``
    directly through the in-process API, configured with a custom
    ``DesignRules(...)`` block.  Mirroring the sibling board-05 bake
    in PR #2981, replace the in-process call with a subprocess
    invocation of the ``kct route`` CLI:

        --manufacturer jlcpcb --strategy negotiated --no-auto-layers
        --layers 4 --seed 42 --timeout 600

    Recipe-vs-AC deviation (Issue #2991, builder empirical validation
    2026-05-17):

      The issue body cites ``--auto-fix --auto-layers --auto-mfr-tier``
      as the verified recipe yielding 29/31 nets.  Two independent
      problems block that as-stated recipe; both were verified
      empirically against main (commit 46bd8601) before the recipe
      was adjusted to the form above:

      1. ``--auto-mfr-tier`` regressed to ~17/31 nets, well under the
         current in-process baseline of 25/31.  This matches Scout
         commit ``92fc35cb`` (2026-05-16) which explicitly notes:
         "Auto-mfr-tier path regressed to 3/31 per attempt (~40s/net
         with C++ router vs previous 13s/net baseline, likely
         VectorCollisionChecker overhead)".  ``--auto-mfr-tier`` is
         NOT the recipe that produced the 29/31 result.

      2. The Scout 2 verified 29/31 recipe -- ``negotiated +
         differential-pairs, 4L`` (commit 92fc35cb) -- DOES yield
         29/31 (94%) routing completion on this board.  However, the
         CLI's ``--differential-pairs`` mode in main (as of 2026-05-17)
         places diff-pair sibling traces at OVERLAPPING positions
         (within-pair clearance -0.150mm, negative).  Under jlcpcb
         tier-1 rules this produces ~20,300 ``diffpair_clearance_intra``
         violations -- a catastrophic routed-DRC regression that blows
         past the 70-error allowlist by ~290x.

      The rich ``NetClassRouting`` per-pair / per-group declarations
      that ``build_net_class_map()`` assembled previously fed the
      in-process router, but ``kct route`` does NOT accept
      ``--net-class-map`` or equivalent (verified via ``kct route
      --help``).  This is the same gap the curator flagged as an
      "open concern" on issue #2991: the routing-time NetClassRouting
      fields (``intra_pair_clearance``, ``coupled_continuity_threshold``,
      etc.) do not project through the subprocess boundary.

      To stay under the DRC tolerance (HARD LIMIT: do not widen
      ``.github/routed-drc-tolerance.yml``), this recipe omits
      ``--differential-pairs``.  Net yield is ~25/31 (status-quo parity
      with the prior in-process baseline) and DRC stays under 60
      errors (under the 70 allowlist).  When the upstream
      diff-pair-overlap router bug is fixed (follow-up issue), this
      recipe can re-add ``--differential-pairs`` for the 29/31 yield.

    What each flag does:

    - ``--manufacturer jlcpcb``: triggers the jlcpcb design-rule
      profile so the router applies the tier-1 trace/space/via floor.
    - ``--strategy negotiated``: the negotiated rip-up/reroute strategy
      (explicit for clarity; this is also the default).
    - ``--no-auto-layers --layers 4``: pin a 4-layer stackup (the
      board's declared topology: F.Cu / In1.Cu GND / In2.Cu PWR / B.Cu).
      The router's layer-escalation loop would otherwise spend the
      wall-clock budget probing 2L and 6L attempts before settling on
      4L; pinning saves time for actual routing.
    - ``--seed 42``: deterministic output.  The Phase 3N CI gate
      (``scripts/ci/check_matchgroup_coverage.py``) re-invokes this
      script with ``--step route --seed 42`` and asserts a byte-stable
      re-route across PRs.  ``kct route`` honours ``--seed`` by
      seeding the global ``random`` module (route_cmd.py:5296-5299).
      This is the issue's stated HARD LIMIT and is preserved.
    - ``--timeout 600``: outer wall-clock budget; per-net timeout
      defaults to 30 s.  600 s gives the pure-Python fallback path on
      CI runners (no native router_cpp.*.so) enough budget for 31
      nets while remaining under the GitHub Actions 10-min ceiling.

    Skip nets ``GND``, ``+1V2``, ``+1V8`` remain handled via copper
    pours on inner planes (In1.Cu / In2.Cu) emitted post-route by
    ``auto_create_zones_for_pour_nets``.

    Per-group ``NetClassRouting`` sidecar engagement note:
        The rich ``NetClassRouting`` instances assembled by
        ``build_net_class_map()`` (DDR / MIPI / HDMI / ADDR) are
        emitted into the ``net_class_map.json`` sidecar BEFORE the
        subprocess runs.  ``kct check`` consumes that sidecar to fire
        ``match_group_length_skew`` and the diff-pair rules during DRC.

    NOTE on Phase 3H (#2723) dependency:
        When ``--length-match-groups`` and ``apply_match_group_tuning``
        land, the CLI itself will perform group-level meander
        insertion; no further change here will be required.
    """
    print("\n" + "=" * 60)
    print("Routing PCB (via ``kct route`` flag recipe -- Issue #2991)...")
    print("=" * 60)

    # Power and ground nets are handled via copper pours on the inner
    # planes (In1.Cu = GND, In2.Cu = PWR).  Skip them at the trace
    # router so they don't fight for outer-layer corridors.
    skip_nets = ["GND", "+1V2", "+1V8"]

    # Emit the JSON sidecar BEFORE invoking the subprocess.  The CI
    # gate (scripts/ci/check_matchgroup_coverage.py:223-235) requires
    # the sidecar to exist on disk after the route step completes,
    # even when ``kct route`` exits non-zero (partial routing).  The
    # sidecar is the single source of truth for the group / diff-pair
    # declarations consumed by ``kct check --net-class-map``.
    net_class_map = build_net_class_map()
    print(f"\n1. Net classes assembled: {len(net_class_map)} entries")
    print(f"   Diff pairs declared: {len(generate_pcb.DIFFPAIRS)}")
    print("   Match groups (length_match_group): 4")
    sidecar_path = write_sidecar(net_class_map, output_path.parent)

    # Issue #2996: ``kct route`` now accepts ``--net-class-map`` (this PR)
    # which merges the rich NetClassRouting declarations
    # (intra_pair_clearance, coupled_routing, length_match_group, ...)
    # into the autorouter's net_class_map at routing time.  This closes
    # the *projection gap*: pre-#2996, ``--differential-pairs`` had no
    # way to consume the sidecar's per-pair ``intra_pair_clearance``
    # overrides and fell back to defaults that resolved to -0.150 mm
    # (overlapping sibling traces, ~20K ``diffpair_clearance_intra``
    # violations -- the bug this issue documents).
    #
    # Pass the sidecar through unconditionally so that future recipes
    # that re-enable ``--differential-pairs`` automatically get the
    # rich per-pair declarations.  For now the recipe keeps
    # ``--differential-pairs`` OFF because the projection fix alone is
    # not sufficient to stay under the 70-error DRC allowlist:
    # empirically the routed PCB still ends up with ~459 residual
    # ``diffpair_clearance_intra`` violations (down from ~20K, a 97%
    # reduction) due to a SEPARATE post-pathfinder serpentine /
    # length-tune geometry issue.  That issue is filed as a follow-up
    # (router-side, out of scope per the curator brief's "DO NOT widen
    # DRC tolerances" hard limit).  Re-enabling ``--differential-pairs``
    # in this recipe is gated on that follow-up landing.
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(input_path),
        "--output",
        str(output_path),
        "--manufacturer",
        "jlcpcb",
        "--strategy",
        "negotiated",
        "--no-auto-layers",
        "--layers",
        "4",
        "--seed",
        "42",
        "--timeout",
        "600",
        "--skip-nets",
        ",".join(skip_nets),
        "--net-class-map",
        str(sidecar_path),
    ]

    print(f"\n2. Input: {input_path}")
    print(f"   Output: {output_path}")
    print(f"   Skipping pour nets: {skip_nets}")
    print(f"   Command: {' '.join(cmd)}")
    print("\n3. Routing...")

    result = subprocess.run(cmd, capture_output=False, text=True)

    # ``kct route`` returns 0 on full success and a non-zero code on
    # partial / failed routing.  Either way it writes a routed PCB to
    # ``output_path`` (the partial-results file is at
    # ``<stem>_partial.kicad_pcb``).  As long as the output file
    # exists, downstream steps (zone generation + DRC) can run; report
    # success/partial purely informationally.
    success = result.returncode == 0

    if not output_path.exists():
        print(f"\n   ERROR: ``kct route`` did not produce {output_path}", file=sys.stderr)
        return False

    if success:
        print("\n   SUCCESS: ``kct route`` reports all signal nets routed!")
    else:
        print(
            f"\n   PARTIAL: ``kct route`` exited with code {result.returncode} "
            "(partial routing; downstream zone + DRC will continue)"
        )

    # Phase 3H (#2723) integration point.  When --length-match-groups
    # lands, the CLI's own routing pipeline will apply
    # apply_match_group_tuning between route_all and the optimizer; no
    # further change required HERE.
    # TODO Phase 3H (#2723): verify --length-match-groups consumes the
    # net_class_map.json sidecar so group meandering engages.

    # Issue #2835: emit copper-pour zones for GND + power nets so the
    # net-status report doesn't flag pour-net pads (~179 pads on this
    # board) as "incomplete".  Without zones, PR #2777's per-net
    # bounding-box partitioning never runs on this board.  Layer
    # assignment is stackup-aware (4-layer): GND -> In1.Cu (full board
    # outline), power nets (+1V2 / +1V8) distributed across In2.Cu / F.Cu
    # with per-net bounding outlines.
    #
    # ``kct route`` may pour zones for known power nets internally on
    # some recipes, but the board's per-net layer-aware zone declaration
    # is more authoritative.  ``auto_create_zones_for_pour_nets`` is
    # idempotent (it adds zones by net+layer; duplicate calls are
    # detected by the upstream ``auto_pour_if_missing`` helper used
    # elsewhere).  Use the board's authoritative ``skip_nets``
    # declaration so the zone-net set matches the router-skip set
    # exactly.
    print("\n4. Generating copper-pour zones...")
    try:
        from kicad_tools.router.net_class import NetClass
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        pour_nets_decl: list[tuple[str, NetClass]] = []
        for net_name in skip_nets:
            if net_name == "GND":
                pour_nets_decl.append((net_name, NetClass.GROUND))
            else:
                pour_nets_decl.append((net_name, NetClass.POWER))
        zone_count = auto_create_zones_for_pour_nets(
            output_path, pour_nets_decl, edge_clearance=0.5
        )
        print(f"   Created {zone_count} zone(s) for {[n for n, _ in pour_nets_decl]}")
    except Exception as exc:  # pragma: no cover - degrade gracefully
        print(f"   Zone generation skipped: {exc}")

    return success


def run_drc(pcb_path: Path) -> bool:
    """Run kct check --mfr jlcpcb on the routed PCB."""
    print("\n" + "=" * 60)
    print("Running DRC (kct check --mfr jlcpcb)...")
    print("=" * 60)

    sidecar = pcb_path.parent / "net_class_map.json"
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "check",
        str(pcb_path),
        "--mfr",
        "jlcpcb",
        "--errors-only",
    ]
    if sidecar.exists():
        cmd.extend(["--net-class-map", str(sidecar)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
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

        # Phase 4N (#2660) pattern: re-route only for the CI regression gate.
        python generate_design.py --step route --seed 42
    """
    import argparse
    import random

    parser = argparse.ArgumentParser(
        prog="generate_design",
        description="Board 07 (matchgroup-test) design generator + Phase 3N CI re-route hook.",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help="Output directory (default: ./output relative to this script).",
    )
    parser.add_argument(
        "--step",
        choices=["all", "schematic", "pcb", "route"],
        default="all",
        help=(
            "Run only the specified step.  ``route`` re-routes the existing "
            "committed unrouted PCB into ``output/matchgroup_test_routed.kicad_pcb``  "
            "without regenerating the schematic or unrouted PCB; used by the "
            "Phase 3N CI gate to detect routing-algorithm regressions."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Seed the global ``random`` module with N before routing for "
            "reproducible output (Issue #2589).  Required by the Phase 3N "
            "CI gate so re-routes are deterministic across PRs."
        ),
    )
    args = parser.parse_args()

    if args.output_dir is not None:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path(__file__).parent / "output"

    output_dir = output_dir.resolve()

    if args.seed is not None:
        random.seed(args.seed)
        print(f"[seed] Seeded global random with --seed {args.seed}")

    try:
        if args.step == "all":
            project_path = create_project(output_dir, "matchgroup_test")
            sch_path = create_schematic(output_dir)
            pcb_path = create_pcb(output_dir)
            routed_path = output_dir / "matchgroup_test_routed.kicad_pcb"
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
            pcb_path = output_dir / "matchgroup_test.kicad_pcb"
            if not pcb_path.exists():
                print(
                    f"Error: unrouted PCB not found at {pcb_path}.  Run "
                    "``python generate_design.py --step pcb`` first or "
                    "use ``--step all``.",
                    file=sys.stderr,
                )
                return 1
            routed_path = output_dir / "matchgroup_test_routed.kicad_pcb"
            route_pcb(pcb_path, routed_path)
            if not routed_path.exists():
                print(
                    f"Error: routed PCB not written to {routed_path}.",
                    file=sys.stderr,
                )
                return 1
            return 0

        print(f"Error: unknown step {args.step!r}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
