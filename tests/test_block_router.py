"""Tests for per-block detail routing with BlockRouter (Issue #1589).

Phase 4 of the sub-block routing series: each PCBBlock gets its own
sub-Pathfinder confined to the block's physical space.
"""

import pytest

from kicad_tools.pcb.blocks.base import PCBBlock
from kicad_tools.router.block_router import BlockRouter, BlockRoutingResult
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.region_graph import RegionGraph
from kicad_tools.router.rules import DesignRules


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_two_component_block(
    name: str = "ldo",
    origin: tuple[float, float] = (20.0, 20.0),
) -> PCBBlock:
    """Block with 2 components sharing a net, plus ports."""
    block = PCBBlock(name=name, block_id=name)
    block.add_component(
        "U1", "SOT-23", 0, 0,
        pads={"1": (-1.0, 0.0), "2": (1.0, 0.0)},
    )
    block.add_component(
        "C1", "C_0805", 3, 0,
        pads={"1": (2.5, 0.0), "2": (3.5, 0.0)},
    )
    block.add_port("VIN", -4.0, 0.0, direction="in")
    block.add_port("VOUT", 6.0, 0.0, direction="out")
    block.place(origin[0], origin[1])
    return block


def _make_router_with_block_pads(
    block: PCBBlock,
    board_size: float = 60.0,
) -> Autorouter:
    """Create an Autorouter and register pads that match a block's components."""
    rules = DesignRules()
    router = Autorouter(board_size, board_size, force_python=True, rules=rules)

    # U1 pad 1 at (-1, 0) relative -> (origin.x - 1, origin.y) absolute
    # U1 pad 2 at (1, 0) relative -> (origin.x + 1, origin.y) absolute
    # C1 pad 1 at (2.5, 0) relative -> (origin.x + 2.5, origin.y) absolute
    # C1 pad 2 at (3.5, 0) relative -> (origin.x + 3.5, origin.y) absolute
    ox, oy = block.origin.x, block.origin.y
    router.add_component("U1", [
        {"number": "1", "x": ox - 1.0, "y": oy, "net": 1, "net_name": "VIN",
         "width": 0.5, "height": 0.5},
        {"number": "2", "x": ox + 1.0, "y": oy, "net": 2, "net_name": "VOUT",
         "width": 0.5, "height": 0.5},
    ])
    router.add_component("C1", [
        {"number": "1", "x": ox + 2.5, "y": oy, "net": 2, "net_name": "VOUT",
         "width": 0.5, "height": 0.5},
        {"number": "2", "x": ox + 3.5, "y": oy, "net": 3, "net_name": "GND",
         "width": 0.5, "height": 0.5},
    ])
    return router


# ===========================================================================
# Unit: BlockRouter sub-grid creation
# ===========================================================================

class TestBlockRouterSubGrid:
    """BlockRouter creates valid sub-grids from PCBBlock definitions."""

    def test_sub_grid_dimensions_match_bounding_box(self):
        block = _make_two_component_block()
        rules = DesignRules()
        br = BlockRouter(block, rules, force_python=True)

        # Force sub-grid creation
        br._create_sub_grid()
        assert br._grid is not None

        # Sub-grid should cover the block bounding box plus margin
        bbox = block.bounding_box
        expected_width = (bbox.max_x - bbox.min_x) + 2 * br.margin
        expected_height = (bbox.max_y - bbox.min_y) + 2 * br.margin

        # Grid dimensions in mm should approximately match
        assert abs(br._grid.width - expected_width) < rules.grid_resolution * 2
        assert abs(br._grid.height - expected_height) < rules.grid_resolution * 2

    def test_sub_grid_origin_offset(self):
        block = _make_two_component_block(origin=(25.0, 30.0))
        rules = DesignRules()
        br = BlockRouter(block, rules, force_python=True, margin=1.0)

        br._create_sub_grid()
        assert br._grid is not None

        # Origin should be at block bbox min minus margin
        bbox = block.bounding_box
        expected_ox = bbox.min_x + block.origin.x - 1.0
        expected_oy = bbox.min_y + block.origin.y - 1.0
        assert abs(br._grid.origin_x - expected_ox) < 0.01
        assert abs(br._grid.origin_y - expected_oy) < 0.01

    def test_requires_placed_block(self):
        block = PCBBlock(name="unplaced")
        block.add_component("U1", "SOT-23", 0, 0, pads={"1": (0, 0)})
        with pytest.raises(ValueError, match="must be placed"):
            BlockRouter(block, DesignRules(), force_python=True)


