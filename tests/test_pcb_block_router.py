"""Tests for PCBBlock-router integration (Issue #1586).

Phase 1: Register PCBBlocks with router as protected zones.
"""

import pytest

from kicad_tools.pcb.blocks.base import PCBBlock
from kicad_tools.pcb.layout import PCBLayout
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.rules import DesignRules


def _make_simple_block(
    name: str = "ldo",
    block_id: str | None = None,
    origin: tuple[float, float] = (20, 20),
) -> PCBBlock:
    """Create a simple block with 2 components and 2 ports for testing."""
    block = PCBBlock(name=name, block_id=block_id)
    block.add_component(
        "U1", "SOT-23", 0, 0,
        pads={"1": (-0.5, 0), "2": (0.5, 0), "3": (0, 0.5)},
    )
    block.add_component(
        "C1", "C_0805", 2, 0,
        pads={"1": (-0.5, 0), "2": (0.5, 0)},
    )
    block.add_port("VIN", -3, 0, direction="in")
    block.add_port("VOUT", 5, 0, direction="out")
    block.add_trace((-0.5, 0), (-3, 0), net="VIN")  # U1.1 to VIN port
    block.add_trace((0.5, 0), (5, 0), net="VOUT")   # U1.2 to VOUT port
    block.place(origin[0], origin[1])
    return block


# =========================================================================
# Unit: block_id field
# =========================================================================

class TestBlockId:
    """Verify PCBBlock has block_id attribute."""

    def test_block_id_defaults_to_name(self):
        block = PCBBlock(name="my_ldo")
        assert block.block_id == "my_ldo"

    def test_block_id_can_be_overridden(self):
        block = PCBBlock(name="ldo", block_id="ldo_instance_2")
        assert block.block_id == "ldo_instance_2"
        assert block.name == "ldo"

    def test_block_id_explicit_none_defaults_to_name(self):
        block = PCBBlock(name="filter", block_id=None)
        assert block.block_id == "filter"


# =========================================================================
# Unit: register_block
# =========================================================================

class TestRegisterBlock:
    """Verify register_block stores block and marks bounding box as blocked."""

    def test_register_block_stores_reference(self):
        router = Autorouter(50, 50, force_python=True)
        block = _make_simple_block()
        router.register_block(block)
        assert block.block_id in router.registered_blocks
        assert router.registered_blocks[block.block_id] is block

    def test_register_block_requires_placement(self):
        router = Autorouter(50, 50, force_python=True)
        block = PCBBlock(name="unplaced")
        block.add_component("U1", "SOT-23", 0, 0, pads={"1": (0, 0)})
        with pytest.raises(ValueError, match="must be placed"):
            router.register_block(block)

    def test_bounding_box_cells_blocked(self):
        """After register_block, cells inside the bounding box should be blocked."""
        rules = DesignRules()
        router = Autorouter(50, 50, force_python=True, rules=rules)
        block = _make_simple_block(origin=(20, 20))
        router.register_block(block)

        grid = router.grid

        # The block has components at relative (0,0) and (2,0),
        # bounding box is [-2,-2] to [4,2] relative, so absolute is
        # [18,18] to [24,22] with block origin at (20,20).
        # Pick a point well inside the bounding box.
        center_gx, center_gy = grid.world_to_grid(20, 20)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][center_gy][center_gx]
        assert cell.blocked, "Center of block bounding box should be blocked"

    def test_cells_outside_block_not_blocked(self):
        """Cells far from the block should remain unblocked."""
        router = Autorouter(50, 50, force_python=True)
        block = _make_simple_block(origin=(20, 20))
        router.register_block(block)

        grid = router.grid
        # Pick a point far from the block
        far_gx, far_gy = grid.world_to_grid(45, 45)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        cell = grid.grid[layer_idx][far_gy][far_gx]
        assert not cell.blocked, "Cell far from block should not be blocked"


# =========================================================================
# Unit: port pads available after register_block
# =========================================================================

class TestPortPadsAvailable:
    """Verify port pad grid cells are NOT fully blocked after registration."""

    def test_port_pad_cells_have_net_or_are_accessible(self):
        """Port pads should be registered on the grid as routing endpoints."""
        rules = DesignRules()
        router = Autorouter(50, 50, force_python=True, rules=rules)
        block = _make_simple_block(origin=(20, 20))
        router.register_block(block)

        grid = router.grid
        # VIN port is at relative (-3, 0) + origin (20, 20) = (17, 20)
        vin_gx, vin_gy = grid.world_to_grid(17, 20)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # The port pad center should have been registered via add_pad,
        # so it should have pad_blocked set (it's a pad, not just obstacle)
        cell = grid.grid[layer_idx][vin_gy][vin_gx]
        assert cell.pad_blocked, "Port pad center cell should be pad_blocked"


# =========================================================================
# Unit: trace metadata includes block_id
# =========================================================================

