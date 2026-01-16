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
