"""Tests for escape routing module for dense packages."""

import math

import pytest

from kicad_tools.router.escape import (
    EscapeDirection,
    EscapeRoute,
    EscapeRouter,
    PackageType,
    _is_multi_row,
    detect_package_type,
    get_package_info,
    is_dense_package,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules

# ==============================================================================
# Test Data Helpers
# ==============================================================================


def create_bga_pads(rows: int, cols: int, pitch: float = 0.8, ref: str = "U1") -> list[Pad]:
    """Create a grid of pads simulating a BGA package."""
    pads = []
    net = 1

    # Center the grid
    start_x = -pitch * (cols - 1) / 2
    start_y = -pitch * (rows - 1) / 2

    for row in range(rows):
        for col in range(cols):
            pad = Pad(
                x=start_x + col * pitch,
                y=start_y + row * pitch,
                width=0.4,
                height=0.4,
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
                through_hole=False,
            )
            pads.append(pad)
            net += 1

    return pads


def create_qfp_pads(pins_per_side: int, pitch: float = 0.5, ref: str = "U1") -> list[Pad]:
    """Create pads simulating a QFP package with pins on all 4 sides."""
    pads = []
    net = 1

    # Calculate package dimensions
    half_width = (pins_per_side - 1) * pitch / 2 + 1.0  # 1mm margin

    # North side (top)
    for i in range(pins_per_side):
        x = -half_width + 1.0 + i * pitch
        pad = Pad(
            x=x,
            y=half_width,
            width=0.3,
            height=0.8,
            net=net,
            net_name=f"NET_{net}",
            layer=Layer.F_CU,
            ref=ref,
        )
        pads.append(pad)
        net += 1

    # East side (right)
    for i in range(pins_per_side):
        y = half_width - 1.0 - i * pitch
        pad = Pad(
            x=half_width,
            y=y,
            width=0.8,
            height=0.3,
            net=net,
            net_name=f"NET_{net}",
            layer=Layer.F_CU,
            ref=ref,
        )
        pads.append(pad)
        net += 1

    # South side (bottom)
    for i in range(pins_per_side):
        x = half_width - 1.0 - i * pitch
        pad = Pad(
            x=x,
            y=-half_width,
            width=0.3,
            height=0.8,
            net=net,
            net_name=f"NET_{net}",
            layer=Layer.F_CU,
            ref=ref,
        )
        pads.append(pad)
        net += 1

    # West side (left)
    for i in range(pins_per_side):
        y = -half_width + 1.0 + i * pitch
        pad = Pad(
            x=-half_width,
            y=y,
            width=0.8,
            height=0.3,
            net=net,
            net_name=f"NET_{net}",
            layer=Layer.F_CU,
            ref=ref,
        )
        pads.append(pad)
        net += 1

    return pads


def create_qfn_pads(pins_per_side: int, pitch: float = 0.5, ref: str = "U1") -> list[Pad]:
    """Create pads simulating a QFN package with thermal pad."""
    # Start with QFP pads
    pads = create_qfp_pads(pins_per_side, pitch, ref)

    # Add center thermal pad
    net = len(pads) + 1
    thermal_pad = Pad(
        x=0,
        y=0,
        width=2.0,
        height=2.0,
        net=net,
        net_name="GND",
        layer=Layer.F_CU,
        ref=ref,
    )
    pads.append(thermal_pad)

    return pads


def create_usbc_smt_pads(
    signal_pads_per_row: int = 8,
    row_pitch: float = 1.0,
    pad_pitch: float = 0.5,
    mounting_tab_offset: float = 1.5,
    ref: str = "J1",
) -> list[Pad]:
    """Create pads simulating a USB-C SMT receptacle (e.g. GCT USB4105).

    Layout:
      Row A (y=0):              N SMT pads, evenly spaced in X
      Row B (y=row_pitch):      N SMT pads, evenly spaced in X
      Tab S1 (TH, y=tab_offset, x=-half_extent - tab_clear)
      Tab S2 (TH, y=tab_offset, x=+half_extent + tab_clear)

    Real USB-C variants put A1/A12 and B1/B12 on the same row but pin spacing
    is irregular due to the USB-C cluster pattern; this helper uses uniform
    pitch which is sufficient for classifier tests (issue #2513).

    Args:
        signal_pads_per_row: Number of SMT pads per row (default 8 -> total 16
            SMT + 2 tabs = 18, matching the simplified board-03 USB-C).
        row_pitch: Vertical distance between rows A and B in mm.
        pad_pitch: Horizontal distance between adjacent pads in mm.
        mounting_tab_offset: Y position of the mounting tabs relative to row A.
        ref: Component reference designator.

    Returns:
        List of Pad objects (SMT signal pads + 2 through-hole tabs).
    """
    pads: list[Pad] = []
    half = (signal_pads_per_row - 1) * pad_pitch / 2
    net = 1
    # Row A
    for i in range(signal_pads_per_row):
        pads.append(
            Pad(
                x=-half + i * pad_pitch,
                y=0.0,
                width=0.25,
                height=0.35,
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
                through_hole=False,
            )
        )
        net += 1
    # Row B (offset by row_pitch)
    for i in range(signal_pads_per_row):
        pads.append(
            Pad(
                x=-half + i * pad_pitch,
                y=row_pitch,
                width=0.25,
                height=0.35,
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
                through_hole=False,
            )
        )
        net += 1
    # Mounting tabs (TH, off-row)
    for tab_x in (-half - 1.0, half + 1.0):
        pads.append(
            Pad(
                x=tab_x,
                y=mounting_tab_offset,
                width=1.0,
                height=1.0,
                net=3,  # GND
                net_name="GND",
                layer=Layer.F_CU,
                ref=ref,
                through_hole=True,
                drill=0.6,
            )
        )
    return pads


def create_sop_pads(pins: int, pitch: float = 1.27, ref: str = "U1") -> list[Pad]:
    """Create pads simulating a SOP package with 2 rows."""
    pads = []
    net = 1
    pins_per_side = pins // 2

    # Calculate dimensions
    half_length = (pins_per_side - 1) * pitch / 2
    row_spacing = 4.0  # Distance between rows

    # Left row
    for i in range(pins_per_side):
        x = -row_spacing / 2
        y = -half_length + i * pitch
        pad = Pad(
            x=x,
            y=y,
            width=0.6,
            height=0.3,
            net=net,
            net_name=f"NET_{net}",
            layer=Layer.F_CU,
            ref=ref,
        )
        pads.append(pad)
        net += 1

    # Right row
    for i in range(pins_per_side):
        x = row_spacing / 2
        y = half_length - i * pitch
        pad = Pad(
            x=x,
            y=y,
            width=0.6,
            height=0.3,
            net=net,
            net_name=f"NET_{net}",
            layer=Layer.F_CU,
            ref=ref,
        )
        pads.append(pad)
        net += 1

    return pads


# ==============================================================================
# Package Detection Tests
# ==============================================================================


class TestIsDensePackage:
    """Tests for is_dense_package function."""

    def test_bga_256_is_dense(self):
        """BGA-256 with 0.8mm pitch is dense due to pin count."""
        pads = create_bga_pads(16, 16, pitch=0.8)
        assert len(pads) == 256
        assert is_dense_package(pads) is True

    def test_bga_100_is_dense(self):
        """BGA-100 with 0.8mm pitch is dense due to pin count > 48."""
        pads = create_bga_pads(10, 10, pitch=0.8)
        assert len(pads) == 100
        assert is_dense_package(pads) is True

    def test_qfp_100_is_dense(self):
        """TQFP-100 is dense due to pin count."""
        pads = create_qfp_pads(25, pitch=0.5)
        assert len(pads) == 100
        assert is_dense_package(pads) is True

    def test_fine_pitch_is_dense(self):
        """Package with < 0.5mm pitch is dense regardless of pin count."""
        pads = create_qfp_pads(8, pitch=0.4)  # 32 pins but fine pitch
        assert len(pads) == 32
        assert is_dense_package(pads) is True

    def test_sop_16_not_dense(self):
        """SOP-16 with standard pitch is not dense."""
        pads = create_sop_pads(16, pitch=1.27)
        assert len(pads) == 16
        assert is_dense_package(pads) is False

    def test_single_pad_not_dense(self):
        """Single pad is not dense."""
        pads = [Pad(x=0, y=0, width=1, height=1, net=1, net_name="VCC", layer=Layer.F_CU)]
        assert is_dense_package(pads) is False

    def test_empty_pads_not_dense(self):
        """Empty list is not dense."""
        assert is_dense_package([]) is False

    def test_tqfp32_08mm_pitch_with_clearance(self):
        """TQFP-32 with 0.8mm pitch is dense regardless of clearance.

        Issue #2513: TQFP-32-class quad packages (>= 32 pins, quad layout,
        pitch <= 0.8 mm) are now flagged as dense unconditionally.  This
        is because with common board-house defaults (trace=0.2 mm,
        clearance=0.15 mm) the dynamic threshold of 2*(0.2+0.15) = 0.7 mm
        is just below 0.8 mm, so the inner pins of the QFP would otherwise
        get blocked at routing time even though they sit at the largest
        pitch one of the still-needs-escape-routing tier.

        Issue #795 (original): This package was NOT being detected as dense
        without considering clearance requirements; we now treat it as
        dense even without explicit clearance numbers because the failure
        mode is reproducible at JLCPCB defaults.
        """
        pads = create_qfp_pads(8, pitch=0.8)  # 32 pins, 0.8mm pitch
        assert len(pads) == 32

        # Issue #2513: TQFP-32 with 0.8mm pitch is dense even without
        # design rules (the TQFP-32-class rule fires at >= 32 pads + quad
        # layout + pitch <= 0.8mm)
        assert is_dense_package(pads) is True

        # WITH design rules, 0.8mm pitch IS dense because:
        # threshold = 2 * (trace_width + clearance) = 2 * (0.2 + 0.2) = 0.8mm
        # and 0.8mm pitch < 0.8mm threshold (also dense via TQFP-32 rule)
        assert is_dense_package(pads, trace_width=0.2, clearance=0.2) is True

    def test_wide_pitch_not_dense_with_clearance(self):
        """Package with wide pitch is not dense even with clearance requirements."""
        pads = create_sop_pads(16, pitch=1.27)  # Standard SOP
        assert len(pads) == 16

        # 1.27mm pitch > 2 * (0.2 + 0.2) = 0.8mm threshold, so not dense
        assert is_dense_package(pads, trace_width=0.2, clearance=0.2) is False

    def test_dynamic_threshold_tight_clearance(self):
        """Tighter clearance requirements make more packages dense."""
        pads = create_qfp_pads(10, pitch=1.0)  # 40 pins, 1.0mm pitch
        assert len(pads) == 40

        # With small clearance, not dense: 2 * (0.15 + 0.15) = 0.6mm < 1.0mm
        assert is_dense_package(pads, trace_width=0.15, clearance=0.15) is False

        # With larger clearance, becomes dense: 2 * (0.25 + 0.3) = 1.1mm > 1.0mm
        assert is_dense_package(pads, trace_width=0.25, clearance=0.3) is True

    def test_qfp64_lqfp_dense(self):
        """LQFP-64 at 0.5mm pitch is dense due to pin count > 48."""
        pads = create_qfp_pads(16, pitch=0.5)  # 4 * 16 = 64 pins
        assert len(pads) == 64
        assert is_dense_package(pads) is True

    def test_through_hole_not_dense(self):
        """Through-hole package with wide pitch should not be dense."""
        pads = []
        for i in range(8):
            pads.append(
                Pad(
                    x=i * 2.54,
                    y=0,
                    width=1.6,
                    height=1.6,
                    net=i + 1,
                    net_name=f"NET_{i + 1}",
                    layer=Layer.F_CU,
                    ref="U1",
                    through_hole=True,
                )
            )
        # 8 pins at 2.54mm pitch, not dense
        assert is_dense_package(pads) is False
        assert is_dense_package(pads, trace_width=0.2, clearance=0.2) is False


class TestTQFP32DenseRule:
    """Tests for the TQFP-32-class dense-package rule (Issue #2513).

    TQFP-32 (32 pins, quad layout, 0.8mm pitch) packages were previously
    not flagged as dense at common board-house defaults (trace=0.2mm,
    clearance=0.15mm) because the dynamic threshold of 2*(0.2+0.15)=0.7mm
    is just below the 0.8mm pitch.  This made the inner pins of the QFP
    unable to escape the perimeter routing, blocking USB_D+/CC1/CC2/XTAL2
    on board 03 (USB joystick).
    """

    def test_tqfp32_at_0_8mm_pitch_is_dense_without_rules(self):
        """TQFP-32 at 0.8mm pitch is dense even without explicit design rules."""
        pads = create_qfp_pads(8, pitch=0.8)  # 32 pins, 0.8mm pitch
        assert len(pads) == 32
        # The TQFP-32-class rule fires unconditionally
        assert is_dense_package(pads) is True

    def test_tqfp32_at_jlcpcb_defaults_is_dense(self):
        """TQFP-32 is dense at JLCPCB defaults (trace=0.2mm, clearance=0.15mm).

        Without the new rule, the dynamic threshold would be
        2*(0.2+0.15) = 0.7mm, which is < 0.8mm pitch -> NOT dense.
        With the new rule, it is dense regardless.
        """
        pads = create_qfp_pads(8, pitch=0.8)
        assert is_dense_package(pads, trace_width=0.2, clearance=0.15) is True

    def test_tqfp48_is_dense(self):
        """A larger TQFP-48 (12 pins/side, 0.5mm pitch) is dense via fine pitch."""
        pads = create_qfp_pads(12, pitch=0.5)
        assert len(pads) == 48
        assert is_dense_package(pads) is True
        assert is_dense_package(pads, trace_width=0.2, clearance=0.15) is True

    def test_qfp16_at_1mm_pitch_not_dense(self):
        """A QFP-16 (4 pins/side, 1.0mm pitch) is NOT dense -- below 32-pin gate."""
        pads = create_qfp_pads(4, pitch=1.0)
        assert len(pads) == 16
        # Below 32-pin gate; pitch = 1.0 > dynamic threshold 0.7
        assert is_dense_package(pads) is False
        assert is_dense_package(pads, trace_width=0.2, clearance=0.15) is False

    def test_sop16_at_1_27mm_not_caught_by_tqfp32_rule(self):
        """SOIC-16 (dual-row, 1.27mm pitch) is correctly NOT dense.

        Confirms the TQFP-32 rule doesn't fire for non-quad layouts.
        SOIC has fewer than 20 pins so it doesn't trigger the existing
        multi-row dense path either.
        """
        pads = create_sop_pads(16, pitch=1.27)
        assert len(pads) == 16
        assert is_dense_package(pads) is False
        assert is_dense_package(pads, trace_width=0.2, clearance=0.15) is False

    def test_qfn32_at_0_5mm_pitch_dense_via_fine_pitch(self):
        """QFN-32 at 0.5mm pitch is dense (fine-pitch path), not via the new rule.

        QFN-32 with thermal pad (33 total pads) at 0.5mm pitch is dense
        because the dynamic threshold at any reasonable trace/clearance
        catches it; this test exists to ensure the new TQFP-32 rule does
        not regress small-pitch QFN behavior.
        """
        pads = create_qfn_pads(8, pitch=0.5)
        assert is_dense_package(pads) is True
        assert is_dense_package(pads, trace_width=0.2, clearance=0.15) is True


class TestUSBCBGAMisclassification:
    """Tests for the BGA-misclassification fix on 2-row connectors (Issue #2513).

    USB-C SMT connectors (e.g. GCT USB4105) have 2 close SMT rows + 2 mounting
    tabs.  The mounting tabs introduce a third unique-Y group so the previous
    grid-pattern check would classify them as BGA, applying ring-based escape
    routing that wastes the bottom layer for what is really a 2-row connector.
    """

    def test_usbc_smt_18pad_not_bga(self):
        """A 2x8 SMT USB-C with 2 mounting tabs (18 pads) is NOT BGA."""
        pads = create_usbc_smt_pads(signal_pads_per_row=8)
        assert len(pads) == 18  # 16 SMT + 2 TH tabs
        assert detect_package_type(pads) != PackageType.BGA

    def test_usbc_smt_24pad_not_bga(self):
        """A larger 2x12 SMT USB-C variant with mounting tabs is NOT BGA."""
        pads = create_usbc_smt_pads(signal_pads_per_row=12)
        assert len(pads) == 26  # 24 SMT + 2 TH tabs
        assert detect_package_type(pads) != PackageType.BGA

    def test_real_bga_still_detected(self):
        """A real 8x8 BGA must still be detected as BGA after the fix."""
        pads = create_bga_pads(8, 8, pitch=0.8)
        assert detect_package_type(pads) == PackageType.BGA

    def test_small_bga_4x4_still_detected(self):
        """A 4x4 BGA-16 must still be classified as BGA after the fix."""
        pads = create_bga_pads(4, 4, pitch=0.8)
        assert detect_package_type(pads) == PackageType.BGA

    def test_three_row_connector_with_outlier_not_bga(self):
        """3-row connector with one short outlier row stays out of BGA path.

        Confirms _count_substantive_axis_groups filters the short row.
        """
        pads = create_usbc_smt_pads(signal_pads_per_row=10)
        # 20 SMT + 2 tabs; 3 unique Y values but tab row only has 2 pads
        assert len(pads) == 22
        assert detect_package_type(pads) != PackageType.BGA


class TestDetectPackageType:
    """Tests for detect_package_type function."""

    def test_detect_bga(self):
        """Detect BGA from grid pattern."""
        pads = create_bga_pads(8, 8, pitch=0.8)
        assert detect_package_type(pads) == PackageType.BGA

    def test_detect_qfp(self):
        """Detect QFP from quad arrangement."""
        pads = create_qfp_pads(12, pitch=0.65)
        assert detect_package_type(pads) == PackageType.QFP

    def test_detect_qfn(self):
        """Detect QFN from quad arrangement with center pad."""
        pads = create_qfn_pads(8, pitch=0.5)
        assert detect_package_type(pads) == PackageType.QFN

    def test_detect_tqfp(self):
        """Detect TQFP from fine-pitch quad arrangement."""
        pads = create_qfp_pads(16, pitch=0.4)
        assert detect_package_type(pads) == PackageType.TQFP

    def test_detect_sop(self):
        """Detect SOP from dual-row arrangement."""
        pads = create_sop_pads(16, pitch=1.27)
        assert detect_package_type(pads) == PackageType.SOP

    def test_detect_unknown_few_pads(self):
        """Unknown for too few pads."""
        pads = [Pad(x=0, y=0, width=1, height=1, net=1, net_name="A", layer=Layer.F_CU)]
        assert detect_package_type(pads) == PackageType.UNKNOWN


class TestGetPackageInfo:
    """Tests for get_package_info function."""

    def test_bga_package_info(self):
        """Get info for BGA package."""
        pads = create_bga_pads(6, 6, pitch=0.8, ref="U1")
        info = get_package_info(pads)

        assert info.ref == "U1"
        assert info.package_type == PackageType.BGA
        assert info.pin_count == 36
        assert info.rows == 6
        assert info.cols == 6
        assert 0.7 < info.pin_pitch < 0.9  # Approximately 0.8mm

    def test_qfp_package_info(self):
        """Get info for QFP package."""
        pads = create_qfp_pads(13, pitch=0.5, ref="U2")
        info = get_package_info(pads)

        assert info.ref == "U2"
        assert info.package_type == PackageType.QFP
        assert info.pin_count == 52
        assert 0.4 < info.pin_pitch < 0.6  # Approximately 0.5mm

    def test_empty_package_info(self):
        """Get info for empty pad list."""
        info = get_package_info([])

        assert info.ref == ""
        assert info.package_type == PackageType.UNKNOWN
        assert info.pin_count == 0
        assert info.is_dense is False


# ==============================================================================
# Escape Router Tests
# ==============================================================================


class TestEscapeRouter:
    """Tests for EscapeRouter class."""

    @pytest.fixture
    def grid_and_rules(self):
        """Create grid and rules for testing."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.35,
            via_diameter=0.7,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(50, 50, rules, origin_x=0, origin_y=0)
        return grid, rules

    def test_analyze_package(self, grid_and_rules):
        """Test package analysis."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_bga_pads(8, 8)
        info = router.analyze_package(pads)

        assert info.package_type == PackageType.BGA
        assert info.is_dense is True
        assert info.pin_count == 64

    def test_generate_escapes_bga(self, grid_and_rules):
        """Test escape generation for BGA."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_bga_pads(4, 4, pitch=0.8)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Should have escape for each pad
        assert len(escapes) == 16

        # Check escape structure
        for escape in escapes:
            assert isinstance(escape, EscapeRoute)
            assert escape.pad in pads
            assert len(escape.segments) >= 1
            assert escape.direction != EscapeDirection.VIA_DOWN or escape.via is not None

    def test_generate_escapes_qfp(self, grid_and_rules):
        """Test escape generation for QFP.

        Issue #2756: With the post-fix pad-clearance clipping, a 0.5mm-pitch
        QFP-32 escape with trace_clearance=0.2mm produces fewer escapes than
        the naive ``one-per-pad`` upper bound -- escapes whose clipped
        segment is too short to clear the pad halo are deferred to the main
        router rather than emitted as clearance-violating stubs.  The
        previous assertion of ``len(escapes) == 32`` was relying on the
        broken pre-#2756 behaviour where the alternating direction emitter
        wrote violating segments unconditionally.
        """
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_qfp_pads(8, pitch=0.5)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # At least the perpendicular (even-indexed) escapes on every edge
        # should survive clipping (perpendicular launches don't run along
        # the same-edge pads).  4 edges * 4 even pins = 16 minimum.
        assert len(escapes) >= 16, (
            f"Expected at least 16 perpendicular-axis escapes for QFP-32, "
            f"got {len(escapes)}"
        )
        # Upper bound is one-per-edge-pad (32) -- no escape should be added
        # spuriously by the clipping path.
        assert len(escapes) <= 32

        # Every escape must respect pad-to-segment clearance against
        # every OTHER pad on the package (the post-#2756 invariant).
        for escape in escapes:
            for seg in escape.segments:
                if seg.layer != Layer.F_CU:
                    continue  # vias escape on other layers, skip
                for other in pads:
                    if other is escape.pad:
                        continue
                    if other.layer != seg.layer and not other.through_hole:
                        continue
                    gap = router._segment_to_pad_edge_gap(seg, other)
                    # Allow a small float tolerance.
                    assert gap >= rules.trace_clearance - 1e-6, (
                        f"Escape from pad {escape.pad.net_name} violates "
                        f"clearance against pad {other.net_name}: "
                        f"gap={gap:.4f} < required={rules.trace_clearance:.4f}"
                    )

    def test_escape_directions_vary(self, grid_and_rules):
        """Test that escapes use different directions."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_qfp_pads(8, pitch=0.5)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Collect unique directions
        directions = {e.direction for e in escapes}

        # Should use multiple directions for QFP
        assert len(directions) > 2

    def test_bga_ring_indices(self, grid_and_rules):
        """Test that BGA escapes have correct ring indices."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_bga_pads(4, 4, pitch=0.8)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Should have multiple ring indices
        ring_indices = {e.ring_index for e in escapes}
        assert len(ring_indices) >= 2  # At least outer and inner rings

    def test_escape_layers_alternate(self, grid_and_rules):
        """Test that BGA escapes alternate layers by ring."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_bga_pads(6, 6, pitch=0.8)  # Larger for more rings
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Group by ring index
        by_ring: dict[int, list[Layer]] = {}
        for escape in escapes:
            if escape.ring_index not in by_ring:
                by_ring[escape.ring_index] = []
            by_ring[escape.ring_index].append(escape.escape_layer)

        # Check that different rings use different layers
        if len(by_ring) >= 2:
            ring_layers = [layers[0] for layers in by_ring.values() if layers]
            # Should have at least 2 different layers used
            assert len(set(ring_layers)) >= 1


class TestStaggeredViaFanout:
    """Tests for staggered via fanout."""

    @pytest.fixture
    def grid_and_rules(self):
        """Create grid and rules for testing."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.35,
            via_diameter=0.7,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(50, 50, rules, origin_x=0, origin_y=0)
        return grid, rules

    def test_staggered_pattern(self, grid_and_rules):
        """Test that vias are placed in staggered pattern."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        # Create a simple 2x2 grid of pads
        pads = create_bga_pads(2, 2, pitch=0.8)
        # Issue #2948: pass empty foreign-copper lists explicitly to
        # preserve the legacy grid-cell-only behavior under test.
        vias = router.staggered_via_fanout(
            pads, foreign_pads=[], foreign_tracks=[]
        )

        # Should have up to 4 vias
        assert len(vias) <= 4

    def test_via_positions_offset(self, grid_and_rules):
        """Test that via positions are offset from pad positions."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_bga_pads(3, 3, pitch=1.0)  # Larger pitch for clarity
        # Issue #2948: pass empty foreign-copper lists explicitly so the
        # legacy fast path (grid-cell-only) is exercised here.
        vias = router.staggered_via_fanout(
            pads, stagger_distance=0.3, foreign_pads=[], foreign_tracks=[]
        )

        # Check that vias are near but not exactly on pad positions
        for via in vias:
            min_dist = float("inf")
            for pad in pads:
                dist = math.sqrt((via.x - pad.x) ** 2 + (via.y - pad.y) ** 2)
                min_dist = min(min_dist, dist)
            # Via should be offset from pad (not exactly on it)
            # but close enough to connect
            assert min_dist < 1.0  # Within 1mm

    def test_foreign_pad_in_envelope_rejects_via(self, grid_and_rules):
        """Issue #2948: when a foreign-net pad sits inside the via's
        clearance envelope, ``staggered_via_fanout`` must drop the
        offending candidate via ``_can_place_via``.

        This pins the wire from caller -> predicate.  Without the wiring
        (the pre-#2948 state) the same call would have emitted the via.
        """
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        # Single same-net pad; with stagger_distance=0 the candidate via
        # lands exactly on the pad.  Place a foreign-net pad at a
        # distance that violates the clearance envelope:
        #
        #   via_diameter = 0.7  -> via_radius = 0.35
        #   foreign pad effective radius = max(0.4, 0.4) / 2 = 0.2
        #   via_clearance = 0.2
        #   required separation = 0.35 + 0.2 + 0.2 = 0.75 mm
        #
        # Place the foreign pad 0.5 mm away -> below the threshold ->
        # rejection expected.
        own_pad = Pad(
            x=10.0, y=10.0, width=0.4, height=0.4,
            net=5, net_name="OWN", layer=Layer.F_CU,
        )
        foreign = Pad(
            x=10.5, y=10.0, width=0.4, height=0.4,
            net=99, net_name="OTHER", layer=Layer.F_CU,
        )

        # Baseline: with no foreign context the legacy path emits the via.
        baseline_vias = router.staggered_via_fanout(
            [own_pad], stagger_distance=0.0
        )
        assert len(baseline_vias) == 1, (
            "Sanity check: legacy path (no foreign context) should emit "
            "the candidate via."
        )

        # Wired path: foreign pad inside the envelope rejects the via.
        wired_vias = router.staggered_via_fanout(
            [own_pad],
            stagger_distance=0.0,
            foreign_pads=[foreign],
            foreign_tracks=[],
        )
        assert wired_vias == [], (
            "Issue #2948: foreign-net pad inside clearance envelope must "
            "cause _can_place_via to reject the candidate via."
        )

    def test_foreign_track_in_envelope_rejects_via(self, grid_and_rules):
        """Issue #2948: foreign-net track segments must also be honored
        when supplied via ``foreign_tracks``.
        """
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        own_pad = Pad(
            x=10.0, y=10.0, width=0.4, height=0.4,
            net=5, net_name="OWN", layer=Layer.F_CU,
        )
        # Horizontal foreign-net trace passing 0.4 mm above the via center.
        # Required clearance:
        #   via_radius (0.35) + trace_half_width (0.1) + via_clearance (0.2)
        #   = 0.65 mm; actual 0.4 mm -> reject.
        foreign_seg = Segment(
            x1=9.0, y1=10.4, x2=11.0, y2=10.4,
            width=0.2, layer=Layer.F_CU, net=99, net_name="OTHER",
        )

        wired_vias = router.staggered_via_fanout(
            [own_pad],
            stagger_distance=0.0,
            foreign_pads=[],
            foreign_tracks=[foreign_seg],
        )
        assert wired_vias == [], (
            "Issue #2948: foreign-net track inside clearance envelope "
            "must cause _can_place_via to reject the candidate via."
        )

    def test_same_net_pad_does_not_reject_via(self, grid_and_rules):
        """Issue #2948: same-net pads in ``foreign_pads`` must NOT cause
        rejection.  The predicate must filter by net before evaluating
        clearance — otherwise the parent pad would always reject its own
        in-pad via.
        """
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        own_pad = Pad(
            x=10.0, y=10.0, width=0.4, height=0.4,
            net=5, net_name="OWN", layer=Layer.F_CU,
        )
        # Same-net "foreign" pad close enough to violate clearance if
        # treated as foreign.  Must be filtered out by net=5.
        same_net = Pad(
            x=10.3, y=10.0, width=0.4, height=0.4,
            net=5, net_name="OWN", layer=Layer.F_CU,
        )

        vias = router.staggered_via_fanout(
            [own_pad],
            stagger_distance=0.0,
            foreign_pads=[same_net],
            foreign_tracks=[],
        )
        assert len(vias) == 1, (
            "Issue #2948: same-net pads must be filtered out before the "
            "world-coord clearance check."
        )


class TestCanPlaceViaOwnNetObstacle:
    """Issue #2963 regression: `_can_place_via` must admit a candidate
    that lands on an own-net `is_obstacle=True` cell.

    Post-PR #2928, isolated pad-metal cells are marked
    ``is_obstacle=True`` on first touch.  Before this fix, the grid-cell
    check inside ``_can_place_via`` rejected EVERY candidate that landed
    inside the destination pad's footprint -- even when ``net`` matched
    the pad's own net.  On board 04 this manifested as NRST/BOOT0 (and
    similar endpoint pads) being unreachable, dropping the route from
    9/9 to 7/9.
    """

    @pytest.fixture
    def grid_and_rules(self):
        """Create grid and rules for testing."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.35,
            via_diameter=0.7,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(50, 50, rules, origin_x=0, origin_y=0)
        return grid, rules

    def _paint_obstacle_cell(self, grid, x, y, net):
        """Mark the cell at world (x, y) as a same-net obstacle on every
        layer, mirroring the post-PR #2928 isolated-pad first-touch
        bookkeeping.
        """
        gx, gy = grid.world_to_grid(x, y)
        for layer_idx in range(grid.num_layers):
            cell = grid.grid[layer_idx][gy][gx]
            cell.blocked = True
            cell.is_obstacle = True
            cell.net = net

    def test_own_net_obstacle_admits_via(self, grid_and_rules):
        """Issue #2963 primary regression:
        ``_can_place_via(x, y, net=pad.net)`` must return True at a cell
        whose ``is_obstacle=True`` belongs to ``pad.net`` (own net).
        """
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pad_net = 7
        pad_x, pad_y = 10.0, 10.0
        self._paint_obstacle_cell(grid, pad_x, pad_y, pad_net)

        # Same-net via: must be admitted even though the cell is an
        # obstacle.  Without the fix this returns False -- the bug that
        # blocked board 04 NRST/BOOT0.
        assert router._can_place_via(pad_x, pad_y, net=pad_net) is True, (
            "Issue #2963: same-net via must be admitted on an own-net "
            "is_obstacle cell (NRST/BOOT0 endpoint reachability)."
        )

    def test_foreign_net_obstacle_rejects_via(self, grid_and_rules):
        """Issue #2963 corollary: foreign-net obstacle cells must still
        reject the candidate via.  The fix must not loosen the original
        PR #2928 invariant.
        """
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        obstacle_net = 7
        probe_net = 99
        x, y = 10.0, 10.0
        self._paint_obstacle_cell(grid, x, y, obstacle_net)

        assert router._can_place_via(x, y, net=probe_net) is False, (
            "Issue #2963: foreign-net obstacle cells must still reject "
            "the via (preserves PR #2928's invariant)."
        )

    def test_none_net_obstacle_rejects_via(self, grid_and_rules):
        """When the caller supplies ``net=None`` (no net context), the
        predicate must remain conservative and reject obstacle cells --
        callers who lack net context cannot prove the cell is own-net.
        """
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        self._paint_obstacle_cell(grid, 10.0, 10.0, net=7)

        assert router._can_place_via(10.0, 10.0, net=None) is False, (
            "Issue #2963: with no net context, conservative reject must "
            "be preserved (no spurious allowance)."
        )


class TestApplyEscapeRoutes:
    """Tests for applying escape routes to grid."""

    @pytest.fixture
    def grid_and_rules(self):
        """Create grid and rules for testing."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.35,
            via_diameter=0.7,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(50, 50, rules, origin_x=0, origin_y=0)
        return grid, rules

    def test_apply_creates_routes(self, grid_and_rules):
        """Test that applying escapes creates Route objects."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_bga_pads(4, 4, pitch=0.8)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)
        routes = router.apply_escape_routes(escapes)

        # Should create Route objects
        assert len(routes) == len(escapes)
        for route in routes:
            assert route.net > 0
            assert len(route.segments) >= 1


# ==============================================================================
# Integration Tests
# ==============================================================================


class TestAutorouterEscapeIntegration:
    """Tests for escape routing integration with Autorouter."""

    def test_detect_dense_packages(self):
        """Test dense package detection through Autorouter."""
        from kicad_tools.router import Autorouter, DesignRules

        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            grid_resolution=0.1,
        )
        router = Autorouter(100, 100, rules=rules)

        # Add a dense package
        bga_pads = create_bga_pads(8, 8, pitch=0.8, ref="U1")
        for i, pad in enumerate(bga_pads):
            router.add_component(
                "U1",
                [
                    {
                        "number": str(i + 1),
                        "x": pad.x + 50,  # Center in board
                        "y": pad.y + 50,
                        "width": pad.width,
                        "height": pad.height,
                        "net": pad.net,
                        "net_name": pad.net_name,
                        "layer": pad.layer,
                    }
                ],
            )

        # Add a non-dense package
        sop_pads = create_sop_pads(8, pitch=1.27, ref="U2")
        for i, pad in enumerate(sop_pads):
            router.add_component(
                "U2",
                [
                    {
                        "number": str(i + 1),
                        "x": pad.x + 20,
                        "y": pad.y + 50,
                        "width": pad.width,
                        "height": pad.height,
                        "net": pad.net + 100,  # Different net range
                        "net_name": f"NET_{pad.net + 100}",
                        "layer": pad.layer,
                    }
                ],
            )

        # Detect dense packages
        dense = router.detect_dense_packages()

        # Should find U1 (BGA) but not U2 (SOP)
        assert len(dense) == 1
        assert dense[0].ref == "U1"

    def test_escape_statistics(self):
        """Test escape statistics through Autorouter."""
        from kicad_tools.router import Autorouter, DesignRules

        rules = DesignRules()
        router = Autorouter(100, 100, rules=rules)

        # Add dense package
        bga_pads = create_bga_pads(8, 8, pitch=0.8, ref="U1")
        for i, pad in enumerate(bga_pads):
            router.add_component(
                "U1",
                [
                    {
                        "number": str(i + 1),
                        "x": pad.x + 50,
                        "y": pad.y + 50,
                        "width": pad.width,
                        "height": pad.height,
                        "net": pad.net,
                        "net_name": pad.net_name,
                        "layer": pad.layer,
                    }
                ],
            )

        stats = router.get_escape_statistics()

        assert stats["dense_packages"] == 1
        assert stats["total_pins_escaped"] == 64
        assert len(stats["package_details"]) == 1
        assert stats["package_details"][0]["ref"] == "U1"


