"""Regression tests for ``boards/03-usb-joystick/`` USB diff-pair routing.

Issue #2760 — Board 03's ``route_demo.py`` was calling plain
``router.route_all()`` instead of the diff-pair-aware
``router.route_all_with_diffpairs()``.  Without the coupled-pair pass, the
router scheduled USB_D- before USB_D+ via per-net priority ordering and
laid down a USB_D- via at ~0.31mm from J1.A6 / U1.29.  That via blocked
USB_D+'s only remaining pad-access corridor (the J1 USB-C connector flip-
routing diagonal), leaving USB_D+ as a partial 2-of-3-pads stub.  The
stub then produced 4 ``diffpair_clearance_intra`` DRC violations at the
J1 connector area.

The fix is a one-line change in ``boards/03-usb-joystick/route_demo.py``:
call ``route_all_with_diffpairs(DifferentialPairConfig(enabled=True))``
instead of ``route_all()``.  This engages
``CoupledPathfinder.route_differential_pair_coupled`` for the USB pair,
atomically reserving both halves' geometry before any single-net A* runs.

This test pins the post-fix behavior:

- ``router.route_all_with_diffpairs`` actually produces routes for both
  USB_D+ and USB_D- (non-zero segment count for each).
- Neither USB_D+ nor USB_D- appears in ``router.routing_failures``.

The test loads the committed unrouted PCB directly (no subprocess), so it
is fast (<10s on a 2L 80x60mm board) and runs in PR-time CI without the
``slow`` marker.

References:
- ``boards/03-usb-joystick/route_demo.py`` -- the script the fix lives in
- ``src/kicad_tools/router/diffpair_routing.py:1751`` --
  ``route_all_with_diffpairs`` entry point
- ``src/kicad_tools/router/diffpair.py:553`` -- ``DifferentialPairConfig``
- Issue #2760 -- root-cause analysis and acceptance criteria
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router import (
    DesignRules,
    DifferentialPairConfig,
    create_net_class_map,
    load_pcb_for_routing,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "03-usb-joystick"
UNROUTED_PCB = BOARD_DIR / "output" / "usb_joystick.kicad_pcb"

# Match ``route_demo.py``'s skip list so the fixture exercises exactly
# the same configuration the demo does.  USB_CC1/USB_CC2 are skipped
# because the USB-C CC channel cannot be autorouted on 2 layers given
# J1's pad density (per the comment at ``route_demo.py:137-140``).
SKIP_NETS = ["VCC", "GND", "VBUS", "USB_CC1", "USB_CC2"]


@pytest.fixture(scope="module")
def unrouted_pcb_path() -> Path:
    """Verify the committed unrouted board 03 PCB exists."""
    if not UNROUTED_PCB.exists():
        pytest.skip(
            f"Board 03 unrouted PCB not found at {UNROUTED_PCB!s}; "
            "regenerate via `python3 boards/03-usb-joystick/generate_pcb.py`."
        )
    return UNROUTED_PCB


@pytest.fixture(scope="module")
def routed_board_03(unrouted_pcb_path: Path):
    """Load board 03 and route it with diff-pair-aware routing.

    Mirrors the configuration in ``boards/03-usb-joystick/route_demo.py``
    so a regression here is a regression in the demo's behavior as well.

    Returns the populated ``Autorouter`` instance plus the net_map dict.
    """
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

    router, net_map = load_pcb_for_routing(
        str(unrouted_pcb_path),
        skip_nets=SKIP_NETS,
        rules=rules,
    )
    router.net_class_map.update(net_class_map)

    # The fix being regression-tested: diff-pair-aware routing.
    router.route_all_with_diffpairs(
        diffpair_config=DifferentialPairConfig(enabled=True),
    )

    return router, net_map


def test_usb_diff_pair_routes_via_coupled_pathfinder(routed_board_03) -> None:
    """USB_D+ and USB_D- both have non-zero segment count after routing.

    Issue #2760: Without ``route_all_with_diffpairs``, USB_D+ was left as
    a 2-of-3-pads stub (J1.A6 -> J1.B6 only) because the USB_D- via near
    U1.29 blocked the only remaining pad-access corridor.  With the
    coupled pre-pass, ``CoupledPathfinder`` reserves both halves of the
    J1 flip-routing corridor atomically and both nets route to all pads.

    This test asserts the minimum viable success criterion: both halves
    of the differential pair have at least one routed segment.  A
    partial route (e.g., USB_D+'s pre-fix 2-of-3-pads stub) still
    satisfies "has at least one segment", so we ALSO assert that neither
    net appears in ``router.routing_failures`` -- that's what catches
    the partial-route regression.
    """
    router, net_map = routed_board_03

    usb_dp_id = net_map.get("USB_D+")
    usb_dn_id = net_map.get("USB_D-")
    assert usb_dp_id is not None, (
        "USB_D+ net missing from board 03 unrouted PCB -- expected this "
        "is one of the canonical example board's signals.  If the "
        "schematic / PCB generator no longer emits USB_D+, this test "
        "needs to be updated to reflect the new net topology."
    )
    assert usb_dn_id is not None, "USB_D- net missing from board 03 unrouted PCB -- same as above."

    routes_by_net: dict[int, int] = {}
    for route in router.routes:
        routes_by_net[route.net] = routes_by_net.get(route.net, 0) + len(route.segments)

    dp_segments = routes_by_net.get(usb_dp_id, 0)
    dn_segments = routes_by_net.get(usb_dn_id, 0)

    assert dp_segments > 0, (
        f"USB_D+ (net {usb_dp_id}) routed with 0 segments.  This is the "
        "Issue #2760 regression: the CoupledPathfinder pre-pass should "
        "have routed USB_D+ as part of the USB_D+/USB_D- diff pair "
        "before any per-net A* runs.  Check that "
        "boards/03-usb-joystick/route_demo.py is still calling "
        "router.route_all_with_diffpairs(...) with enabled=True, and "
        "that USB_D+/USB_D- are still tagged as high_speed_nets in "
        "the net_class_map (which sets coupled_routing=True)."
    )
    assert dn_segments > 0, (
        f"USB_D- (net {usb_dn_id}) routed with 0 segments -- same "
        "regression pattern as USB_D+; see message above."
    )

    # Failure-list cross-check.  Even when a net has some segments, it
    # can still appear in routing_failures if it failed to reach all
    # pads (the pre-fix behavior for USB_D+: 2-of-3-pads stub).  We
    # require neither net to be in routing_failures so the partial-
    # route regression is also caught.
    failed_net_ids = {failure.net for failure in router.routing_failures}
    assert usb_dp_id not in failed_net_ids, (
        f"USB_D+ (net {usb_dp_id}) appears in router.routing_failures.  "
        "This is the partial-route variant of the Issue #2760 "
        "regression: USB_D+ may have some segments but is not connected "
        "to all of its pads.  Pre-fix this typically manifested as "
        "USB_D+ being a 2-of-3-pads stub between J1.A6 and J1.B6 with "
        "U1.29 unconnected due to a USB_D- via blocking pad access."
    )
    assert usb_dn_id not in failed_net_ids, (
        f"USB_D- (net {usb_dn_id}) appears in router.routing_failures -- "
        "same regression pattern as USB_D+; see message above."
    )
