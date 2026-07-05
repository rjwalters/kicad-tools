"""Tests for issue #3900: rescue-only-band no-attempt must not be reported
as a clearance failure.

Background
----------
A dual-row SOP/SOIC package whose pitch sits above both the always-dense
0.75mm cap and the dynamic between-pin-trace threshold
(``2 * (trace_width + trace_clearance)``) enters the dense list only to give
the #3398 in-pad rescue a chance to free the launch corridor.  With the
production default ``SOP_RESCUE_MAX_PER_ROW = 0`` the rescue-only band
deliberately emits NO escape geometry -- no clearance check of any kind runs.

Before #3900, ``generate_escapes`` reported that (correct) empty escape list
as ``"0 pins escaped -- all escapes failed clearance validation"`` and
suggested tuning ``fine_pitch_clearance``.  That attribution is fabricated:
no escape was attempted and no clearance validation ran.  This test locks in
the corrected diagnostic: an INFO "not attempted" message with NO
``fine_pitch_clearance`` suggestion for the rescue-only-band + cap==0 path,
while the genuine clearance-failure WARNING is preserved for other packages.
"""

import logging

from kicad_tools.router.escape import (
    EscapeRouter,
    PackageType,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def make_soic8(
    pitch: float = 1.27,
    ref: str = "U5",
    pad_width: float = 0.6,
    pad_height: float = 1.55,
    row_spacing: float = 5.2,
    start_net: int = 1,
) -> list[Pad]:
    """Create a UCC27211-style SOIC-8 (8 pads, two rows of 4, 1.27mm pitch).

    Every pad is assigned a distinct non-zero net so ``pin_count > 0`` and the
    zero-escape diagnostic branch in ``generate_escapes`` is exercised.
    """
    pins_per_row = 4
    pads: list[Pad] = []
    total_width = (pins_per_row - 1) * pitch
    start_x = -total_width / 2

    # Top row: pins 1..4
    for i in range(pins_per_row):
        pads.append(
            Pad(
                x=start_x + i * pitch,
                y=row_spacing / 2,
                width=pad_width,
                height=pad_height,
                net=start_net + i,
                net_name=f"NET{start_net + i}",
                ref=ref,
                pin=str(i + 1),
                layer=Layer.F_CU,
            )
        )

    # Bottom row: pins 8..5 (right-to-left, matching real SOIC numbering)
    for i in range(pins_per_row):
        pads.append(
            Pad(
                x=start_x + (pins_per_row - 1 - i) * pitch,
                y=-row_spacing / 2,
                width=pad_width,
                height=pad_height,
                net=start_net + pins_per_row + i,
                net_name=f"NET{start_net + pins_per_row + i}",
                ref=ref,
                pin=str(8 - i),
                layer=Layer.F_CU,
            )
        )

    return pads


def _make_router() -> EscapeRouter:
    """Build a router with jlcpcb-tier1-style 0.30/0.20 rules.

    ``dynamic_threshold = 2 * (0.30 + 0.20) = 1.0mm``; a 1.27mm-pitch SOIC-8
    sits above both 0.75mm and 1.0mm, so it takes the rescue-only-band path.
    """
    rules = DesignRules(
        trace_width=0.30,
        trace_clearance=0.20,
        via_drill=0.30,
        via_diameter=0.60,
        grid_resolution=0.05,
    )
    grid = RoutingGrid(
        width=30.0,
        height=30.0,
        rules=rules,
        origin_x=-15.0,
        origin_y=-15.0,
    )
    return EscapeRouter(grid, rules)


class TestRescueBandNotAttempted:
    """Issue #3900: rescue-only-band + cap==0 is 'not attempted', not a failure."""

    def test_rescue_band_is_the_path_taken(self):
        """Sanity check: the SOIC-8 fixture actually classifies as SOP and
        produces zero escapes with the production defer-all cap."""
        router = _make_router()
        pads = make_soic8()
        package_info = router.analyze_package(pads)

        assert package_info.package_type == PackageType.SOP
        assert package_info.pin_pitch >= 0.75

        escapes = router.generate_escapes(package_info)

        # Empty escape list is the CORRECT behaviour for a rescue-only band
        # with SOP_RESCUE_MAX_PER_ROW=0 -- no geometry is emitted.
        assert escapes == []

    def test_rescue_band_disabled_no_clearance_warning(self, caplog):
        """No WARNING about 'failed clearance validation' and no
        'fine_pitch_clearance' suggestion for the not-attempted path."""
        router = _make_router()
        pads = make_soic8()
        package_info = router.analyze_package(pads)

        with caplog.at_level(logging.DEBUG, logger="kicad_tools.router.escape"):
            escapes = router.generate_escapes(package_info)

        assert escapes == []

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("failed clearance validation" in msg for msg in warning_msgs), (
            "Rescue-only-band no-attempt must NOT emit the clearance-failure "
            f"WARNING (issue #3900), but got: {warning_msgs}"
        )

        all_msgs = [r.message for r in caplog.records]
        assert not any("fine_pitch_clearance" in msg for msg in all_msgs), (
            "The fine_pitch_clearance suggestion is fabricated attribution for "
            f"a not-attempted band (issue #3900), but got: {all_msgs}"
        )

    def test_rescue_band_disabled_info_logged(self, caplog):
        """An INFO record explains the escape was not attempted because the
        rescue band is disabled."""
        router = _make_router()
        pads = make_soic8()
        package_info = router.analyze_package(pads)

        with caplog.at_level(logging.INFO, logger="kicad_tools.router.escape"):
            escapes = router.generate_escapes(package_info)

        assert escapes == []

        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("not attempted" in msg and "rescue band disabled" in msg for msg in info_msgs), (
            f"Expected an INFO 'not attempted / rescue band disabled' record, got: {info_msgs}"
        )
        # The env-override next step should be surfaced for users who want the
        # rescue experiment.
        assert any("KICAD_TOOLS_SOP_RESCUE_ROW_CAP" in msg for msg in info_msgs), (
            f"Expected the env-override hint in the INFO record, got: {info_msgs}"
        )


class TestGenuineClearanceFailureStillWarns:
    """Issue #3900 regression guard: the real clearance-failure WARNING path
    must be untouched for non-rescue-only-band packages."""

    def test_non_rescue_band_zero_escapes_still_warns(self, caplog):
        """An SSOP-20 at 0.65mm pitch with impossibly strict clearance still
        emits the WARNING with the fine_pitch_clearance suggestion.

        This mirrors ``TestZeroEscapeWarning.test_warning_logged_on_zero_escapes``
        in ``tests/test_escape_fine_pitch_clearance.py`` -- the SSOP-20 is NOT
        a rescue-only-band SOP, so the ``_escape_not_attempted_rescue_band``
        flag stays False and the genuine-failure branch fires.
        """
        rules = DesignRules(
            trace_width=0.5,
            trace_clearance=0.5,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
        )
        rules.component_clearances["U8"] = 0.5  # Force impossibly strict clearance

        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            origin_x=-10.0,
            origin_y=-10.0,
        )

        router = EscapeRouter(grid, rules)

        # SSOP-20 fixture: two rows of 10 at 0.65mm pitch -> classified SSOP.
        pins_per_row = 10
        pitch = 0.65
        total_width = (pins_per_row - 1) * pitch
        start_x = -total_width / 2
        pads: list[Pad] = []
        for i in range(pins_per_row):
            pads.append(
                Pad(
                    x=start_x + i * pitch,
                    y=2.65,
                    width=0.35,
                    height=1.2,
                    net=1 + i,
                    net_name=f"NET{1 + i}",
                    ref="U8",
                    pin=str(i + 1),
                    layer=Layer.F_CU,
                )
            )
        for i in range(pins_per_row):
            pads.append(
                Pad(
                    x=start_x + (pins_per_row - 1 - i) * pitch,
                    y=-2.65,
                    width=0.35,
                    height=1.2,
                    net=1 + pins_per_row + i,
                    net_name=f"NET{1 + pins_per_row + i}",
                    ref="U8",
                    pin=str(20 - i),
                    layer=Layer.F_CU,
                )
            )

        package_info = router.analyze_package(pads)
        assert package_info.package_type in (PackageType.SSOP, PackageType.TSSOP)

        with caplog.at_level(logging.WARNING, logger="kicad_tools.router.escape"):
            escapes = router.generate_escapes(package_info)

        assert escapes == [], "SSOP-20 with 0.5mm forced clearance should get 0 escapes"

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("failed clearance validation" in msg for msg in warning_msgs), (
            "Genuine clearance failure must still WARN (issue #3900 regression "
            f"guard), but got: {warning_msgs}"
        )
        assert any("fine_pitch_clearance" in msg for msg in warning_msgs), (
            "The fine_pitch_clearance suggestion must still appear on a genuine "
            f"clearance failure, but got: {warning_msgs}"
        )