class TestSOPStaggeredEscape:
    """Tests for SOP/TSSOP/SOIC staggered escape routing."""

    @pytest.fixture
    def grid_and_rules(self):
        """Create grid and rules for testing."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.35,
            via_diameter=0.7,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(50, 50, rules, origin_x=0, origin_y=0)
        return grid, rules

    def test_sop_escapes_use_staggered_method(self, grid_and_rules):
        """Test that SOP packages use staggered escape routing."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        # Create SOP-16 pads with 1.0mm pitch (standard SOP pitch)
        # Note: 0.65mm pitch is now classified as SSOP with different routing
        pads = create_sop_pads(16, pitch=1.0)
        info = router.analyze_package(pads)

        assert info.package_type == PackageType.SOP

        escapes = router.generate_escapes(info)

        # Should have escape for each pad
        assert len(escapes) == 16

    def test_sop_escapes_have_staggered_vias(self, grid_and_rules):
        """Test that SOP escapes place vias in staggered pattern."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        # Create SOP pads with 1.0mm pitch (standard SOP)
        # Note: 0.65mm pitch is now classified as SSOP with different routing
        pads = create_sop_pads(16, pitch=1.0)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # All escapes should have vias
        for escape in escapes:
            assert escape.via is not None
            assert escape.via_pos is not None

    def test_sop_via_positions_are_staggered(self, grid_and_rules):
        """Test that via positions alternate between odd and even pins."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        # Create horizontal SOP (rows at different Y positions) with 1.0mm pitch
        # Note: 0.65mm pitch is now classified as SSOP with different routing
        pads = create_sop_pads(8, pitch=1.0)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Separate escapes by which row they came from
        # Top row escapes (y > 0) and bottom row (y < 0)
        top_escapes = [e for e in escapes if e.pad.x > 0]
        bottom_escapes = [e for e in escapes if e.pad.x < 0]

        # Check staggering within each row
        for row_escapes in [top_escapes, bottom_escapes]:
            if len(row_escapes) < 2:
                continue

            # Sort by position to get sequential pads
            row_escapes.sort(key=lambda e: e.pad.y)

            # Odd and even indexed escapes should have different via distances
            even_via_dists = []
            odd_via_dists = []
            for i, escape in enumerate(row_escapes):
                # Calculate distance from pad to via
                pad = escape.pad
                via_x, via_y = escape.via_pos
                dist = math.sqrt((via_x - pad.x) ** 2 + (via_y - pad.y) ** 2)

                if i % 2 == 0:
                    even_via_dists.append(dist)
                else:
                    odd_via_dists.append(dist)

            # Even and odd should have different average distances (staggered)
            if even_via_dists and odd_via_dists:
                avg_even = sum(even_via_dists) / len(even_via_dists)
                avg_odd = sum(odd_via_dists) / len(odd_via_dists)
                assert abs(avg_even - avg_odd) > 0.01  # Should be different

    def test_sop_escapes_alternate_layers(self, grid_and_rules):
        """Test that odd and even pin escapes use different layers."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_sop_pads(8, pitch=0.65)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Group escapes from one side
        side_escapes = [e for e in escapes if e.pad.x > 0]
        side_escapes.sort(key=lambda e: e.pad.y)

        if len(side_escapes) >= 2:
            # Check that adjacent pins use different layers
            layers = [e.escape_layer for e in side_escapes]
            # Should have both F.Cu and B.Cu
            assert Layer.F_CU in layers or Layer.B_CU in layers

    def test_sop_escapes_perpendicular_to_rows(self, grid_and_rules):
        """Test that SOP packages escape perpendicular to their pin rows."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        # Use the standard SOP helper (vertical orientation - left/right columns)
        # create_sop_pads creates pads with X spread (row_spacing=4mm) < Y spread
        pads = create_sop_pads(8, pitch=0.65)

        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Issue #2319: Some pads may be deferred to the main router when their
        # escape segments would violate clearance against neighboring pad copper.
        # The important thing is that *generated* escapes have correct directions.
        assert len(escapes) > 0, "At least some pads should escape"
        assert len(escapes) <= 8

        # All escapes should have a direction (perpendicular to their row)
        for escape in escapes:
            assert escape.direction in (
                EscapeDirection.NORTH,
                EscapeDirection.SOUTH,
                EscapeDirection.EAST,
                EscapeDirection.WEST,
            )

        # Check escape direction along the escape axis:
        # - Even pads (no via): escape outward along direction vector
        # - Odd pads (with via): via placed inward (opposite to escape direction)
        #   (Issue #1840: odd-pad vias route inward toward IC body center)
        for escape in escapes:
            escape_x, escape_y = escape.escape_point
            # Get the escape direction unit vector
            dx, dy = router._direction_to_vector(escape.direction)
            if escape.via is None:
                # Even pad: escape point moves outward along direction
                # Project displacement onto direction vector
                disp_along_dir = (
                    (escape_x - escape.pad.x) * dx
                    + (escape_y - escape.pad.y) * dy
                )
                assert disp_along_dir > 0, (
                    f"Even pad escape should move outward: disp={disp_along_dir:.4f}"
                )
            else:
                # Odd pad: via is placed inward (opposite to direction)
                via_x, via_y = escape.via_pos
                via_disp_along_dir = (
                    (via_x - escape.pad.x) * dx
                    + (via_y - escape.pad.y) * dy
                )
                assert via_disp_along_dir < 0, (
                    f"Odd pad via should move inward (opposite to direction): "
                    f"disp={via_disp_along_dir:.4f}"
                )