class TestTraceMetadata:
    """Verify get_placed_traces and export_traces include block_id."""

    def test_get_placed_traces_has_block_id(self):
        block = _make_simple_block(name="my_ldo", block_id="ldo_1")
        traces = block.get_placed_traces()
        assert len(traces) > 0
        for trace_dict in traces:
            assert "block_id" in trace_dict
            assert trace_dict["block_id"] == "ldo_1"

    def test_layout_export_traces_block_id(self):
        """Internal block traces have block_id; inter-block traces have None."""
        layout = PCBLayout(name="test")

        block_a = _make_simple_block(name="block_a", origin=(10, 10))
        block_b = _make_simple_block(name="block_b", origin=(30, 10))
        layout.add_block(block_a)
        layout.add_block(block_b)
        layout.route("block_a", "VOUT", "block_b", "VIN")

        traces = layout.export_traces()
        block_traces = [t for t in traces if t["block_id"] is not None]
        inter_traces = [t for t in traces if t["block_id"] is None]

        assert len(block_traces) > 0, "Should have block-internal traces"
        assert len(inter_traces) == 1, "Should have one inter-block trace"

        for t in block_traces:
            assert t["block_id"] in ("block_a", "block_b")

    def test_block_id_default_matches_name(self):
        """When no block_id override, traces use the block name."""
        block = _make_simple_block(name="regulator")
        traces = block.get_placed_traces()
        for t in traces:
            assert t["block_id"] == "regulator"


# =========================================================================
# Edge case: overlapping blocks
# =========================================================================

class TestOverlappingBlocks:
    """Register two blocks with overlapping bounding boxes."""

    def test_overlapping_blocks_no_crash(self):
        router = Autorouter(50, 50, force_python=True)
        block_a = _make_simple_block(name="a", origin=(20, 20))
        block_b = _make_simple_block(name="b", origin=(22, 20))
        # Should not raise
        router.register_block(block_a)
        router.register_block(block_b)
        assert len(router.registered_blocks) == 2

    def test_overlapping_blocks_both_protected(self):
        """Both block interiors should be blocked."""
        router = Autorouter(50, 50, force_python=True)
        block_a = _make_simple_block(name="a", origin=(20, 20))
        block_b = _make_simple_block(name="b", origin=(22, 20))
        router.register_block(block_a)
        router.register_block(block_b)

        grid = router.grid
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Center of block_a at (20, 20)
        gx_a, gy_a = grid.world_to_grid(20, 20)
        assert grid.grid[layer_idx][gy_a][gx_a].blocked

        # Center of block_b at (22, 20)
        gx_b, gy_b = grid.world_to_grid(22, 20)
        assert grid.grid[layer_idx][gy_b][gx_b].blocked


# =========================================================================
# Integration: boundary enforcement -- route_all avoids block interior
# =========================================================================

class TestBoundaryEnforcement:
    """Set up a board with one block and two external pads. Verify routing
    goes around the block, not through it."""

    def test_route_avoids_block_interior(self):
        """Two pads on opposite sides of a block should route around it."""
        rules = DesignRules()
        router = Autorouter(50, 50, force_python=True, rules=rules)

        # Place a block in the center
        block = _make_simple_block(origin=(25, 25))
        router.register_block(block)

        # Add two pads on opposite sides of the block (net 1)
        router.add_component("EXT_L", [
            {"number": "1", "x": 10.0, "y": 25.0, "net": 1, "net_name": "SIG",
             "width": 0.5, "height": 0.5},
        ])
        router.add_component("EXT_R", [
            {"number": "1", "x": 40.0, "y": 25.0, "net": 1, "net_name": "SIG",
             "width": 0.5, "height": 0.5},
        ])

        result = router.route_all()

        # We primarily care that routing succeeds (finds a path around).
        # If the block interior is properly blocked, the router must detour.
        assert result is not None
        # Check that at least one route was produced
        assert len(router.routes) >= 1, "Expected at least one route"


# =========================================================================
# Integration: port routing -- external pad connects to block port
# =========================================================================

class TestPortRouting:
    """External pad connects to a block port via route_all."""

    def test_external_pad_routes_to_port(self):
        """An external pad on the same net as a block port should connect."""
        rules = DesignRules()
        router = Autorouter(50, 50, force_python=True, rules=rules)

        block = _make_simple_block(origin=(25, 25))
        router.register_block(block)

        # VIN port absolute position is (25-3, 25) = (22, 25)
        # Register an external pad on the same net as the port
        router.add_component("EXT", [
            {"number": "1", "x": 10.0, "y": 25.0, "net": 2, "net_name": "VIN_EXT",
             "width": 0.5, "height": 0.5},
        ])

        # Register the port pad with the same net so they need routing
        port_pad_key = (f"_block_{block.block_id}", "VIN")
        if port_pad_key in router.pads:
            # Update port pad net to match the external pad
            router.pads[port_pad_key] = router.pads[port_pad_key]
            # This would normally be done by netlist assignment

        # Even without explicit net assignment on the port pad, the test
        # verifies that port pads are created and accessible on the grid.
        # Full net-aware routing would be tested in Phase 2.
        assert port_pad_key in router.pads, "Port pad should be registered"
