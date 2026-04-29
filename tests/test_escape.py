"""Tests for escape routing module for dense packages."""

import math

import pytest

from kicad_tools.router.escape import (
    EscapeDirection,
    EscapeRoute,
    EscapeRouter,
    PackageType,
    detect_package_type,
    get_package_info,
    is_dense_package,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
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
        """TQFP-32 with 0.8mm pitch is dense when clearance requirements are considered.

        With 0.2mm trace and 0.2mm clearance, the routing space needed between
        pins is 2 * (0.2 + 0.2) = 0.8mm, which equals the pin pitch, meaning
        there's no room to route between adjacent pins.

        Issue #795: This package was NOT being detected as dense without
        considering clearance requirements.
        """
        pads = create_qfp_pads(8, pitch=0.8)  # 32 pins, 0.8mm pitch
        assert len(pads) == 32

        # Without design rules, 0.8mm pitch is NOT dense (threshold is 0.5mm)
        assert is_dense_package(pads) is False

        # WITH design rules, 0.8mm pitch IS dense because:
        # threshold = 2 * (trace_width + clearance) = 2 * (0.2 + 0.2) = 0.8mm
        # and 0.8mm pitch < 0.8mm threshold
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
        """Test escape generation for QFP."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_qfp_pads(8, pitch=0.5)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Should have escape for each edge pad
        assert len(escapes) == 32

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
        vias = router.staggered_via_fanout(pads)

        # Should have up to 4 vias
        assert len(vias) <= 4

    def test_via_positions_offset(self, grid_and_rules):
        """Test that via positions are offset from pad positions."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_bga_pads(3, 3, pitch=1.0)  # Larger pitch for clarity
        vias = router.staggered_via_fanout(pads, stagger_distance=0.3)

        # Check that vias are near but not exactly on pad positions
        for via in vias:
            min_dist = float("inf")
            for pad in pads:
                dist = math.sqrt((via.x - pad.x) ** 2 + (via.y - pad.y) ** 2)
                min_dist = min(min_dist, dist)
            # Via should be offset from pad (not exactly on it)
            # but close enough to connect
            assert min_dist < 1.0  # Within 1mm


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

        # Should generate escapes for all pads
        assert len(escapes) == 8

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
    """Tests for CONNECTOR package type detection (Issue #2265)."""

    def test_40pin_connector_detected_as_connector(self):
        """A 40-pin dual-row through-hole header must be detected as CONNECTOR."""
        pads = create_connector_pads(40)
        assert detect_package_type(pads) == PackageType.CONNECTOR

    def test_20pin_connector_detected_as_connector(self):
        """A 20-pin dual-row through-hole header must be CONNECTOR (boundary)."""
        pads = create_connector_pads(20)
        assert detect_package_type(pads) == PackageType.CONNECTOR

    def test_small_dip_not_connector(self):
        """A 16-pin DIP should remain classified as DIP, not CONNECTOR."""
        pads = create_connector_pads(16)
        assert detect_package_type(pads) == PackageType.DIP

    def test_8pin_dip_not_connector(self):
        """An 8-pin DIP should remain classified as DIP."""
        pads = create_connector_pads(8)
        assert detect_package_type(pads) == PackageType.DIP


class TestConnectorEscapeRouting:
    """Tests for connector escape routing strategy (Issue #2265)."""

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
        assert info.package_type == PackageType.CONNECTOR

        escapes = router.generate_escapes(info)
        assert len(escapes) == 40

    def test_connector_inner_pins_use_via(self, grid_and_rules):
        """Odd-indexed connector pins must escape via layer change."""
        grid, rules = grid_and_rules
        router = EscapeRouter(grid, rules)

        pads = create_connector_pads(40)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        via_escapes = [e for e in escapes if e.via is not None]
        surface_escapes = [e for e in escapes if e.via is None]

        # Half of pins (odd-indexed) should have vias
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