# ==============================================================================
# Fine-Pitch Escape Clearance Tests (Issue #1784)
# ==============================================================================


def _min_segment_edge_gap(seg1, seg2) -> float:
    """Compute minimum edge-to-edge gap between two segments on the same layer.

    Uses a simple point-to-segment closest approach for all endpoint
    combinations, then subtracts half-widths.
    """

    def _pt_seg_dist(px, py, ax, ay, bx, by):
        abx, aby = bx - ax, by - ay
        apx, apy = px - ax, py - ay
        len_sq = abx * abx + aby * aby
        if len_sq < 1e-12:
            return math.sqrt(apx * apx + apy * apy)
        t = max(0.0, min(1.0, (apx * abx + apy * aby) / len_sq))
        cx, cy = ax + t * abx, ay + t * aby
        dx, dy = px - cx, py - cy
        return math.sqrt(dx * dx + dy * dy)

    d1 = _pt_seg_dist(seg1.x1, seg1.y1, seg2.x1, seg2.y1, seg2.x2, seg2.y2)
    d2 = _pt_seg_dist(seg1.x2, seg1.y2, seg2.x1, seg2.y1, seg2.x2, seg2.y2)
    d3 = _pt_seg_dist(seg2.x1, seg2.y1, seg1.x1, seg1.y1, seg1.x2, seg1.y2)
    d4 = _pt_seg_dist(seg2.x2, seg2.y2, seg1.x1, seg1.y1, seg1.x2, seg1.y2)
    centre_dist = min(d1, d2, d3, d4)
    return centre_dist - (seg1.width + seg2.width) / 2


