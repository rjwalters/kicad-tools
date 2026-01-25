"""Tests for fine-pitch SSOP/TSSOP escape routing.

Issue #1090: Router fails on fine-pitch SSOP packages with adjacent signal pins.

This test file validates the escape routing for fine-pitch dual-row packages
(SSOP, TSSOP) where adjacent pins have insufficient clearance for standard
routing between them.
"""

import pytest

from kicad_tools.router.escape import (
    EscapeDirection,
    EscapeRouter,
    PackageInfo,
    PackageType,
    detect_package_type,
    get_package_info,
    is_dense_package,
    is_fine_pitch_ssop,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def make_ssop_pads(
    pin_count: int = 20,
    pitch: float = 0.65,
    ref: str = "U1",
    pad_width: float = 0.35,
    pad_height: float = 1.2,
    row_spacing: float = 5.3,
    start_net: int = 1,
) -> list[Pad]:
    """Create pads for a typical SSOP/TSSOP package.

    Creates a dual-row package with specified pitch and pin count.
    Pins are numbered 1 to pin_count/2 on top row (left to right),
    and pin_count/2+1 to pin_count on bottom row (right to left).

    Args:
        pin_count: Total number of pins (must be even)
        pitch: Pin-to-pin pitch in mm
        ref: Component reference designator
        pad_width: Pad width in mm
        pad_height: Pad height in mm
        row_spacing: Distance between row centers in mm
        start_net: Starting net ID

    Returns:
        List of Pad objects
    """
    assert pin_count % 2 == 0, "Pin count must be even for SSOP"
    pads: list[Pad] = []
    pins_per_row = pin_count // 2

    # Calculate starting X to center the package
    total_width = (pins_per_row - 1) * pitch
    start_x = -total_width / 2

    # Top row (pins 1 to pins_per_row)
    for i in range(pins_per_row):
        pin_num = i + 1
        x = start_x + i * pitch
        y = row_spacing / 2
        pads.append(
            Pad(
                x=x,
                y=y,
                width=pad_width,
                height=pad_height,
                net=start_net + i,
                net_name=f"NET{start_net + i}",
                ref=ref,
                pin=str(pin_num),
                layer=Layer.F_CU,
            )
        )

    # Bottom row (pins pin_count down to pins_per_row+1)
    for i in range(pins_per_row):
        pin_num = pin_count - i
        x = start_x + (pins_per_row - 1 - i) * pitch
        y = -row_spacing / 2
        pads.append(
            Pad(
                x=x,
                y=y,
                width=pad_width,
                height=pad_height,
                net=start_net + pins_per_row + i,
                net_name=f"NET{start_net + pins_per_row + i}",
                ref=ref,
                pin=str(pin_num),
                layer=Layer.F_CU,
            )
        )

    return pads


class TestPackageTypeDetection:
    """Tests for SSOP/TSSOP package type detection."""

    def test_detect_tssop_by_pitch(self):
        """TSSOP with 0.5mm pitch should be detected as TSSOP."""
        pads = make_ssop_pads(pin_count=20, pitch=0.5)
        pkg_type = detect_package_type(pads)
        assert pkg_type == PackageType.TSSOP

    def test_detect_ssop_by_pitch(self):
        """SSOP with 0.65mm pitch should be detected as SSOP."""
        pads = make_ssop_pads(pin_count=20, pitch=0.65)
        pkg_type = detect_package_type(pads)
        assert pkg_type == PackageType.SSOP

    def test_detect_sop_by_pitch(self):
        """Standard SOP with 1.27mm pitch should be detected as SOP."""
        pads = make_ssop_pads(pin_count=16, pitch=1.27)
        pkg_type = detect_package_type(pads)
        assert pkg_type == PackageType.SOP

    def test_ssop_28_detection(self):
        """SSOP-28 with 0.65mm pitch should be detected correctly."""
        pads = make_ssop_pads(pin_count=28, pitch=0.65, ref="U2")
        pkg_type = detect_package_type(pads)
        assert pkg_type == PackageType.SSOP


class TestIsDensePackage:
    """Tests for is_dense_package function with SSOP/TSSOP."""

    def test_ssop_always_dense(self):
        """SSOP with 0.65mm pitch should always be classified as dense."""
        pads = make_ssop_pads(pin_count=20, pitch=0.65)
        assert is_dense_package(pads)

    def test_ssop_dense_regardless_of_clearance(self):
        """Fine-pitch SSOP is dense even with small clearance settings."""
        pads = make_ssop_pads(pin_count=20, pitch=0.65)
        # Even with very small clearance, fine-pitch should be dense
        assert is_dense_package(pads, trace_width=0.1, clearance=0.05)

    def test_tssop_always_dense(self):
        """TSSOP with 0.5mm pitch should always be classified as dense."""
        pads = make_ssop_pads(pin_count=20, pitch=0.5)
        assert is_dense_package(pads)

    def test_soic_not_dense_by_default(self):
        """Standard SOIC with 1.27mm pitch may not be dense with loose rules."""
        pads = make_ssop_pads(pin_count=8, pitch=1.27)
        # With typical trace width and clearance, SOIC can route between pins
        assert not is_dense_package(pads, trace_width=0.2, clearance=0.15)


class TestIsFinePitchSsop:
    """Tests for the is_fine_pitch_ssop helper function."""

    def test_ssop_0p65mm_is_fine_pitch(self):
        """0.65mm pitch SSOP should be detected as fine pitch."""
        pads = make_ssop_pads(pin_count=20, pitch=0.65)
        assert is_fine_pitch_ssop(pads)

    def test_tssop_0p5mm_is_fine_pitch(self):
        """0.5mm pitch TSSOP should be detected as fine pitch."""
        pads = make_ssop_pads(pin_count=20, pitch=0.5)
        assert is_fine_pitch_ssop(pads)

    def test_soic_1p27mm_not_fine_pitch(self):
        """1.27mm pitch SOIC should NOT be fine pitch."""
        pads = make_ssop_pads(pin_count=16, pitch=1.27)
        assert not is_fine_pitch_ssop(pads)

    def test_threshold_boundary(self):
        """Test boundary at 0.75mm threshold."""
        pads_fine = make_ssop_pads(pin_count=16, pitch=0.74)
        pads_not_fine = make_ssop_pads(pin_count=16, pitch=0.76)
        assert is_fine_pitch_ssop(pads_fine)
        assert not is_fine_pitch_ssop(pads_not_fine)


class TestPackageInfo:
    """Tests for get_package_info with SSOP/TSSOP."""

    def test_ssop_package_info(self):
        """Package info should correctly identify SSOP."""
        pads = make_ssop_pads(pin_count=20, pitch=0.65)
        info = get_package_info(pads, trace_width=0.2, clearance=0.2)

        assert info.package_type == PackageType.SSOP
        assert info.pin_count == 20
        assert abs(info.pin_pitch - 0.65) < 0.01
        assert info.is_dense

    def test_tssop_package_info(self):
        """Package info should correctly identify TSSOP."""
        pads = make_ssop_pads(pin_count=28, pitch=0.5)
        info = get_package_info(pads, trace_width=0.15, clearance=0.15)

        assert info.package_type == PackageType.TSSOP
        assert info.pin_count == 28
        assert abs(info.pin_pitch - 0.5) < 0.01
        assert info.is_dense


class TestFinePitchEscapeRouting:
    """Tests for escape routing of fine-pitch SSOP/TSSOP packages."""

    @pytest.fixture
    def ssop_pads(self):
        """Create SSOP-20 pads with 0.65mm pitch."""
        return make_ssop_pads(pin_count=20, pitch=0.65)

    @pytest.fixture
    def tssop_pads(self):
        """Create TSSOP-28 pads with 0.5mm pitch."""
        return make_ssop_pads(pin_count=28, pitch=0.5)

    @pytest.fixture
    def rules(self):
        """Create design rules for fine-pitch routing."""
        return DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.05,
        )

    @pytest.fixture
    def grid(self, rules):
        """Create routing grid."""
        return RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            origin_x=-10.0,
            origin_y=-10.0,
        )

    @pytest.fixture
    def escape_router(self, grid, rules):
        """Create escape router."""
        return EscapeRouter(grid, rules)

    def test_ssop_generates_escape_routes(self, escape_router, ssop_pads):
        """SSOP should generate escape routes for all pins."""
        package_info = escape_router.analyze_package(ssop_pads)
        escapes = escape_router.generate_escapes(package_info)

        assert len(escapes) == len(ssop_pads)

    def test_tssop_generates_escape_routes(self, escape_router, tssop_pads):
        """TSSOP should generate escape routes for all pins."""
        package_info = escape_router.analyze_package(tssop_pads)
        escapes = escape_router.generate_escapes(package_info)

        assert len(escapes) == len(tssop_pads)

    def test_alternating_layers_for_adjacent_pins(self, escape_router, ssop_pads):
        """Adjacent pins should escape on different layers."""
        package_info = escape_router.analyze_package(ssop_pads)
        escapes = escape_router.generate_escapes(package_info)

        # Group escapes by row (top vs bottom based on Y)
        top_escapes = [e for e in escapes if e.pad.y > 0]
        bottom_escapes = [e for e in escapes if e.pad.y < 0]

        # Sort by X position
        top_escapes.sort(key=lambda e: e.pad.x)
        bottom_escapes.sort(key=lambda e: e.pad.x)

        # Check alternating layers in top row
        for i in range(len(top_escapes) - 1):
            escape1 = top_escapes[i]
            escape2 = top_escapes[i + 1]
            # Adjacent pins should have different escape layers
            assert escape1.escape_layer != escape2.escape_layer, (
                f"Adjacent pins {escape1.pad.pin} and {escape2.pad.pin} "
                f"both escape on {escape1.escape_layer}"
            )

    def test_odd_pins_have_vias(self, escape_router, ssop_pads):
        """Odd-indexed pins should have vias for layer transition."""
        package_info = escape_router.analyze_package(ssop_pads)
        escapes = escape_router.generate_escapes(package_info)

        # Group by row
        top_escapes = sorted(
            [e for e in escapes if e.pad.y > 0],
            key=lambda e: e.pad.x,
        )

        # Odd indices (1, 3, 5, ...) should have vias
        for i, escape in enumerate(top_escapes):
            if i % 2 == 1:
                assert escape.via is not None, (
                    f"Odd pin {escape.pad.pin} at index {i} should have a via"
                )
                assert escape.via_pos is not None
            else:
                assert escape.via is None, (
                    f"Even pin {escape.pad.pin} at index {i} should NOT have a via"
                )
                assert escape.via_pos is None

    def test_escape_directions_perpendicular_to_row(self, escape_router, ssop_pads):
        """Escape directions should be perpendicular to the pin row."""
        package_info = escape_router.analyze_package(ssop_pads)
        escapes = escape_router.generate_escapes(package_info)

        for escape in escapes:
            if escape.pad.y > 0:
                # Top row should escape NORTH
                assert escape.direction == EscapeDirection.NORTH
            else:
                # Bottom row should escape SOUTH
                assert escape.direction == EscapeDirection.SOUTH

    def test_segments_have_correct_layers(self, escape_router, ssop_pads):
        """Escape route segments should be on correct layers."""
        package_info = escape_router.analyze_package(ssop_pads)
        escapes = escape_router.generate_escapes(package_info)

        for escape in escapes:
            # First segment should be on pad's layer
            assert escape.segments[0].layer == escape.pad.layer

            if escape.via is not None:
                # If there's a via, last segment should be on escape layer
                assert escape.segments[-1].layer == escape.escape_layer
                assert escape.segments[-1].layer != escape.pad.layer


