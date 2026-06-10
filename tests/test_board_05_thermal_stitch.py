"""Regression guard: board-05 thermal-via stitching covers Q1-Q6 heat-sink pads.

Issue #2901 (umbrella #2746 child 4), acceptance criterion 4 — pin the
post-#2903 thermal-stitch behaviour on the canonical board-05 regression
vehicle.  Each of the six TO-220 power MOSFETs (Q1-Q6, IRLZ44N) must be
able to receive ≥4 thermal vias under its drain (pad 2) heat-sink pad
when the thermal stitch CLI mode is run with appropriate parameters.

The committed routed PCB on disk does NOT yet carry thermal vias --
``design.py`` does not invoke ``kct stitch --thermal`` today (that
pipeline integration is tracked separately).  This test exercises the
thermal-stitch primitive against a temporary copy of the committed
routed PCB and asserts the per-MOSFET via count meets the AC.

When the board-05 pipeline eventually opts in to thermal stitching, this
test will continue to function as a regression guard against the stitch
implementation -- the committed routed PCB will carry the vias, but the
dry-run assertion here measures the underlying primitive directly so the
test stays meaningful regardless of pipeline integration state.

**Why a separate file from ``tests/test_thermal_stitch.py``**: that file
tests the thermal-stitch primitives on synthetic 2-FET fixtures with
controlled geometry.  This test is board-05-specific: it exercises the
real DRV8313 MOSFET context with real routed traces as clearance
obstacles, which is a strictly larger surface than the synthetic
fixture covers.  A regression in clearance-handling for crowded real
boards is caught here but not in the unit-level tests.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import run_thermal_stitch

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
ROUTED_PCB = BOARD_DIR / "output" / "bldc_controller_routed.kicad_pcb"

# The six TO-220 IRLZ44N MOSFETs.  Their drain (pad 2) is the heat-sink
# tab and is the AC #4 target for thermal-via stitching.  Pad 2 nets:
#   Q1, Q3, Q5 (high-side): VMOTOR
#   Q2 (low-side phase A):  PHASE_A
#   Q4 (low-side phase B):  PHASE_B
#   Q6 (low-side phase C):  PHASE_C
MOSFET_REFERENCES = ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6")
HEAT_SINK_PAD_NUMBER = "2"

# Issue #2901 AC #4 target: ≥4 thermal vias per MOSFET heat-sink pad.
MIN_THERMAL_VIAS_PER_MOSFET = 4

# Target plane nets the stitcher must consider.  All four phase / motor
# nets need to be passed in so the low-side MOSFETs (whose drain is on a
# PHASE_x net) get stitched alongside the high-side VMOTOR MOSFETs.  The
# stitch implementation will only succeed on nets that have either a
# zone or sufficient adjacent copper -- failures-by-net surface in
# ``StitchResult.pads_skipped`` for diagnostic attribution.
TARGET_NETS = ["+24V", "PHASE_A", "PHASE_B", "PHASE_C", "GND"]

# Stitch parameters tuned for the IRLZ44N TO-220 footprint on board 05.
# The 0.4mm via with 0.15mm clearance fits the 5mm pitch between adjacent
# MOSFET pads on this layout; the default 0.45/0.20 leaves Q5 pad 2 with
# only 3 candidate positions due to an adjacent trace.  Choose values
# that match what board-05's design.py will likely use when it adopts
# thermal stitching.
STITCH_VIA_SIZE = 0.4
STITCH_DRILL = 0.2
STITCH_CLEARANCE = 0.15
STITCH_THERMAL_RADIUS = 2.2


@pytest.fixture(scope="module")
def routed_pcb_path() -> Path:
    """Resolve the committed routed PCB or skip if absent."""
    if not ROUTED_PCB.exists():
        pytest.skip(
            f"Board 05 routed PCB not found at {ROUTED_PCB!s}; "
            "regenerate via "
            "`uv run python boards/05-bldc-motor-controller/design.py`"
        )
    return ROUTED_PCB


@pytest.fixture
def routed_pcb_copy(routed_pcb_path: Path, tmp_path: Path) -> Path:
    """Yield a per-test scratch copy of the routed PCB.

    The thermal-stitch primitive mutates the on-disk file when
    ``dry_run=False``.  Even in dry-run we copy so the test is robust to
    any future side-effect changes (e.g., zone-fill caching).
    """
    dest = tmp_path / "bldc_controller_routed.kicad_pcb"
    shutil.copy2(routed_pcb_path, dest)
    return dest


class TestBoard05ThermalStitch:
    """Acceptance criterion 4 of issue #2901."""

    @pytest.mark.xfail(
        reason=(
            "Issue #3443: the #3240 carve fallback in zones/generator.py "
            "downgrades the +24V pour outline to its 0.3mm pad-safe bbox "
            "on every fresh zone regen (the 1.5mm raw bbox covers lower-"
            "priority siblings' pads), so the stitch halo around the HS "
            "MOSFET tabs Q1/Q3/Q5 -- which sit at the +24V pad-bbox "
            "extremes -- falls outside the pour's 0.35mm edge margin "
            "(Q1=2, Q3=2, Q5=1 vias).  Reproduced on the PRE-#3423 "
            "committed board with current main's auto-pour, so this is a "
            "zone-generator regression, not a placement effect.  Remove "
            "this marker when #3443 restores the pour margin at the "
            "thermal pads."
        ),
        strict=True,
    )
    def test_each_mosfet_heat_sink_gets_min_vias(
        self,
        routed_pcb_copy: Path,
    ) -> None:
        """Each of Q1-Q6 pad 2 must receive ≥4 thermal vias.

        Runs the thermal-stitch primitive in dry-run mode against a
        temporary copy of the committed routed PCB and aggregates the
        per-pad via count from :class:`StitchResult.vias_added`.  A
        failure attributes the shortfall to specific MOSFETs so the
        fix has a sharp target.

        Defaults are tuned in :data:`STITCH_VIA_SIZE` / friends so the
        primitive can place at least 4 vias on every MOSFET on the
        current routed PCB; a future regression in
        :func:`generate_thermal_via_positions` or in clearance handling
        will trip this assertion.
        """
        result = run_thermal_stitch(
            pcb_path=routed_pcb_copy,
            net_names=TARGET_NETS,
            via_size=STITCH_VIA_SIZE,
            drill=STITCH_DRILL,
            clearance=STITCH_CLEARANCE,
            vias_per_pad=MIN_THERMAL_VIAS_PER_MOSFET,
            thermal_radius=STITCH_THERMAL_RADIUS,
            dry_run=True,
        )

        # Bucket the placed vias by (ref, pad_number) so we can audit
        # per-MOSFET coverage.
        per_mosfet_count: dict[tuple[str, str], int] = {}
        for via in result.vias_added:
            key = (via.pad.reference, via.pad.pad_number)
            per_mosfet_count[key] = per_mosfet_count.get(key, 0) + 1

        # Build a per-MOSFET shortfall report covering all six refs so a
        # single test failure surfaces every offending pad at once.
        shortfalls: list[str] = []
        for ref in MOSFET_REFERENCES:
            count = per_mosfet_count.get((ref, HEAT_SINK_PAD_NUMBER), 0)
            if count < MIN_THERMAL_VIAS_PER_MOSFET:
                shortfalls.append(
                    f"{ref} pad {HEAT_SINK_PAD_NUMBER}: {count} via(s) "
                    f"(need >= {MIN_THERMAL_VIAS_PER_MOSFET})"
                )

        assert not shortfalls, (
            "Board 05 thermal-stitch failed to place enough thermal vias "
            f"on {len(shortfalls)} MOSFET(s):\n  "
            + "\n  ".join(shortfalls)
            + "\n\nThis indicates either a regression in "
            "find_thermal_pad_candidates (the heuristic stopped flagging "
            "the TO-220 IRLZ44N footprint family), in "
            "generate_thermal_via_positions (placement geometry lost "
            "clearance to neighbouring routed traces), or a change to "
            "the routed PCB that crowded the MOSFET pad area beyond what "
            "the stitcher can navigate.  Inspect StitchResult.pads_skipped "
            "for per-position rejection reasons."
        )

    def test_mosfet_vias_land_on_target_nets_only(
        self,
        routed_pcb_copy: Path,
    ) -> None:
        """Vias placed on Q1-Q6 pad 2 must be on one of the target nets.

        Validates the stitch primitive's net-membership invariant: every
        via attributed to a MOSFET heat-sink pad must be associated with
        a pad whose net is in :data:`TARGET_NETS`.  A regression that
        widened the candidate filter to pick up non-plane nets (e.g.,
        GATE_AH) would silently introduce shorts; this catches that.
        """
        result = run_thermal_stitch(
            pcb_path=routed_pcb_copy,
            net_names=TARGET_NETS,
            via_size=STITCH_VIA_SIZE,
            drill=STITCH_DRILL,
            clearance=STITCH_CLEARANCE,
            vias_per_pad=MIN_THERMAL_VIAS_PER_MOSFET,
            thermal_radius=STITCH_THERMAL_RADIUS,
            dry_run=True,
        )

        net_mismatches: list[str] = []
        for via in result.vias_added:
            if via.pad.reference not in MOSFET_REFERENCES:
                continue
            if via.pad.pad_number != HEAT_SINK_PAD_NUMBER:
                continue
            if via.pad.net_name not in TARGET_NETS:
                net_mismatches.append(
                    f"{via.pad.reference} pad {via.pad.pad_number}: "
                    f"via attributed to non-target net "
                    f"{via.pad.net_name!r}"
                )

        assert not net_mismatches, (
            "Thermal-stitch placed vias on non-target nets:\n  "
            + "\n  ".join(net_mismatches)
            + "\n\nThis is a net-filter regression -- the stitcher "
            f"must restrict thermal vias to {TARGET_NETS!r}."
        )

    def test_mosfet_vias_fall_within_thermal_radius(
        self,
        routed_pcb_copy: Path,
    ) -> None:
        """Vias under Q1-Q6 pad 2 must be close to the pad centre.

        The thermal-stitch primitive places vias either ON the pad
        (under-pad mode) or in a HALO ring around it.  For the TO-220
        1.8x1.8mm pad on board 05 the halo mode is selected (pad too
        small for an under-pad grid with the default clearance), so vias
        sit at radii from ~0.9mm (pad edge + clearance) out to
        ``thermal_radius + via_size``.  Allow a generous 4.0mm budget
        so future clearance bumps don't break the test.
        """
        result = run_thermal_stitch(
            pcb_path=routed_pcb_copy,
            net_names=TARGET_NETS,
            via_size=STITCH_VIA_SIZE,
            drill=STITCH_DRILL,
            clearance=STITCH_CLEARANCE,
            vias_per_pad=MIN_THERMAL_VIAS_PER_MOSFET,
            thermal_radius=STITCH_THERMAL_RADIUS,
            dry_run=True,
        )

        import math

        out_of_range: list[str] = []
        max_radius_mm = STITCH_THERMAL_RADIUS + STITCH_VIA_SIZE + 0.5

        for via in result.vias_added:
            if via.pad.reference not in MOSFET_REFERENCES:
                continue
            if via.pad.pad_number != HEAT_SINK_PAD_NUMBER:
                continue
            dx = via.via_x - via.pad.x
            dy = via.via_y - via.pad.y
            r = math.hypot(dx, dy)
            if r > max_radius_mm:
                out_of_range.append(
                    f"{via.pad.reference} pad {via.pad.pad_number}: "
                    f"via at ({via.via_x:.3f},{via.via_y:.3f}) is "
                    f"{r:.3f}mm from pad centre "
                    f"({via.pad.x:.3f},{via.pad.y:.3f}); "
                    f"max allowed {max_radius_mm:.3f}mm"
                )

        assert not out_of_range, (
            "Thermal-stitch placed vias outside the expected thermal "
            f"radius ({max_radius_mm:.3f}mm) on:\n  "
            + "\n  ".join(out_of_range)
            + "\n\nThis is a placement-geometry regression in "
            "generate_thermal_via_positions -- vias should cluster near "
            "the pad they're stitching."
        )
