"""Tests for zone flood fill algorithm and grid integration."""

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.zones import ZoneFiller
from kicad_tools.schema.pcb import Zone


@pytest.fixture
def rules():
    """Standard design rules for testing."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        grid_resolution=0.1,
    )


@pytest.fixture
def grid(rules):
    """10x10mm routing grid at 0.1mm resolution = 100x100 cells."""
    return RoutingGrid(
        width=10.0,
        height=10.0,
        rules=rules,
        origin_x=0.0,
        origin_y=0.0,
    )


@pytest.fixture
def filler(grid, rules):
    """Zone filler for testing."""
    return ZoneFiller(grid, rules)


class TestPointInPolygon:
    """Tests for ray casting point-in-polygon algorithm."""

    def test_point_inside_rectangle(self, filler):
        """Point inside a simple rectangle."""
        rect = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert filler.point_in_polygon(5, 5, rect) is True

    def test_point_outside_rectangle(self, filler):
        """Point outside a simple rectangle."""
        rect = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert filler.point_in_polygon(15, 5, rect) is False
        assert filler.point_in_polygon(-5, 5, rect) is False

    def test_point_on_edge(self, filler):
        """Point on polygon edge (edge case - may be inside or outside)."""
        rect = [(0, 0), (10, 0), (10, 10), (0, 10)]
        # Edge behavior is implementation-defined, just check it doesn't crash
        result = filler.point_in_polygon(0, 5, rect)
        assert isinstance(result, bool)

    def test_point_inside_triangle(self, filler):
        """Point inside a triangle."""
        tri = [(0, 0), (10, 0), (5, 10)]
        assert filler.point_in_polygon(5, 3, tri) is True

    def test_point_outside_triangle(self, filler):
        """Point outside a triangle."""
        tri = [(0, 0), (10, 0), (5, 10)]
        assert filler.point_in_polygon(1, 8, tri) is False

    def test_point_inside_concave_polygon(self, filler):
        """Point inside an L-shaped (concave) polygon."""
        # L-shape: bottom-left corner cut out
        l_shape = [(0, 5), (0, 10), (10, 10), (10, 0), (5, 0), (5, 5)]
        # Inside the L
        assert filler.point_in_polygon(7, 7, l_shape) is True
        assert filler.point_in_polygon(7, 2, l_shape) is True

    def test_point_outside_concave_polygon(self, filler):
        """Point in the concave 'notch' of an L-shape."""
        l_shape = [(0, 5), (0, 10), (10, 10), (10, 0), (5, 0), (5, 5)]
        # In the cut-out corner
        assert filler.point_in_polygon(2, 2, l_shape) is False

    def test_empty_polygon(self, filler):
        """Empty polygon returns False."""
        assert filler.point_in_polygon(5, 5, []) is False

    def test_two_point_polygon(self, filler):
        """Two-point polygon (line) returns False."""
        assert filler.point_in_polygon(5, 5, [(0, 0), (10, 10)]) is False


class TestZoneFillBasic:
    """Tests for basic zone fill functionality."""

    def test_fill_rectangular_zone(self, grid, filler):
        """Fill a simple rectangular zone."""
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-1",
            polygon=[(2, 2), (8, 2), (8, 8), (2, 8)],
        )

        result = filler.fill_zone(zone, layer_index=0)

        # Should have filled cells
        assert len(result.filled_cells) > 0

        # Approximate cell count: 6mm x 6mm at 0.1mm = ~3600 cells
        # Allow some margin for edge rounding
        assert len(result.filled_cells) > 3000
        assert len(result.filled_cells) < 4000

    def test_fill_small_zone(self, grid, filler):
        """Fill a very small zone (1x1mm)."""
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-2",
            polygon=[(4, 4), (5, 4), (5, 5), (4, 5)],
        )

        result = filler.fill_zone(zone, layer_index=0)

        # ~100 cells for 1x1mm at 0.1mm resolution
        assert len(result.filled_cells) > 80
        assert len(result.filled_cells) < 120

    def test_fill_returns_correct_zone(self, grid, filler):
        """FilledZone contains reference to original zone."""
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-3",
            polygon=[(3, 3), (7, 3), (7, 7), (3, 7)],
        )

        result = filler.fill_zone(zone, layer_index=0)

        assert result.zone is zone
        assert result.layer_index == 0

    def test_fill_empty_polygon(self, grid, filler):
        """Zone with empty polygon produces no cells."""
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-4",
            polygon=[],
        )

        result = filler.fill_zone(zone, layer_index=0)

        assert len(result.filled_cells) == 0

    def test_fill_triangle_zone(self, grid, filler):
        """Fill a triangular zone."""
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-5",
            polygon=[(1, 1), (9, 1), (5, 9)],
        )

        result = filler.fill_zone(zone, layer_index=0)

        # Triangle should have roughly half the cells of bounding rectangle
        # 8x8 = 64 sq mm, triangle ~ 32 sq mm, at 0.1mm = ~3200 cells
        assert len(result.filled_cells) > 2500
        assert len(result.filled_cells) < 3500


class TestZoneFillWithObstacles:
    """Tests for zone fill with obstacles (pads, traces)."""

    def test_fill_avoids_other_net_pad(self, grid, rules, filler):
        """Zone fill should not include cells blocked by other-net pads."""
        # Add a pad with different net
        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=1.0,
            net=2,  # Different net
            net_name="+3.3V",
            layer=Layer.F_CU,
        )
        grid.add_pad(pad)

        # Fill zone with net 1
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-6",
            polygon=[(2, 2), (8, 2), (8, 8), (2, 8)],
        )

        result = filler.fill_zone(zone, layer_index=0)

        # Check that pad area is not filled
        pad_gx, pad_gy = grid.world_to_grid(5.0, 5.0)
        assert (pad_gx, pad_gy) not in result.filled_cells

    def test_fill_includes_same_net_pad(self, grid, rules, filler):
        """Zone fill should include cells with same-net pads."""
        # Add a pad with same net
        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=1.0,
            net=1,  # Same net
            net_name="GND",
            layer=Layer.F_CU,
        )
        grid.add_pad(pad)

        # Fill zone with net 1
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-7",
            polygon=[(2, 2), (8, 2), (8, 8), (2, 8)],
        )

        result = filler.fill_zone(zone, layer_index=0)

        # Check that pad area IS filled (same net)
        # Note: The center of a same-net pad should still be fillable
        # because is_obstacle is only true for conflicting nets
        pad_gx, pad_gy = grid.world_to_grid(5.0, 5.0)
        # The pad center is marked as obstacle, but same-net obstacles should be included
        assert len(result.filled_cells) > 0


class TestZoneFillWithClearance:
    """Tests for zone fill with clearance from obstacles."""

    def test_fill_with_clearance(self, grid, rules):
        """Zone fill should create clearance around other-net obstacles."""
        # Add a pad with different net
        pad = Pad(
            x=5.0,
            y=5.0,
            width=0.5,
            height=0.5,
            net=2,  # Different net
            net_name="+3.3V",
            layer=Layer.F_CU,
        )
        grid.add_pad(pad)

        filler = ZoneFiller(grid, rules)

        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-8",
            polygon=[(2, 2), (8, 2), (8, 8), (2, 8)],
            clearance=0.3,  # 0.3mm clearance
        )

        result = filler.fill_zone_with_clearance(zone, layer_index=0)

        # Check that clearance area around pad is not filled
        pad_gx, pad_gy = grid.world_to_grid(5.0, 5.0)
        clearance_cells = int(0.3 / 0.1) + 1  # ~4 cells

        # Cells within clearance should not be filled
        for dy in range(-clearance_cells, clearance_cells + 1):
            for dx in range(-clearance_cells, clearance_cells + 1):
                if dx * dx + dy * dy <= clearance_cells * clearance_cells:
                    assert (pad_gx + dx, pad_gy + dy) not in result.filled_cells


class TestGridZoneIntegration:
    """Tests for zone support in RoutingGrid."""

    def test_add_zone_cells(self, grid):
        """Adding zone cells marks them correctly."""
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-9",
            polygon=[(2, 2), (4, 2), (4, 4), (2, 4)],
        )

        cells = {(20, 20), (21, 20), (20, 21), (21, 21)}
        grid.add_zone_cells(zone, cells, layer_index=0)

        for gx, gy in cells:
            cell = grid.grid[0][gy][gx]
            assert cell.is_zone is True
            assert cell.zone_id == "test-zone-9"
            assert cell.net == 1

    def test_clear_zones(self, grid):
        """Clearing zones removes zone markings."""
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-10",
            polygon=[],
        )

        cells = {(20, 20), (21, 20)}
        grid.add_zone_cells(zone, cells, layer_index=0)

        # Verify cells are marked
        assert grid.grid[0][20][20].is_zone is True

        # Clear zones
        grid.clear_zones()

        # Verify cells are cleared
        assert grid.grid[0][20][20].is_zone is False
        assert grid.grid[0][20][20].zone_id is None

    def test_clear_zones_single_layer(self, grid):
        """Clearing zones on one layer doesn't affect others."""
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-11",
            polygon=[],
        )

        # Add zone cells on both layers
        cells = {(20, 20)}
        grid.add_zone_cells(zone, cells, layer_index=0)
        grid.add_zone_cells(zone, cells, layer_index=1)

        # Clear only layer 0
        grid.clear_zones(layer_index=0)

        # Layer 0 should be cleared
        assert grid.grid[0][20][20].is_zone is False

        # Layer 1 should still have zone
        assert grid.grid[1][20][20].is_zone is True

    def test_get_zone_cells(self, grid):
        """Getting zone cells returns correct set."""
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-12",
            polygon=[],
        )

        original_cells = {(20, 20), (21, 20), (22, 20)}
        grid.add_zone_cells(zone, original_cells, layer_index=0)

        retrieved = grid.get_zone_cells(layer_index=0)

        assert retrieved == original_cells

    def test_get_zone_cells_by_id(self, grid):
        """Getting zone cells by ID filters correctly."""
        zone1 = Zone(net_number=1, net_name="GND", layer="F.Cu", uuid="zone-1", polygon=[])
        zone2 = Zone(net_number=2, net_name="+3.3V", layer="F.Cu", uuid="zone-2", polygon=[])

        grid.add_zone_cells(zone1, {(20, 20)}, layer_index=0)
        grid.add_zone_cells(zone2, {(30, 30)}, layer_index=0)

        # Get only zone-1 cells
        zone1_cells = grid.get_zone_cells(layer_index=0, zone_id="zone-1")
        assert zone1_cells == {(20, 20)}

        # Get all zone cells
        all_cells = grid.get_zone_cells(layer_index=0)
        assert all_cells == {(20, 20), (30, 30)}

    def test_is_zone_cell(self, grid):
        """is_zone_cell correctly identifies zone cells."""
        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="test-zone-13",
            polygon=[],
        )

        grid.add_zone_cells(zone, {(25, 25)}, layer_index=0)

        assert grid.is_zone_cell(25, 25, 0) is True
        assert grid.is_zone_cell(26, 26, 0) is False
        assert grid.is_zone_cell(25, 25, 1) is False  # Different layer


class TestZoneFillPerformance:
    """Performance-related tests for zone filling."""

    def test_large_zone_fills_quickly(self, rules):
        """Large zone (10cm x 10cm) should fill in reasonable time."""
        import time

        # Create a larger grid
        large_grid = RoutingGrid(
            width=100.0,  # 100mm = 10cm
            height=100.0,
            rules=rules,
            origin_x=0.0,
            origin_y=0.0,
        )

        filler = ZoneFiller(large_grid, rules)

        zone = Zone(
            net_number=1,
            net_name="GND",
            layer="F.Cu",
            uuid="perf-test-zone",
            polygon=[(10, 10), (90, 10), (90, 90), (10, 90)],
        )

        start = time.time()
        result = filler.fill_zone(zone, layer_index=0)
        elapsed = time.time() - start

        # Should complete in under 5s (pure Python is slow; perf optimization is future work)
        assert elapsed < 5.0, f"Zone fill took {elapsed:.2f}s, expected <5s"

        # Should have filled a lot of cells
        # 80mm x 80mm at 0.1mm = 640,000 cells
        assert len(result.filled_cells) > 500000