class TestI2SSignalPins:
    """Test case from issue #1090: I2S signals on adjacent TSSOP pins."""

    def test_i2s_adjacent_pins_route_successfully(self):
        """Adjacent I2S signal pins should escape without clearance conflicts.

        This test recreates the scenario from the bug report:
        - PCM5122 DAC in TSSOP-28 (0.65mm pitch)
        - I2S_BCLK, I2S_LRCLK, I2S_DIN on adjacent pins 3, 4, 5
        """
        # Create TSSOP-28 pads with specific net assignments for I2S signals
        pads = []
        pins_per_row = 14
        pitch = 0.65
        row_spacing = 5.3
        start_x = -(pins_per_row - 1) * pitch / 2

        # Top row
        for i in range(pins_per_row):
            pin_num = i + 1
            x = start_x + i * pitch
            y = row_spacing / 2

            # Assign I2S net names to pins 3, 4, 5
            if pin_num == 3:
                net_name = "I2S_BCLK"
                net_id = 9
            elif pin_num == 4:
                net_name = "I2S_LRCLK"
                net_id = 10
            elif pin_num == 5:
                net_name = "I2S_DIN"
                net_id = 11
            else:
                net_name = f"NET{pin_num}"
                net_id = pin_num

            pads.append(
                Pad(
                    x=x,
                    y=y,
                    width=0.35,
                    height=1.2,
                    net=net_id,
                    net_name=net_name,
                    ref="U4",
                    pin=str(pin_num),
                    layer=Layer.F_CU,
                )
            )

        # Bottom row
        for i in range(pins_per_row):
            pin_num = 28 - i
            x = start_x + (pins_per_row - 1 - i) * pitch
            y = -row_spacing / 2
            pads.append(
                Pad(
                    x=x,
                    y=y,
                    width=0.35,
                    height=1.2,
                    net=pin_num + 100,  # Arbitrary net IDs
                    net_name=f"NET{pin_num + 100}",
                    ref="U4",
                    pin=str(pin_num),
                    layer=Layer.F_CU,
                )
            )

        # Set up routing
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )
        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            origin_x=-10.0,
            origin_y=-10.0,
        )
        escape_router = EscapeRouter(grid, rules)

        # Analyze and generate escapes
        package_info = escape_router.analyze_package(pads)
        assert package_info.package_type in (PackageType.SSOP, PackageType.TSSOP)

        escapes = escape_router.generate_escapes(package_info)

        # Find escapes for I2S pins
        i2s_escapes = [
            e for e in escapes if e.pad.net_name in ("I2S_BCLK", "I2S_LRCLK", "I2S_DIN")
        ]
        assert len(i2s_escapes) == 3

        # Verify they have different escape layers
        escape_layers = {e.escape_layer for e in i2s_escapes}
        assert len(escape_layers) >= 2, (
            "Adjacent I2S pins should escape on different layers to avoid conflicts"
        )

        # Verify specific layer assignments based on pin index
        # Pin 3 (index 2, even) -> F.Cu
        # Pin 4 (index 3, odd) -> B.Cu (via)
        # Pin 5 (index 4, even) -> F.Cu
        bclk_escape = next(e for e in i2s_escapes if e.pad.net_name == "I2S_BCLK")
        lrclk_escape = next(e for e in i2s_escapes if e.pad.net_name == "I2S_LRCLK")
        din_escape = next(e for e in i2s_escapes if e.pad.net_name == "I2S_DIN")

        # BCLK (pin 3, even index) stays on surface
        assert bclk_escape.via is None
        assert bclk_escape.escape_layer == Layer.F_CU

        # LRCLK (pin 4, odd index) goes via to inner layer
        assert lrclk_escape.via is not None
        assert lrclk_escape.escape_layer == Layer.B_CU

        # DIN (pin 5, even index) stays on surface
        assert din_escape.via is None
        assert din_escape.escape_layer == Layer.F_CU