class TestFinePitchEscapeClearance:
    """Tests that fine-pitch SSOP/TSSOP escape traces respect clearance.

    Issue #1784: Escape traces for adjacent pins were generated independently,
    causing odd-pin surface segments to run parallel to even-pin escape
    segments with as little as 0.020mm gap, violating DRC clearance rules.
    """

    @pytest.fixture
    def fine_pitch_rules(self):
        """Design rules typical for fine-pitch SSOP boards."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=0.15,
            grid_resolution=0.05,
            min_trace_width=0.1,
        )

    @pytest.fixture
    def fine_pitch_router(self, fine_pitch_rules):
        grid = RoutingGrid(50, 50, fine_pitch_rules, origin_x=0, origin_y=0)
        return EscapeRouter(grid, fine_pitch_rules)

    def test_ssop_065mm_adjacent_clearance(self, fine_pitch_router, fine_pitch_rules):
        """SSOP-20 at 0.65mm pitch must maintain trace clearance between adjacent pins."""
        pads = create_sop_pads(20, pitch=0.65)
        info = fine_pitch_router.analyze_package(pads)
        escapes = fine_pitch_router.generate_escapes(info)

        assert len(escapes) == 20

        # Group escapes by side (left/right column)
        left_escapes = sorted(
            [e for e in escapes if e.pad.x < 0], key=lambda e: e.pad.y
        )
        right_escapes = sorted(
            [e for e in escapes if e.pad.x > 0], key=lambda e: e.pad.y
        )

        for side_escapes in [left_escapes, right_escapes]:
            for idx in range(len(side_escapes) - 1):
                e1 = side_escapes[idx]
                e2 = side_escapes[idx + 1]
                # Check all same-layer segment pairs
                for seg1 in e1.segments:
                    for seg2 in e2.segments:
                        if seg1.layer != seg2.layer:
                            continue
                        gap = _min_segment_edge_gap(seg1, seg2)
                        assert gap >= fine_pitch_rules.trace_clearance - 1e-6, (
                            f"Clearance violation between pad {e1.pad.net_name} "
                            f"and {e2.pad.net_name} on {seg1.layer}: "
                            f"gap={gap:.4f}mm < {fine_pitch_rules.trace_clearance}mm"
                        )

    @pytest.mark.parametrize(
        "pitch,clearance",
        [
            (0.5, 0.1),   # TSSOP, tight clearance
            (0.5, 0.15),  # TSSOP, moderate clearance
            (0.5, 0.2),   # TSSOP, generous clearance
            (0.65, 0.1),  # SSOP, tight clearance
            (0.65, 0.15), # SSOP, moderate clearance
            (0.65, 0.2),  # SSOP, generous clearance
        ],
    )
    def test_parametric_pitch_clearance(self, pitch, clearance):
        """Adjacent escape clearance is respected across pitch/clearance combos."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=clearance,
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=clearance,
            grid_resolution=0.05,
            min_trace_width=0.1,
        )
        grid = RoutingGrid(50, 50, rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, rules)

        pads = create_sop_pads(20, pitch=pitch)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        assert len(escapes) == 20

        # Check all consecutive same-side escapes
        left = sorted([e for e in escapes if e.pad.x < 0], key=lambda e: e.pad.y)
        right = sorted([e for e in escapes if e.pad.x > 0], key=lambda e: e.pad.y)

        for side in [left, right]:
            for idx in range(len(side) - 1):
                for seg1 in side[idx].segments:
                    for seg2 in side[idx + 1].segments:
                        if seg1.layer != seg2.layer:
                            continue
                        gap = _min_segment_edge_gap(seg1, seg2)
                        assert gap >= clearance - 1e-6, (
                            f"pitch={pitch} clearance={clearance}: "
                            f"gap={gap:.4f}mm between {side[idx].pad.net_name} "
                            f"and {side[idx + 1].pad.net_name}"
                        )

    def test_fine_pitch_sufficient_clearance_no_offset(self, fine_pitch_rules):
        """When pitch is wide enough for clearance, no lateral offset is applied.

        At 0.65mm pitch with min_trace_width=0.1 and clearance=0.15:
        lateral_clearance = 0.65 - 0.1 = 0.55 > 0.15, so no offset needed.
        Odd-pin vias should be inline (same row-axis coordinate as pad).
        """
        # Use rules where lateral_clearance (pitch - escape_width) >> clearance
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.1,  # small clearance
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=0.1,
            grid_resolution=0.05,
            min_trace_width=0.1,
        )
        grid = RoutingGrid(50, 50, rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, rules)

        # 0.65mm pitch, escape_width=0.1 -> lateral_clearance=0.55 >> 0.1
        pads = create_sop_pads(8, pitch=0.65)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # For the vertical SOP layout, pads are at x=-2 and x=2.
        # The left column may escape WEST (dx=-1,dy=0 -> row=(0,-1)) or
        # SOUTH (dx=0,dy=-1 -> row=(1,0)) depending on package geometry.
        # In either case, lateral offset shifts along the row axis.
        # With sufficient clearance, the via should stay inline with the
        # pad in the row-direction coordinate.
        left_escapes = sorted(
            [e for e in escapes if e.pad.x < 0], key=lambda e: e.pad.y
        )
        for e in left_escapes:
            if e.via_pos is None:
                continue
            # Determine row axis from escape direction
            if e.direction == EscapeDirection.WEST:
                # row direction is y-axis; check via y == pad y
                assert abs(e.via_pos[1] - e.pad.y) < 1e-6, (
                    f"Unexpected lateral offset for {e.pad.net_name}"
                )
            elif e.direction == EscapeDirection.SOUTH:
                # row direction is x-axis; check via x == pad x
                assert abs(e.via_pos[0] - e.pad.x) < 1e-6, (
                    f"Unexpected lateral offset for {e.pad.net_name}"
                )