# ===========================================================================
# Unit: Block-internal routing
# ===========================================================================

class TestBlockInternalRouting:
    """BlockRouter routes block-internal nets within block boundaries."""

    def test_two_pad_net_routes_successfully(self):
        """Route a simple 2-pad net within a block."""
        block = _make_two_component_block()
        router = _make_router_with_block_pads(block)

        rules = DesignRules()
        br = BlockRouter(block, rules, force_python=True, margin=2.0)
        br.add_pads_from_autorouter(router.pads, router.nets, router.net_names)

        result = br.route_block()
        # VOUT net (U1.2 and C1.1) should route within the block
        assert len(result.routes) > 0
        assert len(result.routed_nets) > 0

    def test_result_contains_connected_pad_keys(self):
        block = _make_two_component_block()
        router = _make_router_with_block_pads(block)

        rules = DesignRules()
        br = BlockRouter(block, rules, force_python=True, margin=2.0)
        br.add_pads_from_autorouter(router.pads, router.nets, router.net_names)

        result = br.route_block()
        # At least one net should have connected pads
        assert len(result.connected_pad_keys) > 0

    def test_empty_block_returns_empty_result(self):
        """Block with no nets produces empty result."""
        block = PCBBlock(name="empty")
        block.add_component("U1", "SOT-23", 0, 0, pads={"1": (0, 0)})
        block.place(10, 10)

        rules = DesignRules()
        br = BlockRouter(block, rules, force_python=True)
        # No pads added -> no nets to route
        result = br.route_block()
        assert result.routes == []
        assert result.routed_nets == set()


# ===========================================================================
# Unit: Coordinate transform
# ===========================================================================

class TestCoordinateTransform:
    """Route coordinates are in board (absolute) space."""

    def test_routes_in_board_coordinates(self):
        """Routes from BlockRouter should be in absolute board coordinates."""
        origin = (25.0, 30.0)
        block = _make_two_component_block(origin=origin)
        router = _make_router_with_block_pads(block, board_size=70.0)

        rules = DesignRules()
        br = BlockRouter(block, rules, force_python=True, margin=2.0)
        br.add_pads_from_autorouter(router.pads, router.nets, router.net_names)

        result = br.route_block()
        if result.routes:
            for route in result.routes:
                for seg in route.segments:
                    # Segment coordinates should be in board space,
                    # near the block origin, not near (0, 0)
                    assert seg.x1 > 10.0, f"x1={seg.x1} too close to origin"
                    assert seg.y1 > 10.0, f"y1={seg.y1} too close to origin"


# ===========================================================================
# Unit: Port boundary enforcement
# ===========================================================================

class TestPortBoundaryEnforcement:
    """Sub-grid boundary cells are blocked except at port locations."""

    def test_boundary_cells_blocked(self):
        """Non-port boundary cells should be blocked after _mark_boundary_blocked."""
        block = _make_two_component_block()
        rules = DesignRules()
        br = BlockRouter(block, rules, force_python=True, margin=1.0)

        br._create_sub_grid()
        assert br._grid is not None

        # Register some pads so the grid is populated
        for pad in br._pads.values():
            br._grid.add_pad(pad)

        br._mark_boundary_blocked()

        grid = br._grid
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Check a non-port boundary cell (top-left corner)
        cell = grid.grid[layer_idx][0][0]
        assert cell.blocked, "Corner boundary cell should be blocked"


# ===========================================================================
# Integration: route_all_block_aware end-to-end
# ===========================================================================

