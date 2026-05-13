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

* ``test_usb_diff_pair_routes_via_coupled_pathfinder`` (#2760) — loads
  the committed unrouted PCB and routes it in-process; fast (<10 s).
* ``test_generated_pcb_contains_required_refs`` /
  ``test_pcb_sync_clean_against_schematic`` /
  ``test_route_demo_achieves_minimum_completion`` (#2744) — regenerate
  the board's schematic + PCB from source via subprocess, then assert
  the parts are present, sync is clean, and the demo router completes
  at least ``MIN_FULLY_ROUTED_NETS`` nets.  The regenerate step also
  exercises ``route_demo.py`` end-to-end (~30-60 s wall-clock), so the
  whole module is fast enough for PR-time CI and is NOT marked
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

import re
import subprocess
import sys
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
GEN_PCB_SCRIPT = BOARD_DIR / "generate_pcb.py"
GEN_SCH_SCRIPT = BOARD_DIR / "generate_schematic.py"
ROUTE_DEMO_SCRIPT = BOARD_DIR / "route_demo.py"
OUTPUT_DIR = BOARD_DIR / "output"
SCH_FILE = OUTPUT_DIR / "usb_joystick.kicad_sch"
PCB_FILE = OUTPUT_DIR / "usb_joystick.kicad_pcb"
ROUTED_PCB_FILE = OUTPUT_DIR / "usb_joystick_routed.kicad_pcb"
UNROUTED_PCB = PCB_FILE  # alias for the #2760 fixture below

# Match ``route_demo.py``'s skip list so the in-process fixture below
# exercises exactly the same configuration the demo does.  USB_CC1 /
# USB_CC2 are skipped because the USB-C CC channel cannot be autorouted
# on 2 layers given J1's pad density (per the comment at
# ``route_demo.py:137-140``).
SKIP_NETS = ["VCC", "GND", "VBUS", "USB_CC1", "USB_CC2"]

# Minimum number of multi-pad signal nets the demo router must fully
# connect on the 2-layer board after the May 2026 baseline.  Calibrated
# from Issue #2744 curator finding: "≥9/16 fully routed" floor (the /16
# in the curator note counted all NETS entries including the 5 skipped
# power nets — VCC/VBUS/GND/USB_CC1/USB_CC2 — so the actual routable
# population is 11, not 16).  Post-fix the typical run completes 9/11.
# Floor of 9 leaves zero slack but matches the curator's explicit
# acceptance criterion; if this is flaky on CI we should lower the floor
# and file a follow-up router-quality issue rather than relax the
# curator's stated minimum silently.
MIN_FULLY_ROUTED_NETS = 9

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


def test_xtal2_unblocks_via_conflict_resolution(routed_board_03) -> None:
    """ViaConflictManager wiring exists and is not invoked spuriously on XTAL2.

    Issue #2838 (closes #2761 gap): On ``main`` at the branch point,
    ``Autorouter`` has no ``_via_manager`` attribute at all and the
    PIN_ACCESS retry path in ``route_net`` never invokes via-conflict
    resolution.  This regression test asserts the **structural wiring**
    is in place: after ``route_all_with_diffpairs`` the
    ``Autorouter._via_manager`` attribute exists (lazy property
    initialized to ``None``).

    Issue #2858 (this PR): Fixed the misclassification of XTAL2's
    XTAL1 blocker.  Pre-fix the failure analyser reported a XTAL1
    "via at 0.32mm" blocker that triggered ``ViaConflictManager`` --
    which then found 0 vias to relocate because the real obstacle is
    a XTAL1 *trace* segment (the nearest XTAL1 via is 7.5 mm away).
    Post-fix the classifier correctly reports the blocker as
    ``blocking_type="trace"``, so ``_resolve_via_conflicts_for_net``
    early-returns at the ``has_via_blocker`` gate (``core.py:9018-9023``)
    WITHOUT invoking ``ViaConflictManager``.  Net result: the
    ``via_manager`` lazy property never fires, and
    ``router._via_manager`` remains ``None``.

    Acceptance criteria (Issue #2858):
      1. ``hasattr(router, "_via_manager")`` -- wiring still in place.
      2. XTAL2's failure surfaces a XTAL1 ``"trace"`` blocker (pinned
         by ``test_xtal2_failure_classified_as_trace_blocker``).
      3. ``router._via_manager`` is ``None`` for this board -- the
         resolver is NOT engaged spuriously when the only blockers
         are traces.

    Fully unblocking XTAL2 requires a downstream trace-clearance
    resolver tracked in #2859 -- out of scope for this PR.
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
    # must continue to route post-fix (the via-conflict resolver does
    # not rip-reroute XTAL1 on this board, but if it ever does, XTAL1
    # must still end up routed).
    xtal1_segments = routes_by_net.get(xtal1_id, 0)
    assert xtal1_segments > 0, (
        f"XTAL1 (net {xtal1_id}) routed with 0 segments after the "
        "via-conflict fix.  Either rip-and-reroute removed XTAL1's "
        "route to free U1.3 but failed to find an alternate path on "
        "retry, or the routing order changed and XTAL1 is now being "
        "scheduled after XTAL2 (which would make this test the wrong "
        "test for the regression)."
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

    # Issue #2858 acceptance criterion 3: ``_resolve_via_conflicts_for_net``
    # MUST NOT be invoked spuriously for XTAL2's failure.  Pre-fix the
    # XTAL1 blocker was misclassified as ``"via"`` and triggered the
    # resolver (which then found 0 vias to relocate).  Post-fix the
    # blocker is correctly classified as ``"trace"`` and the resolver
    # early-returns at ``core.py:9023`` -- the lazy ``via_manager``
    # property is never accessed.
    #
    # The XTAL2-specific guarantee comes from
    # ``test_xtal2_failure_classified_as_trace_blocker``, which pins the
    # ``blocking_type == "trace"`` classification on the same failure
    # record.  Together the two tests pin the full Issue #2858 acceptance.
    assert router._via_manager is None, (
        "Issue #2858 acceptance criterion: ``ViaConflictManager`` should "
        "NOT be invoked spuriously for XTAL2 on board 03 anymore.  The "
        "lazy ``via_manager`` property is initialised on first access "
        "from ``_resolve_via_conflicts_for_net``, so a non-None value "
        "here means the resolver ran on at least one net.  XTAL2's "
        "XTAL1 blocker should now classify as 'trace' (see "
        "test_xtal2_failure_classified_as_trace_blocker) and the "
        "``has_via_blocker`` gate at ``core.py:9018-9023`` should "
        "early-return WITHOUT touching ``self.via_manager``.  If this "
        "fails, either: (1) the classifier regressed and the blocker is "
        "back to ``'via'``, or (2) another net on this board has a "
        "genuine via blocker that the resolver legitimately needs to "
        "handle -- in which case file a follow-up and adjust this "
        "assertion to target XTAL2 specifically via ``manager.stats``."
    )


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
        xtal2_segs = sum(
            len(r.segments) for r in router.routes if r.net == xtal2_id
        )
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
def regenerated_board() -> Path:
    """Regenerate the board's schematic + PCB from source and return BOARD_DIR.

    Running the generators from scratch is the most direct way to assert
    "the generator emits the right components" — relying on the committed
    output files would let a generator regression slip through if someone
    happens to manually re-export the PCB later.

    Skips the test if a generator script is missing (e.g. the board was
    relocated by a future refactor without updating this test).
    """
    for required in (GEN_SCH_SCRIPT, GEN_PCB_SCRIPT, ROUTE_DEMO_SCRIPT):
        if not required.exists():
            pytest.skip(f"Required board 03 script missing: {required}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Regenerate schematic FIRST so the PCB sync check below compares
    # against a same-run schematic (avoids false negatives from stale
    # committed output files that pre-date a generator change).
    sch_proc = subprocess.run(
        [sys.executable, str(GEN_SCH_SCRIPT)],
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

    pcb_proc = subprocess.run(
        [sys.executable, str(GEN_PCB_SCRIPT)],
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

    return BOARD_DIR


def test_generated_pcb_contains_required_refs(regenerated_board: Path) -> None:
    """Generator emits C5/C6 + R10/C10/R11/C11/R12 in the PCB.

    Direct text check on the generated ``.kicad_pcb`` file: cheap and
    precisely targets the issue #2744 root cause (PCB generator missed
    the schematic-emitted load caps + joystick filter parts).
    """
    pcb_text = PCB_FILE.read_text()
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
            str(SCH_FILE),
            str(PCB_FILE),
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
    """Extract ``Routed N/M nets`` from route_demo.py output.

    The script emits a summary line of the form::

        PARTIAL: Routed 9/11 nets

    or::

        SUCCESS: All nets routed, DRC passed!

    Returns ``(routed, total)`` or ``None`` if no match.  The "SUCCESS"
    line is also handled by scanning for the earlier
    ``Final Results / Routes created`` block, but in practice board 03
    is in the PARTIAL regime so the simple PARTIAL parser is enough.
    """
    partial = re.search(r"Routed\s+(\d+)/(\d+)\s+nets", stdout)
    if partial:
        return int(partial.group(1)), int(partial.group(2))
    if "SUCCESS: All nets routed" in stdout:
        # All-success branch: count totals from the breakdown above.
        # ``Nets to route: N`` appears once in the load section.
        m = re.search(r"Nets to route:\s+(\d+)", stdout)
        if m:
            total = int(m.group(1))
            return total, total
    return None


def test_route_demo_achieves_minimum_completion(regenerated_board: Path) -> None:
    """route_demo.py routes at least ``MIN_FULLY_ROUTED_NETS`` signal nets.

    Issue #2744 curator floor: "≥9/16 fully routed".  After the May 2026
    baseline (and the generator fix in this PR) the typical post-skip
    routable population is 11 nets (16 NETS entries minus 5 skipped
    power nets), and the demo router consistently lands at 9/11.  This
    test catches a future router or placement regression that drops
    completion below that floor.

    A hard timeout of 600 s guards against router hangs (the curator
    observed a 355 s timeout on net 2/13 in the pre-fix audit; this
    test budget is generous).
    """
    proc = subprocess.run(
        [sys.executable, str(ROUTE_DEMO_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        cwd=str(BOARD_DIR),
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
        f"expected >= {MIN_FULLY_ROUTED_NETS} (issue #2744 floor).  "
        f"This typically indicates either a router-quality regression on "
        f"USB-C-class pad-density boards or a placement change that pushed "
        f"a previously-routable net out of reach."
    )