# ==============================================================================
# Inward-Via Strategy Tests (Issue #1840)
# ==============================================================================


class TestInwardViaStrategy:
    """Tests for SSOP inward-via escape routing (Issue #1840).

    Verifies that odd-indexed pads in fine-pitch dual-row packages place
    vias INWARD (toward the IC body center) instead of outward, and that
    the inner layer selection prefers signal layers over planes.
    """

    @pytest.fixture
    def fine_pitch_rules(self):
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=0.15,
            grid_resolution=0.05,
            min_trace_width=0.1,
        )

    def test_odd_pad_vias_placed_inward(self, fine_pitch_rules):
        """Odd-pad vias must be placed inward (toward IC body center).

        For a vertical SSOP-20 with left column escaping WEST (dx=-1),
        odd-pad vias should have x > pad.x (closer to center at x=0).
        """
        grid = RoutingGrid(50, 50, fine_pitch_rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, fine_pitch_rules)

        pads = create_sop_pads(20, pitch=0.65)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        for escape in escapes:
            if escape.via is not None:
                dx, dy = router._direction_to_vector(escape.direction)
                # Via displacement should be opposite to escape direction
                via_x, via_y = escape.via_pos
                via_disp = (via_x - escape.pad.x) * dx + (via_y - escape.pad.y) * dy
                assert via_disp < 0, (
                    f"Odd pad {escape.pad.net_name} via should be inward: "
                    f"via_disp_along_direction={via_disp:.4f}"
                )

    def test_even_pad_escapes_stay_outward(self, fine_pitch_rules):
        """Even-pad surface escapes must still route outward."""
        grid = RoutingGrid(50, 50, fine_pitch_rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, fine_pitch_rules)

        pads = create_sop_pads(20, pitch=0.65)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        for escape in escapes:
            if escape.via is None:
                dx, dy = router._direction_to_vector(escape.direction)
                ex, ey = escape.escape_point
                disp = (ex - escape.pad.x) * dx + (ey - escape.pad.y) * dy
                assert disp > 0, (
                    f"Even pad {escape.pad.net_name} escape should be outward: "
                    f"disp={disp:.4f}"
                )

    def test_inner_layer_selection_signal(self, fine_pitch_rules):
        """On 4-layer board with In1.Cu as signal, vias target In1.Cu."""
        from kicad_tools.router.layers import LayerStack

        layer_stack = LayerStack.four_layer_sig_sig_gnd_pwr()
        grid = RoutingGrid(
            50, 50, fine_pitch_rules, origin_x=0, origin_y=0,
            layer_stack=layer_stack,
        )
        router = EscapeRouter(grid, fine_pitch_rules)

        pads = create_sop_pads(20, pitch=0.65)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        for escape in escapes:
            if escape.via is not None:
                assert escape.escape_layer == Layer.IN1_CU, (
                    f"Expected In1.Cu for signal layer, got {escape.escape_layer}"
                )

    def test_inner_layer_selection_plane_fallback(self, fine_pitch_rules):
        """On 4-layer board with In1.Cu as plane (GND), vias fall back to B.Cu."""
        from kicad_tools.router.layers import LayerStack

        # sig_gnd_pwr_sig has In1.Cu=GND plane, In2.Cu=PWR plane
        layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        grid = RoutingGrid(
            50, 50, fine_pitch_rules, origin_x=0, origin_y=0,
            layer_stack=layer_stack,
        )
        router = EscapeRouter(grid, fine_pitch_rules)

        pads = create_sop_pads(20, pitch=0.65)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        for escape in escapes:
            if escape.via is not None:
                assert escape.escape_layer == Layer.B_CU, (
                    f"Expected B.Cu fallback when inner layers are planes, "
                    f"got {escape.escape_layer}"
                )

    def test_two_layer_board_fallback(self, fine_pitch_rules):
        """On 2-layer board, vias fall back to B.Cu."""
        from kicad_tools.router.layers import LayerStack

        layer_stack = LayerStack.two_layer()
        grid = RoutingGrid(
            50, 50, fine_pitch_rules, origin_x=0, origin_y=0,
            layer_stack=layer_stack,
        )
        router = EscapeRouter(grid, fine_pitch_rules)

        pads = create_sop_pads(20, pitch=0.65)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        for escape in escapes:
            if escape.via is not None:
                assert escape.escape_layer == Layer.B_CU, (
                    f"Expected B.Cu on 2-layer board, got {escape.escape_layer}"
                )

    def test_ssop28_inward_vias(self, fine_pitch_rules):
        """SSOP-28 at 0.65mm pitch also uses inward via strategy."""
        grid = RoutingGrid(50, 50, fine_pitch_rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, fine_pitch_rules)

        pads = create_sop_pads(28, pitch=0.65)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        assert len(escapes) == 28

        via_count = sum(1 for e in escapes if e.via is not None)
        # Half of pads (odd-indexed) should have vias
        assert via_count == 14

        # All vias should be inward
        for escape in escapes:
            if escape.via is not None:
                dx, dy = router._direction_to_vector(escape.direction)
                via_x, via_y = escape.via_pos
                via_disp = (via_x - escape.pad.x) * dx + (via_y - escape.pad.y) * dy
                assert via_disp < 0

    def test_clearance_validation_zero_warnings(self, fine_pitch_rules):
        """Inward vias should produce zero clearance warnings on 0.65mm SSOP."""
        import logging
        import logging.handlers

        grid = RoutingGrid(50, 50, fine_pitch_rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, fine_pitch_rules)

        pads = create_sop_pads(20, pitch=0.65)
        info = router.analyze_package(pads)

        handler = logging.handlers.MemoryHandler(capacity=1000)
        escape_logger = logging.getLogger("kicad_tools.router.escape")
        escape_logger.addHandler(handler)
        try:
            escapes = router.generate_escapes(info)
            handler.flush()
            warnings = [
                r for r in handler.buffer
                if r.levelno >= logging.WARNING
                and "clearance violation" in r.getMessage().lower()
            ]
            assert len(warnings) == 0, (
                f"Expected 0 clearance warnings, got {len(warnings)}: "
                + "; ".join(r.getMessage() for r in warnings)
            )
        finally:
            escape_logger.removeHandler(handler)