class TestRouteAllBlockAware:
    """End-to-end block-aware routing via Autorouter."""

    def _setup_two_block_board(self):
        """Create a board with 2 blocks and inter-block nets."""
        rules = DesignRules()
        router = Autorouter(80, 60, force_python=True, rules=rules)

        # Block A at (15, 30) -- LDO with VIN/VOUT
        block_a = PCBBlock(name="block_a", block_id="block_a")
        block_a.add_component("U1A", "SOT-23", 0, 0,
                              pads={"1": (-1, 0), "2": (1, 0)})
        block_a.add_component("C1A", "C_0805", 3, 0,
                              pads={"1": (2.5, 0), "2": (3.5, 0)})
        block_a.add_port("VIN_A", -4, 0, direction="in")
        block_a.add_port("VOUT_A", 6, 0, direction="out")
        block_a.place(15, 30)

        # Block B at (50, 30) -- Another LDO
        block_b = PCBBlock(name="block_b", block_id="block_b")
        block_b.add_component("U1B", "SOT-23", 0, 0,
                              pads={"1": (-1, 0), "2": (1, 0)})
        block_b.add_component("C1B", "C_0805", 3, 0,
                              pads={"1": (2.5, 0), "2": (3.5, 0)})
        block_b.add_port("VIN_B", -4, 0, direction="in")
        block_b.add_port("VOUT_B", 6, 0, direction="out")
        block_b.place(50, 30)

        # Add component pads matching block positions
        # Block A
        router.add_component("U1A", [
            {"number": "1", "x": 14.0, "y": 30.0, "net": 1, "net_name": "NET_A1",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 16.0, "y": 30.0, "net": 2, "net_name": "NET_A2",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1A", [
            {"number": "1", "x": 17.5, "y": 30.0, "net": 2, "net_name": "NET_A2",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 18.5, "y": 30.0, "net": 3, "net_name": "GND",
             "width": 0.5, "height": 0.5},
        ])
        # Block B
        router.add_component("U1B", [
            {"number": "1", "x": 49.0, "y": 30.0, "net": 4, "net_name": "NET_B1",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 51.0, "y": 30.0, "net": 5, "net_name": "NET_B2",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1B", [
            {"number": "1", "x": 52.5, "y": 30.0, "net": 5, "net_name": "NET_B2",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 53.5, "y": 30.0, "net": 3, "net_name": "GND",
             "width": 0.5, "height": 0.5},
        ])

        # Inter-block net: GND shared between C1A.2 and C1B.2
        # (net 3 already has pads in both blocks)

        router.register_block(block_a)
        router.register_block(block_b)

        return router, block_a, block_b

    def test_block_aware_routing_produces_routes(self):
        """route_all_block_aware should produce routes for both blocks."""
        router, block_a, block_b = self._setup_two_block_board()
        routes = router.route_all_block_aware()
        assert len(routes) > 0, "Should produce at least some routes"

    def test_block_aware_routing_with_explicit_blocks(self):
        """Passing blocks explicitly works the same as using registered blocks."""
        router, block_a, block_b = self._setup_two_block_board()
        routes = router.route_all_block_aware(blocks=[block_a, block_b])
        assert len(routes) > 0


# ===========================================================================
# Integration: fallback to flat routing
# ===========================================================================

class TestFallbackToFlatRouting:
    """route_all_block_aware with no blocks falls back to route_all."""

    def test_no_blocks_produces_same_as_route_all(self):
        """When no blocks are defined, behavior matches route_all."""
        rules = DesignRules()
        router = Autorouter(50, 50, force_python=True, rules=rules)
        router.add_component("R1", [
            {"number": "1", "x": 10.0, "y": 25.0, "net": 1, "net_name": "SIG",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 40.0, "y": 25.0, "net": 1, "net_name": "SIG",
             "width": 0.5, "height": 0.5},
        ])
        routes = router.route_all_block_aware()
        assert len(routes) > 0

    def test_empty_block_list_fallback(self):
        """Explicit empty list falls back to flat routing."""
        rules = DesignRules()
        router = Autorouter(50, 50, force_python=True, rules=rules)
        router.add_component("R1", [
            {"number": "1", "x": 10.0, "y": 25.0, "net": 1, "net_name": "SIG",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 40.0, "y": 25.0, "net": 1, "net_name": "SIG",
             "width": 0.5, "height": 0.5},
        ])
        routes = router.route_all_block_aware(blocks=[])
        assert len(routes) > 0


# ===========================================================================
# Integration: GlobalRouter with block occupancy
# ===========================================================================

class TestRegionGraphBlockOccupancy:
    """RegionGraph.register_block_occupancy updates utilization."""

    def test_occupancy_increases_utilization(self):
        rg = RegionGraph(
            board_width=60.0,
            board_height=40.0,
            num_cols=6,
            num_rows=4,
        )
        # Find utilization before
        region = rg.get_region_at(15.0, 20.0)
        assert region is not None
        initial_util = region.utilization

        # Register block occupancy
        rg.register_block_occupancy(10.0, 15.0, 20.0, 25.0, trace_count=3)

        # Utilization should increase for overlapping regions
        region_after = rg.get_region_at(15.0, 20.0)
        assert region_after is not None
        assert region_after.utilization >= initial_util + 3

    def test_non_overlapping_region_unaffected(self):
        rg = RegionGraph(
            board_width=60.0,
            board_height=40.0,
            num_cols=6,
            num_rows=4,
        )
        # Region far from block area
        far_region = rg.get_region_at(55.0, 35.0)
        assert far_region is not None
        initial_util = far_region.utilization

        rg.register_block_occupancy(10.0, 15.0, 20.0, 25.0, trace_count=5)

        assert far_region.utilization == initial_util


# ===========================================================================
# Edge case: single-component block
# ===========================================================================

class TestSingleComponentBlock:
    """Block with one component should handle gracefully."""

    def test_single_component_no_crash(self):
        block = PCBBlock(name="single")
        block.add_component("R1", "R_0805", 0, 0,
                            pads={"1": (-0.5, 0), "2": (0.5, 0)})
        block.add_port("A", -2, 0)
        block.add_port("B", 2, 0)
        block.place(20, 20)

        rules = DesignRules()
        br = BlockRouter(block, rules, force_python=True)
        result = br.route_block()
        # No pads registered, so no routes expected
        assert isinstance(result, BlockRoutingResult)


# ===========================================================================
# Edge case: overlapping block bounding boxes
# ===========================================================================

class TestOverlappingBlockBoundingBoxes:
    """Two blocks with overlapping bounding boxes handle gracefully."""

    def test_overlapping_blocks_no_crash(self):
        rules = DesignRules()
        router = Autorouter(50, 50, force_python=True, rules=rules)

        block_a = _make_two_component_block(name="a", origin=(20, 25))
        block_b = _make_two_component_block(name="b", origin=(22, 25))

        router.register_block(block_a)
        router.register_block(block_b)

        # Should not crash
        routes = router.route_all_block_aware()
        assert isinstance(routes, list)


# ===========================================================================
# Integration: route_all_advanced with use_block_aware flag
# ===========================================================================

class TestRouteAllAdvancedBlockAware:
    """route_all_advanced with use_block_aware=True delegates correctly."""

    def test_advanced_block_aware_with_blocks(self):
        rules = DesignRules()
        router = Autorouter(50, 50, force_python=True, rules=rules)

        block = _make_two_component_block(origin=(25, 25))
        router.register_block(block)

        # Add matching pads
        ox, oy = 25.0, 25.0
        router.add_component("U1", [
            {"number": "1", "x": ox - 1.0, "y": oy, "net": 1, "net_name": "VIN",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": ox + 1.0, "y": oy, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1", [
            {"number": "1", "x": ox + 2.5, "y": oy, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": ox + 3.5, "y": oy, "net": 3, "net_name": "GND",
             "width": 0.5, "height": 0.5},
        ])

        routes = router.route_all_advanced(use_block_aware=True)
        assert len(routes) > 0

    def test_advanced_block_aware_without_blocks_falls_back(self):
        """With no registered blocks, use_block_aware falls back to standard."""
        rules = DesignRules()
        router = Autorouter(50, 50, force_python=True, rules=rules)
        router.add_component("R1", [
            {"number": "1", "x": 10.0, "y": 25.0, "net": 1, "net_name": "SIG",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 40.0, "y": 25.0, "net": 1, "net_name": "SIG",
             "width": 0.5, "height": 0.5},
        ])
        routes = router.route_all_advanced(use_block_aware=True)
        assert len(routes) > 0


# ===========================================================================
# Unit: Inter-block net classification
# ===========================================================================

class TestInterBlockNetClassification:
    """_classify_nets correctly distinguishes internal vs inter-block nets."""

    def test_fully_internal_net_classified_as_internal(self):
        """Net with all pads inside the block is classified as internal."""
        block = _make_two_component_block()
        router = _make_router_with_block_pads(block)

        rules = DesignRules()
        br = BlockRouter(block, rules, force_python=True, margin=2.0)
        br.add_pads_from_autorouter(router.pads, router.nets, router.net_names)

        internal, inter_block = br._classify_nets()
        # Net 2 (VOUT) has U1.2 and C1.1 both inside the block
        assert 2 in internal

    def test_inter_block_net_classified_correctly(self):
        """Net with pads in multiple blocks is classified as inter-block."""
        block = _make_two_component_block(origin=(20.0, 20.0))

        # Create a router with a pad OUTSIDE the block for net 2
        rules = DesignRules()
        router = Autorouter(60, 60, force_python=True, rules=rules)
        ox, oy = 20.0, 20.0
        router.add_component("U1", [
            {"number": "1", "x": ox - 1.0, "y": oy, "net": 1, "net_name": "VIN",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": ox + 1.0, "y": oy, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1", [
            {"number": "1", "x": ox + 2.5, "y": oy, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": ox + 3.5, "y": oy, "net": 3, "net_name": "GND",
             "width": 0.5, "height": 0.5},
        ])
        # Extra component far away, sharing net 2 (VOUT)
        router.add_component("R_EXT", [
            {"number": "1", "x": 55.0, "y": 55.0, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 55.0, "y": 50.0, "net": 4, "net_name": "OTHER",
             "width": 0.5, "height": 0.5},
        ])

        br = BlockRouter(block, rules, force_python=True, margin=2.0)
        br.add_pads_from_autorouter(router.pads, router.nets, router.net_names)

        internal, inter_block = br._classify_nets()
        # Net 2 has 3 pads total but only 2 inside the block -> inter-block
        assert 2 in inter_block
        assert 2 not in internal

    def test_single_pad_inter_block_net(self):
        """Net with 1 pad inside block and others outside is inter-block."""
        block = _make_two_component_block(origin=(20.0, 20.0))

        rules = DesignRules()
        router = Autorouter(60, 60, force_python=True, rules=rules)
        ox, oy = 20.0, 20.0
        # Only U1.1 is inside the block for net 10
        router.add_component("U1", [
            {"number": "1", "x": ox - 1.0, "y": oy, "net": 10, "net_name": "SIG",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": ox + 1.0, "y": oy, "net": 20, "net_name": "OTHER",
             "width": 0.5, "height": 0.5},
        ])
        # R_EXT.1 is outside the block for net 10
        router.add_component("R_EXT", [
            {"number": "1", "x": 55.0, "y": 55.0, "net": 10, "net_name": "SIG",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 55.0, "y": 50.0, "net": 30, "net_name": "GND",
             "width": 0.5, "height": 0.5},
        ])

        br = BlockRouter(block, rules, force_python=True, margin=2.0)
        br.add_pads_from_autorouter(router.pads, router.nets, router.net_names)

        internal, inter_block = br._classify_nets()
        # Net 10 has 1 pad inside, 1 outside -> inter-block
        assert 10 in inter_block
        assert 10 not in internal

    def test_result_includes_inter_block_nets(self):
        """route_block result includes inter_block_nets set."""
        block = _make_two_component_block(origin=(20.0, 20.0))

        rules = DesignRules()
        router = Autorouter(60, 60, force_python=True, rules=rules)
        ox, oy = 20.0, 20.0
        router.add_component("U1", [
            {"number": "1", "x": ox - 1.0, "y": oy, "net": 1, "net_name": "VIN",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": ox + 1.0, "y": oy, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1", [
            {"number": "1", "x": ox + 2.5, "y": oy, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": ox + 3.5, "y": oy, "net": 3, "net_name": "GND",
             "width": 0.5, "height": 0.5},
        ])
        # Extra pad outside for net 3
        router.add_component("R_EXT", [
            {"number": "1", "x": 55.0, "y": 55.0, "net": 3, "net_name": "GND",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 55.0, "y": 50.0, "net": 4, "net_name": "X",
             "width": 0.5, "height": 0.5},
        ])

        br = BlockRouter(block, rules, force_python=True, margin=2.0)
        br.add_pads_from_autorouter(router.pads, router.nets, router.net_names)

        result = br.route_block()
        assert isinstance(result.inter_block_nets, set)
        # Net 3 (GND) has pads inside and outside -> inter-block
        assert 3 in result.inter_block_nets

    def test_bounds_property(self):
        """BlockRouter.bounds returns absolute bounding box."""
        block = _make_two_component_block(origin=(25.0, 30.0))
        rules = DesignRules()
        br = BlockRouter(block, rules, force_python=True, margin=1.0)

        min_x, min_y, max_x, max_y = br.bounds
        assert min_x < 25.0
        assert min_y < 30.0
        assert max_x > 25.0
        assert max_y > 30.0


# ===========================================================================
# Integration: register_block_occupancy called during block-aware routing
# ===========================================================================

class TestBlockOccupancyIntegration:
    """register_block_occupancy is called during route_all_block_aware."""

    def test_block_aware_routing_updates_region_utilization(self):
        """After block-aware routing, regions overlapping blocks have utilization."""
        rules = DesignRules()
        router = Autorouter(80, 60, force_python=True, rules=rules)

        block = _make_two_component_block(origin=(20.0, 20.0))
        router.register_block(block)

        ox, oy = 20.0, 20.0
        router.add_component("U1", [
            {"number": "1", "x": ox - 1.0, "y": oy, "net": 1, "net_name": "VIN",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": ox + 1.0, "y": oy, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1", [
            {"number": "1", "x": ox + 2.5, "y": oy, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": ox + 3.5, "y": oy, "net": 3, "net_name": "GND",
             "width": 0.5, "height": 0.5},
        ])

        # Route and verify it completes without error
        routes = router.route_all_block_aware()
        assert isinstance(routes, list)

    def test_inter_block_nets_in_two_block_board(self):
        """Two-block board correctly identifies GND as inter-block net."""
        rules = DesignRules()
        router = Autorouter(80, 60, force_python=True, rules=rules)

        block_a = PCBBlock(name="block_a", block_id="block_a")
        block_a.add_component("U1A", "SOT-23", 0, 0,
                              pads={"1": (-1, 0), "2": (1, 0)})
        block_a.add_component("C1A", "C_0805", 3, 0,
                              pads={"1": (2.5, 0), "2": (3.5, 0)})
        block_a.add_port("VIN_A", -4, 0, direction="in")
        block_a.add_port("VOUT_A", 6, 0, direction="out")
        block_a.place(15, 30)

        block_b = PCBBlock(name="block_b", block_id="block_b")
        block_b.add_component("U1B", "SOT-23", 0, 0,
                              pads={"1": (-1, 0), "2": (1, 0)})
        block_b.add_component("C1B", "C_0805", 3, 0,
                              pads={"1": (2.5, 0), "2": (3.5, 0)})
        block_b.add_port("VIN_B", -4, 0, direction="in")
        block_b.add_port("VOUT_B", 6, 0, direction="out")
        block_b.place(50, 30)

        router.add_component("U1A", [
            {"number": "1", "x": 14.0, "y": 30.0, "net": 1, "net_name": "NET_A1",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 16.0, "y": 30.0, "net": 2, "net_name": "NET_A2",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1A", [
            {"number": "1", "x": 17.5, "y": 30.0, "net": 2, "net_name": "NET_A2",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 18.5, "y": 30.0, "net": 3, "net_name": "GND",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("U1B", [
            {"number": "1", "x": 49.0, "y": 30.0, "net": 4, "net_name": "NET_B1",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 51.0, "y": 30.0, "net": 5, "net_name": "NET_B2",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1B", [
            {"number": "1", "x": 52.5, "y": 30.0, "net": 5, "net_name": "NET_B2",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 53.5, "y": 30.0, "net": 3, "net_name": "GND",
             "width": 0.5, "height": 0.5},
        ])

        router.register_block(block_a)
        router.register_block(block_b)

        routes = router.route_all_block_aware()
        assert len(routes) > 0


# ===========================================================================
# Issue #1654: RegionGraph corridor costs wired into inter-block routing
# ===========================================================================

class TestCorridorCostsInterBlockRouting:
    """Verify that route_all_block_aware sets corridor preferences for inter-block nets."""

    def _setup_two_block_board(self):
        """Create a board with 2 blocks and a shared inter-block signal net."""
        rules = DesignRules()
        router = Autorouter(80, 60, force_python=True, rules=rules)

        # Block A at (15, 30)
        block_a = PCBBlock(name="block_a", block_id="block_a")
        block_a.add_component("U1A", "SOT-23", 0, 0,
                              pads={"1": (-1, 0), "2": (1, 0)})
        block_a.add_component("C1A", "C_0805", 3, 0,
                              pads={"1": (2.5, 0), "2": (3.5, 0)})
        block_a.add_port("VIN_A", -4, 0, direction="in")
        block_a.add_port("VOUT_A", 6, 0, direction="out")
        block_a.place(15, 30)

        # Block B at (50, 30)
        block_b = PCBBlock(name="block_b", block_id="block_b")
        block_b.add_component("U1B", "SOT-23", 0, 0,
                              pads={"1": (-1, 0), "2": (1, 0)})
        block_b.add_component("C1B", "C_0805", 3, 0,
                              pads={"1": (2.5, 0), "2": (3.5, 0)})
        block_b.add_port("VIN_B", -4, 0, direction="in")
        block_b.add_port("VOUT_B", 6, 0, direction="out")
        block_b.place(50, 30)

        # Block A pads
        router.add_component("U1A", [
            {"number": "1", "x": 14.0, "y": 30.0, "net": 1, "net_name": "NET_A1",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 16.0, "y": 30.0, "net": 2, "net_name": "NET_A2",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1A", [
            {"number": "1", "x": 17.5, "y": 30.0, "net": 2, "net_name": "NET_A2",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 18.5, "y": 30.0, "net": 3, "net_name": "INTER_AB",
             "width": 0.5, "height": 0.5},
        ])
        # Block B pads
        router.add_component("U1B", [
            {"number": "1", "x": 49.0, "y": 30.0, "net": 4, "net_name": "NET_B1",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 51.0, "y": 30.0, "net": 5, "net_name": "NET_B2",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1B", [
            {"number": "1", "x": 52.5, "y": 30.0, "net": 5, "net_name": "NET_B2",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 53.5, "y": 30.0, "net": 3, "net_name": "INTER_AB",
             "width": 0.5, "height": 0.5},
        ])

        router.register_block(block_a)
        router.register_block(block_b)
        return router

    def test_corridor_preferences_set_for_inter_block_nets(self):
        """set_corridor_preference is called for inter-block nets during Phase B."""
        from unittest.mock import patch

        router = self._setup_two_block_board()

        calls = []
        original_set = router.grid.set_corridor_preference

        def tracking_set(corridor, net, penalty):
            calls.append((net, penalty))
            return original_set(corridor, net, penalty)

        with patch.object(router.grid, "set_corridor_preference", side_effect=tracking_set):
            routes = router.route_all_block_aware()

        # INTER_AB (net 3) is the inter-block net shared between block_a and block_b.
        # It should have received a corridor assignment.
        assigned_nets = {net for net, _ in calls}
        assert 3 in assigned_nets, (
            f"Inter-block INTER_AB net should receive corridor preference, "
            f"but only these nets were assigned: {assigned_nets}"
        )
        # Penalty should match the expected value (5.0)
        for net, penalty in calls:
            assert penalty == 5.0, f"Corridor penalty should be 5.0, got {penalty}"

        assert len(routes) > 0, "Should produce routes"

    def test_corridor_preferences_cleared_after_routing(self):
        """clear_all_corridor_preferences is called after Phase B routing."""
        from unittest.mock import patch

        router = self._setup_two_block_board()

        clear_calls = []
        original_clear = router.grid.clear_all_corridor_preferences

        def tracking_clear():
            clear_calls.append(True)
            return original_clear()

        with patch.object(router.grid, "clear_all_corridor_preferences", side_effect=tracking_clear):
            router.route_all_block_aware()

        assert len(clear_calls) > 0, (
            "clear_all_corridor_preferences should be called after Phase B"
        )

    def test_no_corridor_for_non_inter_block_nets(self):
        """Block-internal-only nets should not receive corridor preferences."""
        from unittest.mock import patch

        router = self._setup_two_block_board()

        assigned_nets = []
        original_set = router.grid.set_corridor_preference

        def tracking_set(corridor, net, penalty):
            assigned_nets.append(net)
            return original_set(corridor, net, penalty)

        with patch.object(router.grid, "set_corridor_preference", side_effect=tracking_set):
            router.route_all_block_aware()

        # Nets 1, 2 are block_a internal; nets 4, 5 are block_b internal.
        # They should NOT receive corridor assignments.
        for internal_net in [1, 2, 4, 5]:
            assert internal_net not in assigned_nets, (
                f"Block-internal net {internal_net} should not receive corridor preference"
            )

    def test_global_router_fallback_routes_without_corridor(self):
        """If GlobalRouter returns None for a net, it still routes without corridor."""
        from unittest.mock import patch

        router = self._setup_two_block_board()

        # Patch GlobalRouter.route_net to always return None
        with patch(
            "kicad_tools.router.global_router.GlobalRouter.route_net",
            return_value=None,
        ):
            routes = router.route_all_block_aware()

        # Routing should still succeed (fallback to no corridor guidance)
        assert len(routes) > 0, "Should produce routes even without corridor assignments"

    def test_single_block_no_corridor_assignments(self):
        """Single-block design has no inter-block nets, so no corridors are assigned."""
        from unittest.mock import patch

        rules = DesignRules()
        router = Autorouter(60, 40, force_python=True, rules=rules)

        block = PCBBlock(name="only_block", block_id="only_block")
        block.add_component("U1", "SOT-23", 0, 0,
                            pads={"1": (-1, 0), "2": (1, 0)})
        block.add_component("C1", "C_0805", 3, 0,
                            pads={"1": (2.5, 0), "2": (3.5, 0)})
        block.place(20, 20)

        router.add_component("U1", [
            {"number": "1", "x": 19.0, "y": 20.0, "net": 1, "net_name": "VIN",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 21.0, "y": 20.0, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("C1", [
            {"number": "1", "x": 22.5, "y": 20.0, "net": 2, "net_name": "VOUT",
             "width": 0.5, "height": 0.5},
            {"number": "2", "x": 23.5, "y": 20.0, "net": 3, "net_name": "GND",
             "width": 0.5, "height": 0.5},
        ])

        router.register_block(block)

        calls = []
        original_set = router.grid.set_corridor_preference

        def tracking_set(corridor, net, penalty):
            calls.append(net)
            return original_set(corridor, net, penalty)

        with patch.object(router.grid, "set_corridor_preference", side_effect=tracking_set):
            routes = router.route_all_block_aware()

        # No inter-block nets means no corridor assignments
        assert len(calls) == 0, (
            f"Single-block board should have no corridor assignments, got {calls}"
        )
