"""Regression test: board 01's 2.54mm-pitch nets are not blacklisted (Issue #2910).

Board 01 (voltage divider) ships 1x02 pin headers at 2.54mm pitch.  Their
pads land 0.030mm off the 0.1mm coarse routing grid, which exceeded the
pre-fix ``resolution / 10`` off-grid threshold.  The per-edge
``PADS_OFF_GRID`` emit (router/core.py around line 1677) fired for any
edge involving J1/J2 pads, and the resulting failure entry pushed GND
and VOUT onto the rip-up blacklist via
``route_all_negotiated``'s ``off_grid_nets`` set (router/core.py around
line 5080).

This regression test pins post-fix behaviour:

- Loading board 01 with the same setup the CLI uses (multi-resolution
  grid plan, fine zones applied) yields adaptive-grid coverage for
  every J1/J2 pin header pad.
- The Autorouter's per-edge PADS_OFF_GRID emit no longer fires for
  those pads (verified by checking that ``routing_failures`` after a
  short routing budget contains no ``PADS_OFF_GRID`` entries for GND
  or VOUT).

The test runs a bounded negotiated route (small iteration cap, short
per-net timeout) so it completes in CI-acceptable time.  Full 3/3
routing is asserted in board 01's CI smoke flow elsewhere; this test's
contract is specifically about the off-grid classification, not the
overall router's ability to navigate congestion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_PCB = REPO_ROOT / "boards" / "01-voltage-divider" / "output" / "voltage_divider.kicad_pcb"


@pytest.fixture(scope="module")
def loaded_router():
    """Load board 01 with the same multi-resolution grid plan the CLI uses."""
    from kicad_tools.router.io import load_pcb_for_routing, compute_multi_resolution_plan
    from kicad_tools.router.layers import LayerStack

    router, _ = load_pcb_for_routing(
        str(BOARD_PCB),
        layer_stack=LayerStack.two_layer(),
    )

    # Apply fine zones (the CLI does this from MultiResolutionGridPlan).
    pad_list = list(router.pads.values())
    plan = compute_multi_resolution_plan(
        pad_list,
        clearance=router.rules.trace_clearance,
        board_width=30,
        board_height=25,
    )
    if plan and plan.is_multi_resolution:
        router.fine_zones = list(plan.fine_zones)

    return router


def test_j1_j2_pads_have_adaptive_grid_coverage(loaded_router):
    """All pads of J1 and J2 pin headers must report adaptive-grid coverage.

    Pre-fix, these pads were classified as structurally off-grid based on
    a 0.01mm threshold against the 0.1mm coarse grid (their offset is
    0.030mm).  Post-fix, the adaptive-grid check recognises them via
    either the explicit FineZone (built by compute_multi_resolution_plan
    when the off-grid pad cluster trips its escalation threshold) or the
    implicit pitch-derived sub-grid.
    """
    router = loaded_router

    for key, pad in router.pads.items():
        if pad.ref in ("J1", "J2"):
            assert router._pad_has_adaptive_grid_coverage(pad), (
                f"Pad {key} at ({pad.x},{pad.y}) on 2.54mm-pitch connector "
                f"{pad.ref} should have adaptive-grid coverage"
            )


def test_gnd_and_vout_nets_not_classified_off_grid(loaded_router):
    """``_net_has_off_grid_pads`` returns False for GND and VOUT on board 01.

    Issue #2910 acceptance criterion #2: pads on 2.54mm-pitch pin headers
    (J1, J2) are adaptive-grid-covered, so the nets they participate in
    must NOT be classified as structurally off-grid.

    GND connects J1.2 + R2.2 + J2.2 -- J1.2 and J2.2 are on 2.54mm pitch.
    VOUT connects R1.2 + R2.1 + J2.1 -- J2.1 is on 2.54mm pitch.
    """
    router = loaded_router

    net_by_name = {name: nid for nid, name in router.net_names.items()}
    gnd_id = net_by_name.get("GND")
    vout_id = net_by_name.get("VOUT")
    assert gnd_id is not None, "Expected GND net on board 01"
    assert vout_id is not None, "Expected VOUT net on board 01"

    assert not router._net_has_off_grid_pads(gnd_id), (
        "GND's off-coarse-grid pads (J1.2, J2.2) sit on a 2.54mm-pitch "
        "pin header and are adaptive-grid-covered; _net_has_off_grid_pads "
        "must NOT report this net as structurally off-grid."
    )
    assert not router._net_has_off_grid_pads(vout_id), (
        "VOUT's off-coarse-grid pad (J2.1) sits on a 2.54mm-pitch pin "
        "header and is adaptive-grid-covered; _net_has_off_grid_pads "
        "must NOT report this net as structurally off-grid."
    )


def test_no_pads_off_grid_failures_for_2p54mm_connector_nets(loaded_router):
    """Per-edge ``PADS_OFF_GRID`` emit must not fire for J1/J2-bearing nets.

    Runs a bounded negotiated route and asserts ``routing_failures`` does
    not contain a ``PADS_OFF_GRID`` entry for any pad on J1 or J2.
    Pre-fix, the per-edge emit at router/core.py around line 1677 fired
    whenever an MST/RSMT edge involving a 2.54mm-pitch pad failed -- the
    failure populated ``routing_failures`` which built the rip-up
    blacklist at line 5080.

    This is the core regression-guard for issue #2910: even if the
    overall router has unrelated congestion issues on this board, the
    off-grid blacklist must not silently exclude GND and VOUT from
    recovery.
    """
    router = loaded_router

    # Route as signals (the board's power nets have no copper zones).
    router._pour_nets_without_zones = {"VIN", "VOUT", "GND"}

    # Short-bounded negotiated route -- we only need enough iterations
    # to exercise the per-edge PADS_OFF_GRID emit for any edges that
    # touch J1/J2 pads.
    router.route_all_negotiated(
        max_iterations=3,
        per_net_timeout=15.0,
        timeout=90.0,
    )

    # Collect any PADS_OFF_GRID failures involving J1/J2 pad refs
    off_grid_failures_for_connectors = [
        f for f in router.routing_failures
        if f.reason.startswith("PADS_OFF_GRID")
        and (
            (f.source_pad and "J1" in str(f.source_pad)) or
            (f.source_pad and "J2" in str(f.source_pad)) or
            (f.target_pad and "J1" in str(f.target_pad)) or
            (f.target_pad and "J2" in str(f.target_pad))
        )
    ]

    assert not off_grid_failures_for_connectors, (
        "Per-edge PADS_OFF_GRID emit fired for a J1/J2 pad on board 01. "
        "These 2.54mm-pitch pads have adaptive-grid coverage and must "
        "not be blacklisted from the rip-up loop.  Failures: "
        f"{[(f.net_name, f.source_pad, f.target_pad, f.reason) for f in off_grid_failures_for_connectors]}"
    )