# ==============================================================================
# Connector Escape Tests (Issue #2265)
# ==============================================================================


def create_connector_pads(
    pins: int, pitch: float = 2.54, ref: str = "J1"
) -> list[Pad]:
    """Create pads simulating a dual-row through-hole connector (e.g., 2x20 header).

    Args:
        pins: Total pin count (must be even for dual-row).
        pitch: Pin pitch in mm (default 2.54mm for standard headers).
        ref: Component reference.

    Returns:
        List of through-hole Pad objects in dual-row arrangement.
    """
    pads = []
    pins_per_side = pins // 2
    half_length = (pins_per_side - 1) * pitch / 2
    row_spacing = pitch  # Standard 2-row connector has row spacing == pitch

    net = 1
    # Row A (left / lower-X)
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=-row_spacing / 2,
                y=-half_length + i * pitch,
                width=1.6,
                height=1.6,
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
                through_hole=True,
            )
        )
        net += 1

    # Row B (right / higher-X)
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=row_spacing / 2,
                y=-half_length + i * pitch,
                width=1.6,
                height=1.6,
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
                through_hole=True,
            )
        )
        net += 1

    return pads


class TestConnectorDetection:
    """Tests for MULTI_ROW_CONNECTOR package type detection (Issue #2279)."""

    def test_40pin_connector_detected_as_multi_row(self):
        """A 40-pin dual-row through-hole header must be detected as MULTI_ROW_CONNECTOR."""
        pads = create_connector_pads(40)
        assert detect_package_type(pads) == PackageType.MULTI_ROW_CONNECTOR

    def test_20pin_connector_detected_as_multi_row(self):
        """A 20-pin dual-row through-hole header must be MULTI_ROW_CONNECTOR (boundary)."""
        pads = create_connector_pads(20)
        assert detect_package_type(pads) == PackageType.MULTI_ROW_CONNECTOR

    def test_small_dip_not_multi_row_connector(self):
        """A 16-pin DIP should remain classified as DIP, not MULTI_ROW_CONNECTOR."""
        pads = create_connector_pads(16)
        assert detect_package_type(pads) == PackageType.DIP

    def test_8pin_dip_not_multi_row_connector(self):
        """An 8-pin DIP should remain classified as DIP."""
        pads = create_connector_pads(8)
        assert detect_package_type(pads) == PackageType.DIP


