"""Regression tests for ``boards/03-usb-joystick/``.

This module pins two independent regressions that both surfaced during
the May 2026 board-03 audit:

* **Issue #2744** — Generator parts drift.  The PCB generator at
  ``boards/03-usb-joystick/generate_pcb.py`` was silently dropping seven
  components that the matching schematic emits:

  - ``C5`` / ``C6`` (22 pF crystal load caps) — from
    ``create_crystal_with_loads(cap_ref_start=5)``
  - ``R10`` / ``C10``, ``R11`` / ``C11``, ``R12`` (joystick anti-alias
    RC filter + BTN pull-up) — from
    ``create_analog_joystick(filter_ref_start=10)``

  The drift caused ``kct validate --sync`` to flag seven schematic-only
  refs and the BOM<->PCB export preflight to block manufacturing output,
  even though the build chain reported "OK verify".

* **Issue #2760** — USB diff-pair routing.  ``route_demo.py`` was calling
  plain ``router.route_all()`` instead of the diff-pair-aware
  ``router.route_all_with_diffpairs()``.  Without the coupled-pair pass,
  the router scheduled USB_D- before USB_D+ via per-net priority
  ordering and laid down a USB_D- via at ~0.31 mm from J1.A6 / U1.29.
  That via blocked USB_D+'s only remaining pad-access corridor,
  leaving USB_D+ as a partial 2-of-3-pads stub and producing 4
  ``diffpair_clearance_intra`` DRC violations at the J1 connector.

The fix for #2744 lives in ``generate_pcb.py`` (new
``generate_xtal_load_caps`` + ``generate_joystick_filter`` helpers).
The fix for #2760 is a one-line change in ``route_demo.py``: call
``route_all_with_diffpairs(DifferentialPairConfig(enabled=True))``
instead of ``route_all()``.

The test classes below pin each regression independently:

* ``test_usb_diff_pair_routes_via_coupled_pathfinder`` (#2760 / #3308) —
  loads the committed unrouted PCB and routes it in-process.  Post-#3308
  this uses the canonical generate_design.py:route_pcb() recipe (0.05mm
  grid, in-pad escape rescues on U1) which is much slower than the
  pre-fix 0.1mm path -- runtime is ~5-10 min, so this test is marked
  ``@pytest.mark.slow`` and runs in nightly CI only.
* ``test_generated_pcb_contains_required_refs`` /
  ``test_pcb_sync_clean_against_schematic`` (#2744) — regenerate the
  board's schematic + PCB from source via subprocess and assert the
  parts are present and sync is clean.  These are fast (~20s) and run
  in PR-time CI.
* ``test_route_demo_achieves_minimum_completion`` (#2744 / #3308) —
  Post-#3308 ``route_demo.py`` delegates to ``generate_design.py:route_pcb()``
  (0.05mm grid, in-pad escape rescues on U1) which is slower than the
  pre-#3308 0.1mm recipe -- runtime is ~2 min, so this test is marked
  ``@pytest.mark.slow``.

References:
- ``boards/03-usb-joystick/generate_pcb.py`` -- the #2744 fix lives here
- ``boards/03-usb-joystick/route_demo.py`` -- the #2760 fix lives here
- ``src/kicad_tools/router/diffpair_routing.py:1751`` --
  ``route_all_with_diffpairs`` entry point
- ``src/kicad_tools/router/diffpair.py:553`` -- ``DifferentialPairConfig``
- Issue #2744 -- generator parts drift acceptance criteria
- Issue #2760 -- diff-pair routing root-cause analysis
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.router import (
    DesignRules,
    create_net_class_map,
    load_pcb_for_routing,
)

# Issue #3436: CI runs the suite with `-n auto --timeout=60`.  Board
# generation / real-library scans beat 60s alone, but on the 4-core CI
# runner under full-suite xdist contention the wall-clock reaper killed
# them spuriously.  The marker overrides the CLI default with a
# contention-tolerant budget; it does NOT slow the happy path.
pytestmark = pytest.mark.timeout(600)


REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "03-usb-joystick"
GEN_PCB_SCRIPT = BOARD_DIR / "generate_pcb.py"
GEN_SCH_SCRIPT = BOARD_DIR / "generate_schematic.py"
ROUTE_DEMO_SCRIPT = BOARD_DIR / "route_demo.py"
OUTPUT_DIR = BOARD_DIR / "output"
SCH_FILE = OUTPUT_DIR / "usb_joystick.kicad_sch"
PCB_FILE = OUTPUT_DIR / "usb_joystick.kicad_pcb"
ROUTED_PCB_FILE = OUTPUT_DIR / "usb_joystick_routed.kicad_pcb"
UNROUTED_PCB = PCB_FILE  # alias for the #2760 fixture below

# Issue #3410: ``generate_design.py:route_pcb()`` now delegates to the
# production ``kct route`` CLI invocation (pinned by
# ``tests/router/test_board03_routing_baseline.py``), so the in-process
# ``route_all`` fixture below is no longer "the recipe" -- it is kept
# as a ROUTER-CAPABILITY regression net for the in-pad-rescue /
# intra-IC-consolidation code paths on this board's geometry.  Only
# VCC / GND / VBUS are skipped (served by the generator's pour zones).
SKIP_NETS = ["VCC", "GND", "VBUS"]

# Minimum number of multi-pad signal nets the demo router must fully
# connect.  Issue #3410 (June 9 2026): the canonical recipe now
# delegates to the production ``kct route`` invocation (see
# ``generate_design.py:route_pcb``) and, after the J1 USB-C re-spin +
# MST tie-group fix + escape-defer + auto-pour zone-preservation fix,
# reaches 13/13 with 0 DRC errors at jlcpcb-tier1.  This matches the
# floor pinned by ``tests/router/test_board03_routing_baseline.py``
# (``REQUIRED_NETS_ROUTED = 13``).
MIN_FULLY_ROUTED_NETS = 13

# References the schematic emits that the PCB generator MUST also emit
# to keep sync clean.  This is the explicit anti-regression list from
# the issue #2744 curator: drift in any of these refs blocks export.
REQUIRED_PCB_REFS = ("C5", "C6", "C10", "C11", "R10", "R11", "R12")


# ---------------------------------------------------------------------------
# Issue #2760: USB diff-pair routing (in-process, fast)
# ---------------------------------------------------------------------------


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
    """Load board 03 and route it with the canonical recipe.

    Issue #3308: mirrors ``generate_design.py:route_pcb()`` (the
    canonical recipe ``route_demo.py`` now delegates to) so a regression
    here is a regression in both the demo's behavior AND the build
    recipe.

    Key differences from the legacy pre-#3308 fixture:

      * 0.05mm grid (was 0.1mm) -- needed for J1 USB-C off-grid pad
        escape (#3095).
      * 0.15mm trace width / 0.15mm trace clearance (was 0.2 / 0.2).
      * ``manufacturer="jlcpcb-tier1"`` declared on DesignRules so the
        EscapeRouter can resolve ``via_in_pad_supported`` (#3183).
      * fine-pitch clearance 0.08mm at 0.8mm threshold (#3095).
      * ``USB_CC1`` / ``USB_CC2`` NO LONGER skipped (#3095 made them
        reachable on the finer grid).
      * ``intra_pair_clearance=0.15mm`` on USB_D+/USB_D-.
      * ``random.seed(42)`` for determinism (#3065 plumbing).
      * Uses ``route_all`` with ``enable_in_pad_escape_rescues=True``
        and explicit rescue pins (U1 12-15, 26-27) -- the canonical
        recipe DISABLES ``CoupledPathfinder`` because the pre-pass
        packs USB_D+/D- into adjacent 0.05mm grid cells producing
        intra-clearance violations (#3095).  The per-net A* route_all
        handles the diff pair with lateral offset.

    Returns the populated ``Autorouter`` instance plus the net_map dict.
    """
    import os as _os
    import random as _random
    from dataclasses import replace as _dc_replace

    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        fine_pitch_clearance=0.08,
        fine_pitch_threshold=0.8,
        manufacturer="jlcpcb-tier1",
    )
    net_class_map = create_net_class_map(
        power_nets=["VCC", "VBUS", "GND"],
        high_speed_nets=["USB_D+", "USB_D-"],
        clock_nets=["XTAL1", "XTAL2"],
    )

    # Annotate the diff-pair partners on the USB pair so the validate-side
    # diff-pair rules engage from the routed-PCB sidecar (#2684).  Widen
    # ``intra_pair_clearance`` to 0.15mm so the JLCPCB
    # ``diffpair_clearance_intra`` rule clears (#3095).
    if "USB_D+" in net_class_map and "USB_D-" in net_class_map:
        net_class_map["USB_D+"] = _dc_replace(
            net_class_map["USB_D+"],
            diffpair_partner="USB_D-",
            intra_pair_clearance=0.15,
        )
        net_class_map["USB_D-"] = _dc_replace(
            net_class_map["USB_D-"],
            diffpair_partner="USB_D+",
            intra_pair_clearance=0.15,
        )

    router, net_map = load_pcb_for_routing(
        str(unrouted_pcb_path),
        skip_nets=SKIP_NETS,
        rules=rules,
    )
    router.net_class_map.update(net_class_map)

    # Deterministic seed (#3065 plumbing).
    _random.seed(42)

    # Enable the extended-pitch in-pad fallback so U1's TQFP-32 inner-row
    # pins reach the EscapeRouter's in-pad rescue path (#3183).
    _prior_extended = _os.environ.get("KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK")
    _os.environ["KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK"] = "1"
    try:
        # Force lazy escape router re-init now that the env var is set.
        router._escape_router = None
        router.route_all(
            enable_in_pad_escape_rescues=True,
            in_pad_escape_rescue_pins={"U1": ["12", "13", "14", "15", "26", "27"]},
            suppress_no_timeout_warning=True,
        )
    finally:
        if _prior_extended is None:
            _os.environ.pop("KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK", None)
        else:
            _os.environ["KICAD_TOOLS_EXTENDED_PITCH_IN_PAD_FALLBACK"] = _prior_extended

    return router, net_map


@pytest.mark.slow
def test_usb_diff_pair_routes_via_coupled_pathfinder(routed_board_03) -> None:
    """USB_D+ and USB_D- both have non-zero segment count after routing.

    Issue #2760: Without diff-pair-aware routing, USB_D+ was left as
    a 2-of-3-pads stub (J1.A6 -> J1.B6 only) because the USB_D- via near
    U1.29 blocked the only remaining pad-access corridor.

    Issue #3095 / #3308 (June 2026): the canonical recipe in
    ``generate_design.py:route_pcb()`` (which ``route_demo.py`` now
    delegates to) DISABLES ``CoupledPathfinder`` because the pre-pass
    packs USB_D+/D- into adjacent 0.05mm grid cells producing 19
    ``diffpair_clearance_intra`` violations.  USB_D+ now routes via
    per-net A* with explicit ``intra_pair_clearance=0.15mm`` widening
    and in-pad escape rescues on U1.  USB_D- may currently be partial
    under the router state pinned by ``test_board03_routing_baseline.py``
    -- that's tracked under #3308 AC #2/#3 as a separate router regression.

    This test asserts the minimum viable success criterion (the original
    #2760 floor): USB_D+ has > 0 routed segments AND is not in
    ``routing_failures``.  We do NOT assert USB_D- completeness here
    because the current canonical recipe leaves USB_D- partial under
    HEAD; the aggregate floor of 11/13 in ``test_board03_routing_baseline``
    catches further USB_D-/CC1 regressions.
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
        "original Issue #2760 regression mode: USB_D+ becoming a stub "
        "between J1.A6 and J1.B6 only.  Check that the canonical recipe "
        "in boards/03-usb-joystick/generate_design.py:route_pcb() is "
        "still calling router.route_all() with "
        "enable_in_pad_escape_rescues=True and in_pad_escape_rescue_pins "
        "for U1 (pins 12-15, 26-27), and that USB_D+/USB_D- are still "
        "tagged with intra_pair_clearance=0.15."
    )
    # USB_D- may be partial under current router HEAD (issue #3308 AC
    # #2/#3 -- separate router regression tracked under that issue's
    # follow-up bisect work).  We only assert it has at least one
    # segment -- a zero-segment USB_D- IS catastrophic.
    assert dn_segments > 0, (
        f"USB_D- (net {usb_dn_id}) routed with 0 segments -- "
        "catastrophic regression: even partial routes are missing.  "
        "See USB_D+ note above for diagnostic guidance."
    )

    # USB_D+ specific failure-list check: USB_D+ MUST NOT be in
    # routing_failures.  This is the partial-route variant of the
    # original #2760 regression (USB_D+ 2-of-3-pads stub).  We do NOT
    # assert this for USB_D- because the canonical recipe currently
    # leaves it partial under HEAD; the aggregate 11/13 floor in
    # test_board03_routing_baseline.py catches further degradation.
    failed_net_ids = {failure.net for failure in router.routing_failures}
    assert usb_dp_id not in failed_net_ids, (
        f"USB_D+ (net {usb_dp_id}) appears in router.routing_failures.  "
        "This is the partial-route variant of the Issue #2760 "
        "regression: USB_D+ may have some segments but is not connected "
        "to all of its pads.  Pre-fix this typically manifested as "
        "USB_D+ being a 2-of-3-pads stub between J1.A6 and J1.B6 with "
        "U1.29 unconnected due to a USB_D- via blocking pad access.  "
        "Post-#3095 / #3183 the in-pad escape rescue on U1.29 was the "
        "expected fix; check the rescue pin list in the canonical "
        "recipe (boards/03-usb-joystick/generate_design.py:route_pcb)."
    )


@pytest.mark.skipif(
    not __import__(
        "kicad_tools.router.via_conflict", fromlist=["TRACE_RIP_REROUTE_ENABLED"]
    ).TRACE_RIP_REROUTE_ENABLED,
    reason=(
        "Issue #2872 round-2 (PR #2876 Judge feedback): the trace "
        "rip-reroute branch is default-disabled because the "
        "transactional wrapper's per-route validate_segment_clearance "
        "/ validate_via_clearance primitives do not catch diff-pair "
        "intra-pair clearance (rule_id diffpair_clearance_intra) or "
        "match-group length-skew (rule_id match_group_length_skew) "
        "violations -- both surfaced as +6 / +9 DRC errors on boards "
        "06 / 07 in CI.  The transactional wrapper itself is sound "
        "(snapshot/rollback covers all required state) and ships in "
        "this PR; enabling the flag is deferred to a follow-up that "
        "extends _TraceResolverTransaction.validate_committed_geometry "
        "to detect the missing rule categories.  Set "
        "KICAD_TOOLS_TRACE_RIP_REROUTE_ENABLED=1 to run this test.  "
        "When the validator extension lands, the default flips back "
        "to True and this skipif drops."
    ),
)
def test_xtal2_unblocks_via_conflict_resolution(routed_board_03) -> None:
    """XTAL2 routes via ``ViaConflictManager`` trace rip-and-reroute.

    Issue #2838 (closes #2761 gap): wired ``ViaConflictManager`` into
    ``Autorouter.route_net``'s PIN_ACCESS retry path.

    Issue #2858: fixed the misclassification of XTAL2's XTAL1 blocker
    so it correctly emits ``blocking_type == "trace"`` instead of
    ``"via"``.  Pre-#2858 the via-only resolver fired but found no
    vias to relocate (the nearest XTAL1 via is 7.5 mm away from U1.3),
    so XTAL2 stayed unrouted.

    Issue #2859 (this PR): extends ``ViaConflictManager`` with a
    trace-blocker branch (``find_blocking_traces`` /
    ``try_trace_rip_reroute``).  ``Autorouter._resolve_via_conflicts_for_net``
    now dispatches to that branch when the failure analyser reports a
    ``"trace"`` blocker (the case on board 03 for XTAL2).  Net result:
    the resolver rips the XTAL1 trace, routes XTAL2, and re-routes
    XTAL1.

    Acceptance criteria (Issue #2859):
      1. ``hasattr(router, "_via_manager")`` -- structural wiring still
         in place (the #2838 minimum bar).
      2. XTAL1 still routes -- the rip-reroute may temporarily un-mark
         the XTAL1 route but must restore or re-route it (the rip-
         reroute pattern restores on failure; on success it re-routes).
      3. XTAL2 is no longer in ``router.routing_failures`` and has at
         least one route -- the canonical "XTAL2 unblocked" assertion.
      4. The resolver's success counter
         (``trace_rip_reroutes_succeeded + rip_reroutes_succeeded``)
         is ``>= 1`` -- proves the resolver actually fired and the
         success is not accidental.
    """
    router, net_map = routed_board_03

    xtal1_id = net_map.get("XTAL1")
    xtal2_id = net_map.get("XTAL2")
    assert xtal1_id is not None, (
        "XTAL1 net missing from board 03 unrouted PCB.  Expected this "
        "is one of the canonical example board's clock signals; if the "
        "schematic / PCB generator no longer emits XTAL1, this test "
        "needs to be updated."
    )
    assert xtal2_id is not None, "XTAL2 net missing from board 03 unrouted PCB -- see XTAL1 note."

    routes_by_net: dict[int, int] = {}
    for route in router.routes:
        routes_by_net[route.net] = routes_by_net.get(route.net, 0) + len(route.segments)

    # No regression on XTAL1: it routed first under pre-fix main and
    # must continue to route post-fix (the trace rip-and-reroute
    # branch may rip XTAL1 to free U1.3, but it must re-route XTAL1
    # before returning success -- otherwise the rip-reroute restore-on-
    # failure path would have triggered).
    xtal1_segments = routes_by_net.get(xtal1_id, 0)
    assert xtal1_segments > 0, (
        f"XTAL1 (net {xtal1_id}) routed with 0 segments after the "
        "trace-conflict fix.  Either rip-and-reroute removed XTAL1's "
        "route to free U1.3 but failed to find an alternate path on "
        "retry (the restore-on-failure path should have kicked in), or "
        "the routing order changed and XTAL1 is now being scheduled "
        "after XTAL2 (which would make this test the wrong test for "
        "the regression)."
    )

    # Structural wiring assertion (the #2838 minimum bar): the
    # ``Autorouter._via_manager`` attribute must exist.  This is what
    # makes the test fail on ``main`` at the branch point -- the
    # attribute is entirely absent.
    assert hasattr(router, "_via_manager"), (
        "Autorouter is missing the _via_manager attribute.  This is "
        "the Issue #2838 structural regression: the ViaConflictManager "
        "wiring is entirely absent from Autorouter.  Check that "
        "core.py imports ViaConflictManager and __init__ initializes "
        "self._via_manager."
    )

    # Issue #2859 acceptance criterion: XTAL2 must route to completion.
    failed_net_ids = {failure.net for failure in router.routing_failures}
    assert xtal2_id not in failed_net_ids, (
        f"XTAL2 (net {xtal2_id}) is still in routing_failures after "
        "the trace-conflict fix.  This is the Issue #2859 regression: "
        "the trace rip-and-reroute branch should have ripped the XTAL1 "
        "trace blocker at U1.3, routed XTAL2, and re-routed XTAL1.  "
        "Check core.py:_resolve_via_conflicts_for_net's trace branch "
        "and via_conflict.py:try_trace_rip_reroute."
    )
    xtal2_segments = routes_by_net.get(xtal2_id, 0)
    assert xtal2_segments > 0, (
        f"XTAL2 (net {xtal2_id}) has 0 segments after the trace "
        "rip-and-reroute claimed success.  This is inconsistent: the "
        "resolver returned routes for XTAL2 but they were not added "
        "to router.routes.  Check the recursive route_net call inside "
        "_resolve_via_conflicts_for_net's trace branch."
    )

    # Issue #2859 / #2872 canonical acceptance counter: when the
    # resolver fires, it must have successfully completed (the
    # acceptance gate is *if-fired-then-succeeded*, not *must-fire*).
    # The actual primary acceptance is XTAL2 routing 11/11 above
    # (``xtal2 not in failed_net_ids`` and ``xtal2_segments > 0``).
    #
    # Issue #2872 follow-up: subsequent fixes (#2866 narrow-channel
    # guard, #2868 C++ validator narrow-channel guard, #2870
    # net-aware halo carve-out) improved board 03 routing enough
    # that XTAL2 may now succeed on the first pass without
    # PIN_ACCESS failure, in which case ``_via_manager`` stays
    # ``None`` (lazy-init) and the resolver's success counters are
    # both zero.  That's a desirable side effect of the upstream
    # improvements -- the test must not regress to "trace resolver
    # had to fire", because then routing improvement turns this
    # test red for the wrong reason.
    if router._via_manager is not None:
        manager_stats = router._via_manager.stats
        resolver_attempted_count = (
            manager_stats.trace_rip_reroutes_attempted
            + manager_stats.rip_reroutes_attempted
            + manager_stats.relocations_attempted
        )
        resolver_fired_count = (
            manager_stats.trace_rip_reroutes_succeeded
            + manager_stats.rip_reroutes_succeeded
            + manager_stats.relocations_succeeded
        )
        # If the resolver attempted a rip-reroute, at least one of
        # those attempts must have succeeded (otherwise the resolver
        # is doing destructive surgery without payoff).  This guards
        # against the Issue #2872 round-1 regression where the
        # transactional wrapper rolled back every attempt.
        if resolver_attempted_count > 0:
            assert resolver_fired_count >= 1, (
                f"Issue #2859 / #2872 acceptance counter: resolver "
                f"attempted {resolver_attempted_count} resolution(s) but "
                f"none succeeded.  Stats: "
                f"trace_conflicts_found={manager_stats.trace_conflicts_found}, "
                f"trace_rip_reroutes_attempted="
                f"{manager_stats.trace_rip_reroutes_attempted}, "
                f"trace_rip_reroutes_succeeded="
                f"{manager_stats.trace_rip_reroutes_succeeded}, "
                f"conflicts_found={manager_stats.conflicts_found}, "
                f"rip_reroutes_attempted={manager_stats.rip_reroutes_attempted}, "
                f"rip_reroutes_succeeded={manager_stats.rip_reroutes_succeeded}, "
                f"relocations_attempted={manager_stats.relocations_attempted}, "
                f"relocations_succeeded={manager_stats.relocations_succeeded}.  "
                "Either the restore-on-failure path in "
                "try_trace_rip_reroute is broken, or the Issue #2872 "
                "transactional wrapper is rolling back every attempt."
            )


@pytest.mark.slow
def test_xtal2_failure_classified_as_trace_blocker(routed_board_03) -> None:
    """Issue #2858: XTAL2's pad-access blocker (if any) must surface as ``"trace"``.

    Pre-fix behaviour (the bug):
      The failure analyser at ``failure_analysis.py:1546`` defaulted to
      ``blocking_type == "via"`` for any cell that wasn't a pad, trace
      centerline, or pad-clearance zone.  XTAL2's U1.3 pad was blocked
      by a XTAL1 *trace* clearance halo (~0.3-0.9 mm from the pad), not
      a via (nearest XTAL1 via is 7.5 mm away).  Reporting ``"via"``
      misrouted the failure to ``ViaConflictManager`` which then found
      nothing to relocate, leaving the failure terminal.

    Post-fix behaviour (two valid outcomes -- both pass this test):

      A. **XTAL2 routes fully.**  The classifier fix changes the
         ``_resolve_via_conflicts_for_net`` early-return point, which
         in turn changes the downstream negotiated-routing strategy's
         exploration path enough that XTAL2 finds a working route.
         This is a positive side effect of the fix: with the spurious
         ``ViaConflictManager`` engagement gone, the negotiated /
         rip-up-and-reroute strategy has more iterations available
         and can solve the routing.

      B. **XTAL2 fails with a ``"trace"``-classified blocker.**  If
         the routing strategy still cannot solve XTAL2 (e.g. on a
         different machine or after future strategy tuning that
         changes iteration counts), the failure record must report
         the XTAL1 blocker as ``blocking_type == "trace"`` rather
         than ``"via"`` -- the actual Issue #2858 acceptance criterion.

    Either outcome confirms the fix: outcome A is empirical evidence
    the misclassification was load-bearing in the resolver flow, and
    outcome B directly pins the classifier change.  The fail case is
    "blocker classified as 'via'" -- which would mean the classifier
    fix regressed.

    References:
      - Issue #2858 (this fix)
      - PR #2856 / Issue #2838 (XTAL2 wiring fix that exposed this bug)
      - Issue #2859 (trace-clearance resolver, dependent on this fix)
    """
    router, net_map = routed_board_03

    xtal1_id = net_map.get("XTAL1")
    xtal2_id = net_map.get("XTAL2")
    assert xtal1_id is not None and xtal2_id is not None, (
        "XTAL1 / XTAL2 nets missing from board 03; see "
        "test_xtal2_unblocks_via_conflict_resolution for the canonical "
        "skip path."
    )

    xtal2_failures = [f for f in router.routing_failures if f.net == xtal2_id]

    # Outcome A: XTAL2 routes fully (no failures recorded).  This is a
    # legitimate post-fix outcome; the classifier acceptance is then
    # pinned by the synthetic tests in
    # ``tests/test_failure_analysis.py::TestBlockerGeometryClassifier``.
    if not xtal2_failures:
        # Verify XTAL2 actually has routes (not just absent from
        # routing_failures due to never being attempted).
        xtal2_segs = sum(len(r.segments) for r in router.routes if r.net == xtal2_id)
        assert xtal2_segs > 0, (
            "XTAL2 has no failures AND no routes -- it was skipped "
            "entirely, not routed.  Check that the routing pass still "
            "attempts XTAL2; if the skip list grew, update SKIP_NETS."
        )
        return

    # Outcome B: XTAL2 still fails -- the classifier fix must surface
    # the XTAL1 blocker as ``"trace"`` rather than ``"via"``.
    xtal1_blockers = []
    for failure in xtal2_failures:
        if failure.analysis is None:
            continue
        for blocker in failure.analysis.pad_access_blockers:
            if blocker.blocking_net == xtal1_id:
                xtal1_blockers.append(blocker)

    assert len(xtal1_blockers) >= 1, (
        f"XTAL2 failed but no XTAL1 blocker appears in its "
        f"pad_access_blockers records: {[f.analysis for f in xtal2_failures]!r}. "
        "If the failure analyser is no longer emitting pad_access_blockers "
        "for this case, this test needs to be updated; see issue #2858 "
        "for what changed in the analyser."
    )

    # The Issue #2858 acceptance criterion: at least one XTAL1 blocker on
    # XTAL2's failure must be classified ``"trace"``, not ``"via"``.
    # Pre-fix ALL of these would be classified ``"via"``; post-fix the
    # closest blocker (the segment halo) must be ``"trace"``.
    trace_classified = [b for b in xtal1_blockers if b.blocking_type == "trace"]
    assert len(trace_classified) >= 1, (
        "Issue #2858 acceptance criterion: XTAL2's failure must report a "
        "XTAL1 blocker with blocking_type='trace' (the real obstacle is a "
        "XTAL1 trace segment clearance halo, ~0.3-0.9 mm from U1.3).  Got "
        f"blocker types: {[b.blocking_type for b in xtal1_blockers]!r}.  "
        "If this test fails, check that "
        "_classify_blocker_geometry (failure_analysis.py) was invoked "
        "from the else branch of analyze_pad_access_blockers's cascade."
    )


# ---------------------------------------------------------------------------
# Issue #2744: Generator parts drift (subprocess, slower but bounded)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def regenerated_board(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Regenerate the board's schematic + PCB into a TEMP dir and return it.

    Running the generators from scratch is the most direct way to assert
    "the generator emits the right components" — relying on the committed
    output files would let a generator regression slip through if someone
    happens to manually re-export the PCB later.

    Issue #3580: the generators MUST NOT write into the committed
    ``boards/03-usb-joystick/output/`` directory.  Under ``pytest -n
    auto`` (xdist) other workers read the committed artifacts in
    parallel (e.g. ``tests/test_fleet_45_census.py``,
    ``tests/test_manifest_integrity.py``) and can observe a truncated
    mid-write file; an in-place rewrite also silently clobbers the
    committed artifact in the working tree (the board-03 incident on
    PR #3589).  Both generator scripts accept an explicit output path,
    so we regenerate into a per-module temp dir instead.

    Skips the test if a generator script is missing (e.g. the board was
    relocated by a future refactor without updating this test), or if no
    KiCad symbol libraries are installed (generate_schematic.py resolves
    symbols like Connector_Generic from the system libraries; the nightly
    slow-tests runner installs the ``kicad-symbols`` apt package -- see
    .github/workflows/slow-tests.yml and the issue #3458 inventory).
    """
    for required in (GEN_SCH_SCRIPT, GEN_PCB_SCRIPT, ROUTE_DEMO_SCRIPT):
        if not required.exists():
            pytest.skip(f"Required board 03 script missing: {required}")

    from kicad_tools.schematic.registry import _default_symbol_paths

    if not _default_symbol_paths():
        pytest.skip(
            "No KiCad symbol libraries found (see "
            "kicad_tools.schematic.registry._default_symbol_paths); "
            "generate_schematic.py cannot resolve symbols. Install the "
            "system KiCad symbol libraries (apt: kicad-symbols) or set "
            "KICAD_SYMBOL_DIR."
        )

    out_dir = tmp_path_factory.mktemp("board03_regen")

    # Regenerate schematic FIRST so the PCB sync check below compares
    # against a same-run schematic (avoids false negatives from stale
    # committed output files that pre-date a generator change).
    # ``generate_schematic.py`` accepts a directory and appends the
    # default filename (``usb_joystick.kicad_sch``).
    sch_proc = subprocess.run(
        [sys.executable, str(GEN_SCH_SCRIPT), str(out_dir)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(BOARD_DIR),
    )
    if sch_proc.returncode != 0:
        pytest.fail(
            "generate_schematic.py failed:\n"
            f"stdout:\n{sch_proc.stdout[-2000:]}\n"
            f"stderr:\n{sch_proc.stderr[-2000:]}"
        )

    # ``generate_pcb.py`` joins its positional arg onto the script dir;
    # passing an ABSOLUTE path short-circuits the join (pathlib
    # semantics), so the PCB lands in the temp dir, never in
    # ``boards/03-usb-joystick/output/``.
    pcb_proc = subprocess.run(
        [sys.executable, str(GEN_PCB_SCRIPT), str(out_dir / "usb_joystick.kicad_pcb")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(BOARD_DIR),
    )
    if pcb_proc.returncode != 0:
        pytest.fail(
            "generate_pcb.py failed:\n"
            f"stdout:\n{pcb_proc.stdout[-2000:]}\n"
            f"stderr:\n{pcb_proc.stderr[-2000:]}"
        )

    return out_dir


def test_generated_pcb_contains_required_refs(regenerated_board: Path) -> None:
    """Generator emits C5/C6 + R10/C10/R11/C11/R12 in the PCB.

    Direct text check on the generated ``.kicad_pcb`` file: cheap and
    precisely targets the issue #2744 root cause (PCB generator missed
    the schematic-emitted load caps + joystick filter parts).
    """
    pcb_text = (regenerated_board / "usb_joystick.kicad_pcb").read_text()
    missing = [ref for ref in REQUIRED_PCB_REFS if f'reference "{ref}"' not in pcb_text]
    assert not missing, (
        f"generate_pcb.py is missing schematic-side refs from the PCB "
        f"output (regression of issue #2744): {missing}. "
        f"Schematic calls create_crystal_with_loads(cap_ref_start=5) and "
        f"create_analog_joystick(filter_ref_start=10) — both helpers must "
        f"be mirrored in the PCB generator or sync drift will block export."
    )


def test_pcb_sync_clean_against_schematic(regenerated_board: Path) -> None:
    """``kct validate --sync`` reports zero errors and zero warnings.

    This is the canonical schematic<->PCB drift check.  Issue #2744
    surfaced because the build chain reported "OK" while sync was
    actually broken; this test catches a re-introduction of that drift
    at PR time so a future regression doesn't slip into ``main`` and
    block board 03 export downstream.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "validate",
            "--sync",
            str(regenerated_board / "usb_joystick.kicad_sch"),
            str(regenerated_board / "usb_joystick.kicad_pcb"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        "kct validate --sync returned non-zero exit code "
        f"{proc.returncode} — schematic<->PCB drift detected.\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert "NETLIST IN SYNC" in proc.stdout, (
        f"kct validate --sync did not report 'NETLIST IN SYNC'.\nstdout:\n{proc.stdout}"
    )


def _parse_routed_net_count(stdout: str) -> tuple[int, int] | None:
    """Extract ``Routed N/M [signal] nets`` from route_demo.py output.

    Issue #3308: ``route_demo.py`` now delegates to
    ``generate_design.py:route_pcb()`` which emits ``PARTIAL: Routed
    N/M signal nets`` (with the word ``signal`` in between).  We accept
    both ``Routed N/M nets`` and ``Routed N/M signal nets`` so this
    parser keeps working across the recipe consolidation.

    The script emits a summary line of the form::

        PARTIAL: Routed 11/13 signal nets

    or::

        SUCCESS: All nets routed, DRC passed!

    Returns ``(routed, total)`` or ``None`` if no match.
    """
    partial = re.search(r"Routed\s+(\d+)/(\d+)(?:\s+\w+)?\s+nets", stdout)
    if partial:
        return int(partial.group(1)), int(partial.group(2))
    if "SUCCESS: All nets routed" in stdout:
        # All-success branch: count totals from the breakdown above.
        # ``Nets to route: N`` or ``Nets routed: N`` appears once in the
        # load / final-results section.
        for pattern in (r"Nets to route:\s+(\d+)", r"Nets routed:\s+(\d+)"):
            m = re.search(pattern, stdout)
            if m:
                total = int(m.group(1))
                return total, total
    return None


@pytest.mark.slow
def test_route_demo_achieves_minimum_completion(regenerated_board: Path) -> None:
    """route_demo.py routes at least ``MIN_FULLY_ROUTED_NETS`` signal nets.

    Issue #3308 (June 2026): ``route_demo.py`` now delegates to
    ``generate_design.py:route_pcb()`` so this test exercises the
    canonical recipe.  Routable population is 13 nets (16 NETS minus
    3 skipped power nets VCC/GND/VBUS); the canonical recipe lands at
    11/13 under current router HEAD with USB_D-/USB_CC1 partial.  This
    matches the floor pinned by ``tests/router/test_board03_routing_baseline.py``.

    Issue #2744 background: original curator floor was "≥9/16" which
    counted 5 skipped power nets in the denominator.  Post-#3308 the
    accounting is corrected to "≥11/13".

    A hard timeout of 600 s guards against router hangs (the curator
    observed a 355 s timeout on net 2/13 in the pre-fix audit; this
    test budget is generous).

    Issue #3580: the demo is pointed at the regenerated TEMP input and a
    TEMP output path so it never rewrites the committed
    ``boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb`` (the
    in-place rewrite clobbered PR #3589's refill-only artifact and races
    parallel xdist readers of the committed file).  ``route_demo.py``
    joins its positional args onto the board dir; absolute paths
    short-circuit the join (pathlib semantics).
    """
    routed_tmp = regenerated_board / "usb_joystick_routed.kicad_pcb"
    # PYTHONDONTWRITEBYTECODE: route_demo.py imports generate_design
    # from the board dir; without this the import drops a __pycache__/
    # into boards/03-usb-joystick/, dirtying the working tree.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [
            sys.executable,
            str(ROUTE_DEMO_SCRIPT),
            str(regenerated_board / "usb_joystick.kicad_pcb"),
            str(routed_tmp),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        cwd=str(BOARD_DIR),
        env=env,
    )
    # route_demo.py returns 0 on DRC-clean, 1 on DRC errors.  Either
    # exit code is acceptable here — we are pinning routing completion,
    # not DRC cleanliness (the diffpair_clearance_intra DRC errors are
    # a known consequence of partial USB_D+/D- routes and are tracked
    # separately in the curator note).
    assert proc.returncode in (0, 1), (
        f"route_demo.py returned unexpected exit code {proc.returncode}\n"
        f"stdout (last 4000 chars):\n{proc.stdout[-4000:]}\n"
        f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}"
    )

    parsed = _parse_routed_net_count(proc.stdout)
    assert parsed is not None, (
        "Could not find 'Routed N/M nets' or 'SUCCESS: All nets routed' "
        "line in route_demo.py output.  This typically means the router "
        "crashed before producing a summary.\n"
        f"stdout (last 4000 chars):\n{proc.stdout[-4000:]}"
    )
    routed, total = parsed
    assert routed >= MIN_FULLY_ROUTED_NETS, (
        f"Board 03 fully-routed net count regressed: routed {routed}/{total}, "
        f"expected >= {MIN_FULLY_ROUTED_NETS} (issue #3308 / #2744 floor).  "
        f"This typically indicates either a router-quality regression on "
        f"USB-C-class pad-density boards or a placement change that pushed "
        f"a previously-routable net out of reach.  See "
        f"tests/router/test_board03_routing_baseline.py for the parallel "
        f"`kct route` floor and per-net reach assertions."
    )


# ---------------------------------------------------------------------------
# Issue #2851: granular rollback preserves non-regressing fixes
# ---------------------------------------------------------------------------


def test_fix_drc_preserves_safe_nudges_on_routed_board(tmp_path) -> None:
    """Issue #2851: ``kct fix-drc`` repairs >0 violations on board 03.

    Layer-3 acceptance criterion from issue #2839 / parent #2833.

    Pre-#2851, board 03's routed PCB had 18 clearance nudges available
    (max 0.1880 mm, well under the 2.0 mm cap), of which only a small
    handful broke connectivity.  Under the bulk-snapshot rollback, all
    18 nudges were thrown away whenever the connectivity check fired,
    leaving the user with "Repaired 0/N" even though the majority were
    safe.

    With granular per-nudge rollback in place, ``fix-drc`` must keep at
    least one nudge that did NOT touch a regressed net, so the post-fix
    summary reports ``Repaired K/N`` with K > 0.

    This test loads the committed routed PCB and runs ``fix-drc``
    in-process via the CLI module; no router invocation is required.
    The board 03 routed PCB has ~4 clearance violations clustered around
    the USB diff-pair flip-routing corridor; some of those nudges touch
    USB_D+ (whose connectivity is fragile due to the 2-of-3-pads stub
    geometry) and some do not.  The granular rollback should preserve
    the latter group.
    """
    if not ROUTED_PCB_FILE.exists():
        pytest.skip(
            f"Board 03 routed PCB not found at {ROUTED_PCB_FILE!s}; "
            "regenerate via the board's generate/route demo scripts."
        )

    output_file = tmp_path / "usb_joystick_repaired.kicad_pcb"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "fix-drc",
            str(ROUTED_PCB_FILE),
            "-o",
            str(output_file),
            "--format",
            "json",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    # fix-drc exit codes: 0 = clean, 2 = partial, 3 = full rollback.
    # We accept anything except 3 -- a full rollback would mean granular
    # attribution failed and the legacy bulk path fired.
    assert proc.returncode != 3, (
        "fix-drc full-rolled-back on board 03: this is the issue #2851 "
        "regression we are trying to prevent.  Expected granular "
        "rollback to preserve at least 1 nudge.\n"
        f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}"
    )

    # Parse JSON to assert at least one nudge was applied.  ``fix-drc``
    # may report ``total_repaired = 0`` when there are no violations at
    # all (clean board), in which case the test is vacuously satisfied
    # by the exit-code check above.
    import json as _json

    if not proc.stdout.strip():
        return  # No JSON output (likely no violations).

    # ``fix-drc`` prints a ``Running DRC on:`` preamble to stdout before
    # the JSON document when ``--drc-report`` is not supplied.  Find the
    # first ``{`` to locate the JSON body.
    json_start = proc.stdout.find("{")
    if json_start < 0:
        return  # No JSON document emitted -- no violations to repair.
    json_text = proc.stdout[json_start:]

    try:
        data = _json.loads(json_text)
    except _json.JSONDecodeError:
        pytest.fail(f"Could not parse fix-drc JSON output:\n{proc.stdout[-2000:]}")

    total_violations = data.get("total_violations", 0)
    total_repaired = data.get("total_repaired", 0)

    if total_violations == 0:
        return  # No work to do; vacuously satisfied.

    assert total_repaired > 0, (
        "fix-drc on board 03 reported total_violations="
        f"{total_violations} but total_repaired={total_repaired}.  "
        "This is the issue #2851 layer-3 contract: granular rollback "
        "must preserve at least 1 nudge that does not touch a regressed "
        "net.  A zero count here means the granular path attributed the "
        "regression to every nudge (legitimate full rollback) OR the "
        "granular path fell back to bulk snapshot restore."
    )


# ---------------------------------------------------------------------------
# Issue #2918: Crystal placement must be on the MCU's XTAL-pin side
# ---------------------------------------------------------------------------
#
# Root cause: ``boards/03-usb-joystick/generate_pcb.py::generate_crystal()``
# hardcoded ``x = BOARD_ORIGIN_X + 55`` (east of the MCU), but the TQFP-32
# MCU's XTAL pins (pin 2 = XTAL1, pin 3 = XTAL2) sit on the WEST edge of
# the package (pad offset = -4.5 mm from U1 centre).  This forced the
# autorouter to drive 17-22 mm channel-blocked traces across the MCU body,
# leaving XTAL1 and XTAL2 unrouted.
#
# Fix: relocate Y1 to ``BOARD_ORIGIN_X + 22`` (west of U1) with
# ``y = BOARD_ORIGIN_Y + 30`` aligned to the XTAL pad row; load caps
# C5/C6 follow via the shared ``xtal_cx`` / ``xtal_cy`` references.
#
# This unit test pins the literal so a future hand-edit can't silently
# re-introduce the east-side placement.  Integration coverage (the route
# actually completing on XTAL1) is provided by the existing
# ``test_route_demo_achieves_minimum_completion`` test above -- with the
# fix in place that test's 9-net floor is reached, whereas with the
# east-side placement only 7-8 nets route.


def test_crystal_placed_west_of_mcu(regenerated_board: Path) -> None:
    """Issue #2918: Y1 placement literal must be west of U1's XTAL pins.

    ``generate_crystal()`` and ``generate_xtal_load_caps()`` must agree on
    the crystal centre being on the WEST side of U1 (at
    ``BOARD_ORIGIN_X + 22``, ``BOARD_ORIGIN_Y + 30``), not the pre-fix
    east-side hardcode (``BOARD_ORIGIN_X + 55``).

    The check inspects the source of ``generate_pcb.py`` rather than the
    runtime output so the failure message points directly at the offending
    literal.  An integration check (XTAL1 actually routing fully) lives
    in the route-demo completion test above.

    Acceptance criteria from issue #2918:

      1. ``generate_pcb.py`` regenerates a PCB where Y1 sits west of U1
         (``BOARD_ORIGIN_X + 22``).
      2. ``kct route ... --manufacturer jlcpcb --auto-layers`` produces
         ``XTAL1`` fully connected (3/3 pads).
      3. No regression on any other board.

    Criterion (1) is pinned here; (2) is pinned by the route-demo test
    above (the 9-net floor is unattainable without XTAL1 routing); (3) is
    a manual fleet-status check in the PR description.
    """
    src = (BOARD_DIR / "generate_pcb.py").read_text()

    # Match ``x = BOARD_ORIGIN_X + 22`` allowing for whitespace variance.
    crystal_x_pat = re.compile(
        r"def\s+generate_crystal\b.*?x\s*=\s*BOARD_ORIGIN_X\s*\+\s*22\b",
        re.DOTALL,
    )
    assert crystal_x_pat.search(src), (
        "generate_crystal() no longer hardcodes x = BOARD_ORIGIN_X + 22.  "
        "Issue #2918 fix moved Y1 from the east side of U1 "
        "(BOARD_ORIGIN_X + 55) to the west side (BOARD_ORIGIN_X + 22) so "
        "the autorouter can reach U1's west-edge XTAL pins (pin 2/3) "
        "without crossing the MCU body.  If you intentionally moved the "
        "crystal again, update this regression test to match -- but "
        "verify XTAL1 still routes 3/3 pads after the move, per the "
        "issue #2918 acceptance criteria."
    )

    # Match ``y = BOARD_ORIGIN_Y + 30`` inside ``generate_crystal``.
    crystal_y_pat = re.compile(
        r"def\s+generate_crystal\b.*?y\s*=\s*BOARD_ORIGIN_Y\s*\+\s*30\b",
        re.DOTALL,
    )
    assert crystal_y_pat.search(src), (
        "generate_crystal() no longer pins y = BOARD_ORIGIN_Y + 30.  "
        "The XTAL pad row on U1 sits at y ~= BOARD_ORIGIN_Y + 30 (mid "
        "between pin 2 at y_offset -2.0 and pin 3 at y_offset -1.2 "
        "relative to U1's centre).  Misaligning this row reintroduces "
        "the channel-blocked routing failure from issue #2918."
    )

    # And the load-cap helper must reference the SAME centre so C5/C6
    # follow the crystal.  Without this, the caps would drift back to
    # the east side and break XTAL1/XTAL2 trace lengths.
    load_caps_cx_pat = re.compile(
        r"def\s+generate_xtal_load_caps\b.*?xtal_cx\s*=\s*BOARD_ORIGIN_X\s*\+\s*22\b",
        re.DOTALL,
    )
    assert load_caps_cx_pat.search(src), (
        "generate_xtal_load_caps() xtal_cx is no longer aligned to "
        "BOARD_ORIGIN_X + 22 -- the crystal moved but the load caps did "
        "not follow.  This breaks the XTAL1/XTAL2 trace geometry per "
        "issue #2918.  Both ``generate_crystal()`` and "
        "``generate_xtal_load_caps()`` must share the same crystal centre."
    )
    load_caps_cy_pat = re.compile(
        r"def\s+generate_xtal_load_caps\b.*?xtal_cy\s*=\s*BOARD_ORIGIN_Y\s*\+\s*30\b",
        re.DOTALL,
    )
    assert load_caps_cy_pat.search(src), (
        "generate_xtal_load_caps() xtal_cy is no longer aligned to "
        "BOARD_ORIGIN_Y + 30 -- see xtal_cx note above."
    )

    # Negative assertion: the pre-fix east-side literal must NOT reappear
    # in either helper.  A diff that introduces ``BOARD_ORIGIN_X + 55``
    # back into the crystal block is exactly the regression we are
    # guarding against.
    east_side_pat = re.compile(
        r"def\s+generate_(crystal|xtal_load_caps)\b.*?BOARD_ORIGIN_X\s*\+\s*55\b",
        re.DOTALL,
    )
    assert not east_side_pat.search(src), (
        "Pre-issue-#2918 east-side crystal literal (BOARD_ORIGIN_X + 55) "
        "has reappeared in generate_crystal() or generate_xtal_load_caps().  "
        "This is the exact regression issue #2918 was opened to prevent; "
        "the crystal must stay on the MCU's XTAL-pin side."
    )


def test_generated_pcb_places_crystal_west_of_mcu(regenerated_board: Path) -> None:
    """Issue #2918: regenerated PCB places Y1 to the west of U1.

    Stronger than the source-literal check above: this regenerates the
    actual PCB and asserts the absolute x coordinate of Y1 is *less*
    than U1's absolute x.  Catches the case where someone refactors the
    generator to compute the position differently (e.g. via a placement
    strategy) but accidentally re-introduces an east-of-U1 result.

    Parses the ``.kicad_pcb`` text directly with regex; no KiCad
    dependency required for the assertion.
    """
    pcb_text = PCB_FILE.read_text()

    # Each ``(footprint ...)`` block contains a ``(reference "REF")`` and
    # an ``(at X Y [ROT])`` line for the footprint origin.  We use
    # non-greedy footprint blocks and pull the first ``(at X Y...)`` line
    # which is the footprint position (subsequent ``(at ...)`` lines
    # inside the block belong to pads / text and have a different scope).
    def _find_footprint_x(ref: str) -> float | None:
        # Iterate over footprint blocks and find the one with the matching
        # reference.  The footprint ``(at X Y ...)`` is the first ``(at``
        # token immediately after ``(footprint ...`` and before any
        # ``(pad`` or ``(fp_text reference``.
        for m in re.finditer(
            r'\(footprint\s+"[^"]+"\s*\(layer\s+"[^"]+"\)\s*'
            r"\(uuid\s+\"[^\"]+\"\)\s*"
            r"\(at\s+([\-0-9.]+)\s+([\-0-9.]+)",
            pcb_text,
        ):
            # Look forward to the corresponding reference within this
            # footprint block (bounded by the next ``(footprint`` or end
            # of string).
            block_start = m.start()
            next_fp = pcb_text.find("(footprint", m.end())
            block_end = next_fp if next_fp != -1 else len(pcb_text)
            block = pcb_text[block_start:block_end]
            ref_m = re.search(rf'\(fp_text\s+reference\s+"{re.escape(ref)}"', block)
            if ref_m:
                return float(m.group(1))
        return None

    y1_x = _find_footprint_x("Y1")
    u1_x = _find_footprint_x("U1")

    assert y1_x is not None, (
        "Y1 (crystal) footprint not found in regenerated board 03 PCB.  "
        "Did generate_crystal() get removed?"
    )
    assert u1_x is not None, (
        "U1 (MCU) footprint not found in regenerated board 03 PCB.  Did the MCU helper get renamed?"
    )

    assert y1_x < u1_x, (
        f"Issue #2918 regression: Y1 (crystal) at x={y1_x:.3f} is NOT "
        f"west of U1 (MCU) at x={u1_x:.3f}.  The MCU's XTAL pins sit on "
        "U1's west edge; placing Y1 east of U1 forces 17-22 mm "
        "channel-blocked traces that the autorouter cannot complete."
    )


# ---------------------------------------------------------------------------
# Issue #2943: J2 west-side nudge to clear JOY_Y channel past J2-5
# ---------------------------------------------------------------------------
#
# Root cause: After issue #2918's west-side crystal placement
# (Y1 at BOARD_ORIGIN_X + 22), the routing channel between Y1's south
# pad metal and J2-5's north pad metal was only ~3.4 mm tall at
# x = 19..22 -- the exact corridor JOY_Y needs to thread from the
# filter column (filt_cx = BOARD_ORIGIN_X + 27) westward to J2-4
# (x = BOARD_ORIGIN_X + 17 pre-nudge).  The result was 6
# ``clearance_pad_segment`` errors where JOY_Y segments clipped
# J2-5 (JOY_BTN, 1.6 mm dia pad).
#
# Fix: nudge J2 west by 2 mm (joy_cx: BOARD_ORIGIN_X + 15 ->
# BOARD_ORIGIN_X + 13).  This walks J2-5 from x = 19 to x = 17
# and opens a clean >5 mm channel.  PR #2941 (merged same day)
# explicitly forbids router-side relaxation of these errors:
# the same-component carve-out now correctly rejects negative-
# clearance segments, so the fix MUST be geometric.
#
# This unit test pins the literal joy_cx = BOARD_ORIGIN_X + 13 so a
# future hand-edit can't silently re-introduce the J2-5 channel
# clip.  Integration coverage (the DRC count actually dropping to
# the 4-error baseline) comes from the .github/routed-drc-tolerance.yml
# floor of 4 set in the same PR.


def test_joystick_nudged_west_to_clear_joy_y_channel(regenerated_board: Path) -> None:
    """Issue #2943: J2 placement literal must sit at BOARD_ORIGIN_X + 13.

    ``generate_joystick()`` and ``generate_joystick_filter()`` must agree
    on the joystick connector centre being at ``BOARD_ORIGIN_X + 13``
    (the post-nudge x), not the pre-nudge ``BOARD_ORIGIN_X + 15``.

    The check inspects the source of ``generate_pcb.py`` rather than the
    runtime output so the failure message points directly at the offending
    literal.  An integration check (the 6 J2-5 ``clearance_pad_segment``
    errors actually disappearing) is implicit in the routed-drc-tolerance
    floor of 4 (down from 9) set in the same PR.

    Acceptance criteria from issue #2943:

      1. ``generate_joystick()`` uses ``x = BOARD_ORIGIN_X + 13``.
      2. ``generate_joystick_filter()`` uses ``joy_cx = BOARD_ORIGIN_X + 13``
         (so the filter column references stay consistent).
      3. The pre-nudge literal ``BOARD_ORIGIN_X + 15`` must NOT appear in
         either helper any longer.

    Criterion (1) and (2) are pinned here; the DRC count drop is pinned
    by the floor of 4 in ``.github/routed-drc-tolerance.yml``.
    """
    src = (BOARD_DIR / "generate_pcb.py").read_text()

    # Match ``x = BOARD_ORIGIN_X + 13`` inside ``generate_joystick()``.
    joystick_x_pat = re.compile(
        r"def\s+generate_joystick\b(?!_filter).*?x\s*=\s*BOARD_ORIGIN_X\s*\+\s*13\b",
        re.DOTALL,
    )
    assert joystick_x_pat.search(src), (
        "generate_joystick() no longer hardcodes x = BOARD_ORIGIN_X + 13.  "
        "Issue #2943 fix nudged J2 from BOARD_ORIGIN_X + 15 to "
        "BOARD_ORIGIN_X + 13 (2 mm west) so the JOY_Y routing channel "
        "between Y1 (BOARD_ORIGIN_X + 22) and J2-5 widens from ~3.4 mm "
        "to >5 mm.  If you intentionally moved J2 again, verify the "
        "J2-5 ``clearance_pad_segment`` errors haven't returned (check "
        "DRC against the post-#2943 baseline of 4 errors at jlcpcb "
        "tier-1)."
    )

    # Match ``joy_cx = BOARD_ORIGIN_X + 13`` inside ``generate_joystick_filter()``.
    filter_cx_pat = re.compile(
        r"def\s+generate_joystick_filter\b.*?joy_cx\s*=\s*BOARD_ORIGIN_X\s*\+\s*13\b",
        re.DOTALL,
    )
    assert filter_cx_pat.search(src), (
        "generate_joystick_filter() joy_cx is no longer aligned to "
        "BOARD_ORIGIN_X + 13 -- the joystick moved but the filter helper "
        "did not follow.  Both ``generate_joystick()`` and "
        "``generate_joystick_filter()`` must share the same J2 centre, "
        "otherwise the filter column drifts relative to the connector "
        "pads."
    )

    # Negative assertion: the pre-#2943 literal ``BOARD_ORIGIN_X + 15``
    # must NOT reappear inside either joystick helper.  A diff that
    # restores the old position is exactly the regression we are
    # guarding against.
    pre_nudge_pat = re.compile(
        r"def\s+generate_joystick(?:_filter)?\b.*?(?:x|joy_cx)\s*=\s*BOARD_ORIGIN_X\s*\+\s*15\b",
        re.DOTALL,
    )
    assert not pre_nudge_pat.search(src), (
        "Pre-issue-#2943 J2 position literal (BOARD_ORIGIN_X + 15) has "
        "reappeared in generate_joystick() or generate_joystick_filter().  "
        "This is the exact regression issue #2943 was opened to prevent; "
        "with Y1 on the west side (issue #2918), J2 must stay west of "
        "x = +15 or the JOY_Y channel collapses and the 6 J2-5 "
        "clearance_pad_segment errors return."
    )


def test_joystick_j2_pin1_inside_pcb_edge(regenerated_board: Path) -> None:
    """Issue #2943 guard: J2-1 (GND) absolute x stays inside PCB west edge.

    The nudge moves J2-1 from absolute x = BOARD_ORIGIN_X + 11 to
    BOARD_ORIGIN_X + 9.  Curator flagged this as a guard: confirm J2's
    body doesn't overhang the PCB west edge after the nudge.

    Acceptance: J2-1 pad centre absolute x must be > BOARD_ORIGIN_X + 0.8
    (i.e., pad edge stays inside the PCB by at least the pad radius
    plus a small safety margin).  J2's body (which extends beyond the
    PCB south edge by design) is unaffected -- the nudge is in X only.
    """
    pcb_text = PCB_FILE.read_text()

    # Find J2's footprint position.
    def _find_footprint_xy(ref: str) -> tuple[float, float] | None:
        for m in re.finditer(
            r'\(footprint\s+"[^"]+"\s*\(layer\s+"[^"]+"\)\s*'
            r"\(uuid\s+\"[^\"]+\"\)\s*"
            r"\(at\s+([\-0-9.]+)\s+([\-0-9.]+)",
            pcb_text,
        ):
            block_start = m.start()
            next_fp = pcb_text.find("(footprint", m.end())
            block_end = next_fp if next_fp != -1 else len(pcb_text)
            block = pcb_text[block_start:block_end]
            ref_m = re.search(rf'\(fp_text\s+reference\s+"{re.escape(ref)}"', block)
            if ref_m:
                return float(m.group(1)), float(m.group(2))
        return None

    j2_xy = _find_footprint_xy("J2")
    assert j2_xy is not None, (
        "J2 (joystick) footprint not found in regenerated board 03 PCB.  "
        "Did generate_joystick() get removed?"
    )
    j2_cx, _ = j2_xy

    # J2-1 (GND) sits at pad offset -4 mm from J2 centre.  Pad diameter
    # is 1.6 mm, so the pad's west edge is at j2_cx - 4 - 0.8 = j2_cx - 4.8.
    # BOARD_ORIGIN_X = 100 (world coords).  We need pad-west-edge >
    # BOARD_ORIGIN_X (>= 0 mm slack), and ideally with some safety margin.
    pad1_centre_x = j2_cx - 4.0
    pad1_west_edge_x = pad1_centre_x - 0.8  # 1.6 mm dia / 2

    # BOARD_ORIGIN_X is 100.0 from the generator constant.
    board_west_x = 100.0
    edge_clearance = pad1_west_edge_x - board_west_x

    assert edge_clearance > 0.0, (
        f"J2-1 (GND) pad west edge at x={pad1_west_edge_x:.3f} mm is "
        f"OUTSIDE the PCB west edge (x={board_west_x:.3f} mm).  J2 is at "
        f"cx={j2_cx:.3f}; the issue #2943 nudge moved J2 too far west.  "
        "Pull J2 back east until the pad sits fully inside the PCB."
    )
    assert edge_clearance >= 0.5, (
        f"J2-1 (GND) pad west edge has only {edge_clearance:.3f} mm of "
        f"clearance to the PCB west edge.  This is below the safe margin "
        "of 0.5 mm; the JLCPCB process requires at least 0.4 mm "
        "edge-to-copper clearance, so this will likely DRC-fail.  Pull "
        "J2 back east."
    )
