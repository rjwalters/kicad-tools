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