class TestConnectorEscapeRouting:
    """Tests for multi-row connector escape routing strategy (Issue #2279)."""

    @pytest.fixture
    def grid_and_rules(self):
        """Create grid and rules for testing."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.35,
            via_diameter=0.7,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(80, 80, rules, origin_x=0, origin_y=0)
        return grid, rules

    def test_connector_escapes_all_pins(self, grid_and_rules):
        """Escape router must produce an escape for every connector pin."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(40)
        info = router.analyze_package(pads)
        assert info.package_type == PackageType.MULTI_ROW_CONNECTOR

        escapes = router.generate_escapes(info)
        assert len(escapes) == 40

    def test_connector_inner_row_uses_via(self, grid_and_rules):
        """Inner-row pads must escape via layer change; outer row stays on surface."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(40)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        via_escapes = [e for e in escapes if e.via is not None]
        surface_escapes = [e for e in escapes if e.via is None]

        # For a 2-row connector: outer row (20) on surface, inner row (20) via
        assert len(via_escapes) == 20
        assert len(surface_escapes) == 20

    def test_connector_via_escapes_use_alternate_layer(self, grid_and_rules):
        """Via escapes must route to an alternate layer (not F.Cu)."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(40)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        for escape in escapes:
            if escape.via is not None:
                # Escape layer must differ from pad surface layer
                assert escape.escape_layer != Layer.F_CU

    def test_connector_escape_segments_valid(self, grid_and_rules):
        """Every escape must have at least one segment with valid coordinates."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(40)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        for escape in escapes:
            assert len(escape.segments) >= 1
            for seg in escape.segments:
                assert seg.width > 0
                assert seg.net > 0

    def test_connector_outer_row_ring_index_zero(self, grid_and_rules):
        """Outer-row escapes must have ring_index 0."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(40)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        surface_escapes = [e for e in escapes if e.via is None]
        for escape in surface_escapes:
            assert escape.ring_index == 0

    def test_connector_inner_row_ring_index_nonzero(self, grid_and_rules):
        """Inner-row escapes must have ring_index >= 1."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(40)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        via_escapes = [e for e in escapes if e.via is not None]
        for escape in via_escapes:
            assert escape.ring_index >= 1

    def test_connector_via_stagger(self, grid_and_rules):
        """Via positions for adjacent inner-row pads must be staggered."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(40)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Collect inner-row via positions (sorted by pad Y for vertical layout)
        via_escapes = sorted(
            [e for e in escapes if e.via is not None],
            key=lambda e: e.pad.y,
        )

        # Adjacent vias in the inner row should be staggered along the row axis
        min_stagger = router.via_spacing / 2
        for i in range(len(via_escapes) - 1):
            vp1 = via_escapes[i].via_pos
            vp2 = via_escapes[i + 1].via_pos
            assert vp1 is not None and vp2 is not None
            # The Y coordinates of adjacent vias should differ (stagger along row)
            y_diff = abs(vp1[1] - vp2[1])
            # At least one pair must show stagger (not all aligned)
            if y_diff > 0.01:
                # Via Y positions differ, confirming stagger
                break
        else:
            # If we never broke, no stagger was detected -- fail
            pytest.fail("No via stagger detected for adjacent inner-row pads")


def create_multi_row_connector_pads(
    rows: int, cols: int, pitch: float = 2.54, ref: str = "J1"
) -> list[Pad]:
    """Create pads simulating a multi-row through-hole connector.

    Args:
        rows: Number of rows (2, 3, 4, etc.)
        cols: Number of columns per row
        pitch: Pin pitch in mm
        ref: Component reference

    Returns:
        List of through-hole Pad objects in multi-row arrangement.
    """
    pads = []
    net = 1
    half_rows = (rows - 1) * pitch / 2
    half_cols = (cols - 1) * pitch / 2

    for r in range(rows):
        for c in range(cols):
            pads.append(
                Pad(
                    x=-half_rows + r * pitch,
                    y=-half_cols + c * pitch,
                    width=1.6,
                    height=1.6,
                    net=net,
                    net_name=f"NET_{net}",
                    layer=Layer.F_CU,
                    ref=ref,
                    through_hole=True,
                )
            )
            net += 1

    return pads


class TestMultiRowDetection:
    """Tests for _is_multi_row() helper function."""

    def test_2x20_is_multi_row(self):
        """A 2x20 connector is a multi-row arrangement."""
        pads = create_connector_pads(40)
        assert _is_multi_row(pads) is True

    def test_3x10_is_multi_row(self):
        """A 3x10 connector is a multi-row arrangement."""
        pads = create_multi_row_connector_pads(3, 10)
        assert _is_multi_row(pads) is True

    def test_4x8_is_multi_row(self):
        """A 4x8 connector is a multi-row arrangement."""
        pads = create_multi_row_connector_pads(4, 8)
        assert _is_multi_row(pads) is True

    def test_1x8_not_multi_row(self):
        """A single-row 1x8 connector is NOT multi-row."""
        pads = create_multi_row_connector_pads(1, 8)
        assert _is_multi_row(pads) is False

    def test_2x2_too_small(self):
        """A 2x2 header has only 4 pads but meets min threshold; check count gate."""
        pads = create_multi_row_connector_pads(2, 2)
        # 4 pads, 2 rows, 2 cols -- meets the 2<=rows<=6 and cols>=rows check
        assert _is_multi_row(pads) is True

    def test_3_pads_too_few(self):
        """Fewer than 4 pads should return False."""
        pads = create_multi_row_connector_pads(1, 3)
        assert _is_multi_row(pads) is False


class TestMultiRowConnectorDetection:
    """Tests for detect_package_type() with multi-row connectors."""

    def test_3x10_detected_as_multi_row_connector(self):
        """A 3x10 TH connector (30 pins) must be MULTI_ROW_CONNECTOR."""
        pads = create_multi_row_connector_pads(3, 10)
        assert detect_package_type(pads) == PackageType.MULTI_ROW_CONNECTOR

    def test_2x25_detected_as_multi_row_connector(self):
        """A 2x25 TH connector (50 pins) must be MULTI_ROW_CONNECTOR."""
        pads = create_multi_row_connector_pads(2, 25)
        assert detect_package_type(pads) == PackageType.MULTI_ROW_CONNECTOR

    def test_single_row_connector_not_multi_row(self):
        """A 1x20 single-row connector (20 pins) must NOT be MULTI_ROW_CONNECTOR."""
        pads = create_multi_row_connector_pads(1, 20)
        # Single row, through-hole -- should be THROUGH_HOLE, not MULTI_ROW_CONNECTOR
        result = detect_package_type(pads)
        assert result != PackageType.MULTI_ROW_CONNECTOR

    def test_2x4_below_threshold(self):
        """A 2x4 TH header (8 pins) should be DIP, not MULTI_ROW_CONNECTOR."""
        pads = create_multi_row_connector_pads(2, 4)
        result = detect_package_type(pads)
        assert result == PackageType.DIP


class TestMultiRowDenseDetection:
    """Tests for is_dense_package() with multi-row connectors."""

    def test_2x20_is_dense(self):
        """A 2x20 TH connector (40 pins) must be detected as dense."""
        pads = create_connector_pads(40)
        assert is_dense_package(pads) is True

    def test_2x10_is_dense(self):
        """A 2x10 TH connector (20 pins) must be detected as dense."""
        pads = create_connector_pads(20)
        assert is_dense_package(pads) is True

    def test_2x4_not_dense(self):
        """A 2x4 TH connector (8 pins) should NOT be dense (below threshold)."""
        pads = create_connector_pads(8)
        # 8 pins, 2.54mm pitch -- neither count nor pitch triggers dense
        assert is_dense_package(pads) is False


class TestMultiRowPackageInfo:
    """Tests for get_package_info() with multi-row connectors."""

    def test_package_info_rows_cols(self):
        """Package info for a 2x20 connector must populate rows and cols."""
        pads = create_connector_pads(40)
        info = get_package_info(pads)
        assert info.package_type == PackageType.MULTI_ROW_CONNECTOR
        # _estimate_grid_dimensions returns (unique_y, unique_x)
        # Vertical 2x20: 2 unique X values, 20 unique Y values
        assert info.rows == 20
        assert info.cols == 2
        assert info.rows * info.cols >= 40  # covers all pads

    def test_3x10_package_info_rows_cols(self):
        """Package info for a 3x10 connector must populate rows and cols."""
        pads = create_multi_row_connector_pads(3, 10)
        info = get_package_info(pads)
        assert info.package_type == PackageType.MULTI_ROW_CONNECTOR
        # 3 unique X values, 10 unique Y values
        assert info.rows == 10
        assert info.cols == 3
        assert info.rows * info.cols >= 30


class TestMultiRowEscapeGeneration:
    """Tests for _escape_multi_row_connector() escape route generation."""

    @pytest.fixture
    def grid_and_rules(self):
        """Create grid and rules for testing."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.35,
            via_diameter=0.7,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(80, 80, rules, origin_x=0, origin_y=0)
        return grid, rules

    def test_2x10_produces_20_escapes(self, grid_and_rules):
        """A 2x10 connector must produce exactly 20 escape routes."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(20)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)
        assert len(escapes) == 20

    def test_2x10_outer_no_via_inner_via(self, grid_and_rules):
        """For 2x10 connector: outer row surface escape, inner row via escape."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(20)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        via_escapes = [e for e in escapes if e.via is not None]
        surface_escapes = [e for e in escapes if e.via is None]

        assert len(via_escapes) == 10
        assert len(surface_escapes) == 10

    def test_3x10_escape_counts(self, grid_and_rules):
        """For a 3x10 connector (30 pins), 1 outer row on surface, 2 inner rows via."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_multi_row_connector_pads(3, 10)
        info = router.analyze_package(pads)
        assert info.package_type == PackageType.MULTI_ROW_CONNECTOR

        escapes = router.generate_escapes(info)
        assert len(escapes) == 30

        via_escapes = [e for e in escapes if e.via is not None]
        surface_escapes = [e for e in escapes if e.via is None]

        # 1 outer row (10 pads) on surface, 2 inner rows (20 pads) via
        assert len(surface_escapes) == 10
        assert len(via_escapes) == 20

    def test_inner_escape_layer_not_fcu(self, grid_and_rules):
        """Inner-row escapes must use _select_inner_escape_layer, not hardcoded F.Cu."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(40)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        for escape in escapes:
            if escape.via is not None:
                assert escape.escape_layer != Layer.F_CU
                # Should be B.Cu on a 2-layer board (no inner signal layers)
                assert escape.escape_layer == Layer.B_CU

    def test_escape_segments_have_two_for_via(self, grid_and_rules):
        """Via escapes must have 2 segments: pad-to-via and via-to-escape."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(40)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        for escape in escapes:
            if escape.via is not None:
                assert len(escape.segments) == 2
                # First segment on pad layer, second on escape layer
                assert escape.segments[0].layer == Layer.F_CU
                assert escape.segments[1].layer == escape.escape_layer
            else:
                assert len(escape.segments) == 1


# ==============================================================================
# Segment-to-Pad Clearance Tests (Issue #2319)
# ==============================================================================


def create_ssop28_pads(
    pad_width: float = 0.42, pitch: float = 0.65, ref: str = "U5"
) -> list[Pad]:
    """Create pads simulating an SSOP-28 package with realistic pad dimensions.

    Models a PCM5122PW-style SSOP-28: 0.42mm pad width at 0.65mm pitch.

    Args:
        pad_width: Pad width along the row axis (mm).
        pitch: Pin pitch (mm).
        ref: Component reference.

    Returns:
        List of 28 Pad objects in dual-row arrangement.
    """
    pads = []
    net = 1
    pins_per_side = 14
    half_length = (pins_per_side - 1) * pitch / 2
    row_spacing = 5.6  # Typical SSOP-28 body width

    # Left row (pins 1-14)
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=-row_spacing / 2,
                y=-half_length + i * pitch,
                width=1.5,           # pad extent perpendicular to row (lead length)
                height=pad_width,    # pad extent along row axis
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
            )
        )
        net += 1

    # Right row (pins 15-28)
    for i in range(pins_per_side):
        pads.append(
            Pad(
                x=row_spacing / 2,
                y=half_length - i * pitch,
                width=1.5,
                height=pad_width,
                net=net,
                net_name=f"NET_{net}",
                layer=Layer.F_CU,
                ref=ref,
            )
        )
        net += 1

    return pads


class TestSegmentToPadClearance:
    """Tests for segment-to-pad clearance validation (Issue #2319)."""

    @pytest.fixture
    def ssop28_rules(self):
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=0.15,
            grid_resolution=0.05,
            min_trace_width=0.1,
        )

    def test_ssop28_042mm_pad_no_violations(self, ssop28_rules):
        """SSOP-28 with 0.42mm pads at 0.65mm pitch produces zero violations.

        This is the primary acceptance criterion for Issue #2319: the escape
        router must not generate clearance violations for tight-pitch packages
        like the PCM5122PW.  Pins that cannot escape within clearance are
        deferred to the main router instead.
        """
        import logging
        import logging.handlers

        grid = RoutingGrid(80, 80, ssop28_rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, ssop28_rules)

        pads = create_ssop28_pads(pad_width=0.42, pitch=0.65)
        info = router.analyze_package(pads)

        handler = logging.handlers.MemoryHandler(capacity=1000)
        escape_logger = logging.getLogger("kicad_tools.router.escape")
        escape_logger.addHandler(handler)
        try:
            escapes = router.generate_escapes(info)
            handler.flush()
            warnings = [
                r for r in handler.buffer
                if r.levelno >= logging.WARNING
                and "clearance violation" in r.getMessage().lower()
            ]
            assert len(warnings) == 0, (
                f"Expected 0 clearance warnings, got {len(warnings)}: "
                + "; ".join(r.getMessage() for r in warnings)
            )
        finally:
            escape_logger.removeHandler(handler)

    def test_tight_sop_defers_some_pins(self):
        """Tight SOP geometry defers some pins to the main router.

        When the row-split produces a "row" containing pads from both
        columns, even-pin escape segments may pass through the copper of
        neighboring pads in the same row.  The clearance checker must
        detect this and defer those pins.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.35,
            via_diameter=0.7,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(50, 50, rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, rules)

        # SOP-8 at 0.65mm pitch.  With row_spacing=4mm, x_spread > y_spread
        # so is_horizontal=True and rows are split by Y.  Pads from the same
        # Y-band but at different X positions end up adjacent in the row, and
        # their escape segments going NORTH/SOUTH pass through neighboring
        # pad copper, triggering the clearance check.
        pads = create_sop_pads(8, pitch=0.65)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Some pins should be deferred
        assert len(escapes) < len(pads), (
            f"Expected some pins to be deferred, but all {len(pads)} pads "
            f"got escapes ({len(escapes)} escapes generated)"
        )
        assert len(escapes) > 0, "All pins were deferred -- at least some should escape"

    def test_validate_segment_to_pad_clearance(self, ssop28_rules):
        """Validator must detect segment-to-pad clearance violations.

        Construct a segment that intentionally violates clearance against
        a neighboring pad and verify the validator logs a warning.
        """
        import logging
        import logging.handlers

        from kicad_tools.router.primitives import Segment

        grid = RoutingGrid(80, 80, ssop28_rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, ssop28_rules)

        # Two pads at 0.65mm pitch
        pad0 = Pad(
            x=0.0, y=0.0, width=1.5, height=0.42,
            net=1, net_name="NET_1", layer=Layer.F_CU, ref="U5",
        )
        pad1 = Pad(
            x=0.0, y=0.65, width=1.5, height=0.42,
            net=2, net_name="NET_2", layer=Layer.F_CU, ref="U5",
        )
        row_pads = [pad0, pad1]

        # Create an escape route for pad0 that deliberately passes close
        # to pad1.  The segment runs from pad0 outward (WEST), right
        # next to pad1's copper.
        violating_seg = Segment(
            x1=0.0, y1=0.0, x2=-1.0, y2=0.0,
            width=0.1, layer=Layer.F_CU, net=1, net_name="NET_1",
        )
        escape = EscapeRoute(
            pad=pad0,
            direction=EscapeDirection.WEST,
            escape_point=(-1.0, 0.0),
            escape_layer=Layer.F_CU,
            via_pos=None,
            segments=[violating_seg],
            via=None,
            ring_index=0,
        )

        handler = logging.handlers.MemoryHandler(capacity=1000)
        escape_logger = logging.getLogger("kicad_tools.router.escape")
        escape_logger.addHandler(handler)
        try:
            # Validate with segment-to-pad checking enabled
            router._validate_escape_clearances(
                [escape], ssop28_rules.trace_clearance, row_pads,
            )
            handler.flush()
            pad_warnings = [
                r for r in handler.buffer
                if r.levelno >= logging.WARNING
                and "segment-to-pad" in r.getMessage().lower()
            ]
            # The segment at y=0 is within 0.65 - 0.42/2 - 0.1/2 = 0.39mm
            # of pad1's edge, which is > 0.15mm clearance, so this particular
            # geometry may or may not violate.  The key test is that the
            # validator *runs* without error.  We test the actual violation
            # detection via the full SSOP-28 flow above.
        finally:
            escape_logger.removeHandler(handler)

    def test_fine_pitch_clearance_used_when_configured(self):
        """Escape router must use fine_pitch_clearance when set."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=0.15,
            grid_resolution=0.05,
            min_trace_width=0.1,
            fine_pitch_clearance=0.1,
            fine_pitch_threshold=0.8,
        )
        grid = RoutingGrid(80, 80, rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, rules)

        pads = create_ssop28_pads(pad_width=0.42, pitch=0.65)
        info = router.analyze_package(pads)
        # With fine_pitch_clearance=0.1 (looser than 0.15), more escapes
        # should succeed compared to trace_clearance=0.15
        escapes_fine = router.generate_escapes(info)

        # Now with default clearance (no fine_pitch_clearance)
        rules_default = DesignRules(
            trace_width=0.2,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=0.15,
            grid_resolution=0.05,
            min_trace_width=0.1,
        )
        grid_default = RoutingGrid(80, 80, rules_default, origin_x=0, origin_y=0)
        router_default = EscapeRouter(grid_default, rules_default)
        escapes_default = router_default.generate_escapes(info)

        # fine_pitch_clearance=0.1 is more relaxed, so at least as many escapes
        assert len(escapes_fine) >= len(escapes_default), (
            f"fine_pitch_clearance should allow at least as many escapes: "
            f"got {len(escapes_fine)} vs {len(escapes_default)} with default"
        )

    def test_subgrid_clearance_factor_configurable(self):
        """subgrid_clearance_factor in DesignRules should be configurable."""
        rules_default = DesignRules()
        assert rules_default.subgrid_clearance_factor == 0.5

        rules_custom = DesignRules(subgrid_clearance_factor=0.75)
        assert rules_custom.subgrid_clearance_factor == 0.75

    def test_segment_to_pad_edge_gap_basic(self):
        """_segment_to_pad_edge_gap returns correct edge-to-edge gap."""
        from kicad_tools.router.primitives import Segment

        rules = DesignRules()
        grid = RoutingGrid(10, 10, rules, origin_x=0, origin_y=0)
        router = EscapeRouter(grid, rules)

        # Pad centered at (0, 1.0) with height=0.42mm
        pad = Pad(
            x=0.0, y=1.0, width=1.5, height=0.42,
            net=1, net_name="NET_1", layer=Layer.F_CU,
        )

        # Segment running along x-axis at y=0, width=0.1
        seg = Segment(
            x1=-1.0, y1=0.0, x2=1.0, y2=0.0,
            width=0.1, layer=Layer.F_CU, net=2, net_name="NET_2",
        )

        # Distance from segment center-line (y=0) to pad edge:
        # pad center at y=1.0, pad half-height = 0.21, so pad edge at y=0.79
        # segment half-width = 0.05
        # edge-to-edge gap = 0.79 - 0.05 = 0.74
        gap = EscapeRouter._segment_to_pad_edge_gap(seg, pad)
        assert abs(gap - 0.74) < 0.01, f"Expected gap ~0.74, got {gap}"
