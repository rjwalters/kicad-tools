"""Tests for same-component pad clearance relaxation (Issue #2452).

When two pads share the same component reference (e.g., crystal Y1 with
OSC_IN and OSC_OUT) but belong to different nets, the standard clearance
envelope can block the entire corridor between them.  The relaxation logic
in ``RoutingGrid._relax_same_component_clearance`` unblocks clearance-only
cells in the overlap region while preserving a reduced clearance of
``trace_width / 2`` around each pad's metal.
"""

import math

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def _make_crystal_pads(
    ref: str = "Y1",
    pitch: float = 2.0,
    pad_w: float = 1.0,
    pad_h: float = 1.3,
    center_x: float = 5.0,
    center_y: float = 5.0,
) -> tuple[Pad, Pad]:
    """Create two SMD pads mimicking a crystal oscillator footprint."""
    pad1 = Pad(
        x=center_x - pitch / 2,
        y=center_y,
        width=pad_w,
        height=pad_h,
        net=1,
        net_name="OSC_IN",
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )
    pad2 = Pad(
        x=center_x + pitch / 2,
        y=center_y,
        width=pad_w,
        height=pad_h,
        net=2,
        net_name="OSC_OUT",
        layer=Layer.F_CU,
        ref=ref,
        pin="2",
    )
    return pad1, pad2


class TestSameComponentClearanceRelaxation:
    """Verify that same-component pad clearance is relaxed correctly."""

    @pytest.fixture
    def fine_grid_rules(self):
        """Design rules with 0.05mm resolution matching the crystal issue."""
        return DesignRules(
            grid_resolution=0.05,
            trace_width=0.2,
            trace_clearance=0.15,
        )

    @pytest.fixture
    def crystal_grid(self, fine_grid_rules):
        """Grid with two crystal pads at 2.0mm pitch."""
        grid = RoutingGrid(width=10.0, height=10.0, rules=fine_grid_rules)
        pad1, pad2 = _make_crystal_pads()
        grid.add_pad(pad1)
        grid.add_pad(pad2)
        return grid

    def test_corridor_has_passable_cells(self, crystal_grid):
        """The corridor between same-component pads must have unblocked cells.

        Before the fix, the full clearance envelopes overlapped and left zero
        passable cells between the two crystal pads.
        """
        grid = crystal_grid
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # The corridor is centered at x=5.0, between pads at x=4.0 and x=6.0.
        # Check a vertical slice at the midpoint.
        mid_gx, mid_gy = grid.world_to_grid(5.0, 5.0)

        passable_count = 0
        for gy in range(max(0, mid_gy - 20), min(grid.rows, mid_gy + 21)):
            if not grid._blocked[layer_idx, gy, mid_gx]:
                passable_count += 1

        assert passable_count > 0, (
            "No passable cells found at corridor midpoint -- "
            "same-component clearance relaxation did not open the corridor"
        )

    def test_pad_metal_cells_remain_blocked(self, crystal_grid):
        """Pad metal cells must never be unblocked by the relaxation."""
        grid = crystal_grid
        layer_idx = grid.layer_to_index(Layer.F_CU.value)

        # Check pad metal area for pad at (4.0, 5.0) with 1.0 x 1.3 mm
        for pad_x, pad_y, pad_w, pad_h in [(4.0, 5.0, 1.0, 1.3), (6.0, 5.0, 1.0, 1.3)]:
            metal_x1 = pad_x - pad_w / 2
            metal_y1 = pad_y - pad_h / 2
            metal_x2 = pad_x + pad_w / 2
            metal_y2 = pad_y + pad_h / 2

            mgx1 = int(math.ceil((metal_x1 - grid.origin_x) / grid.resolution))
            mgy1 = int(math.ceil((metal_y1 - grid.origin_y) / grid.resolution))
            mgx2 = int(math.floor((metal_x2 - grid.origin_x) / grid.resolution))
            mgy2 = int(math.floor((metal_y2 - grid.origin_y) / grid.resolution))

            for gy in range(mgy1, mgy2 + 1):
                for gx in range(mgx1, mgx2 + 1):
                    if 0 <= gx < grid.cols and 0 <= gy < grid.rows:
                        assert grid._pad_blocked[layer_idx, gy, gx], (
                            f"Pad metal cell ({gx}, {gy}) was unblocked by relaxation"
                        )
                        assert grid._blocked[layer_idx, gy, gx], (
                            f"Pad metal cell ({gx}, {gy}) is not blocked"
                        )

    def test_reduced_clearance_maintained(self, crystal_grid):
        """Cells within trace_width/2 of pad metal must remain blocked."""
        grid = crystal_grid
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        reduced = grid.rules.trace_width / 2  # 0.1mm

        # Check that cells immediately adjacent to pad metal (within 0.1mm)
        # are still blocked.  Pad1 metal right edge is at x=4.5.
        # Reduced clearance extends to x=4.6.
        # At 0.05mm resolution, cells at gx for x in [4.5, 4.6] should be blocked.
        for wx in [4.52, 4.55, 4.58]:
            gx, gy = grid.world_to_grid(wx, 5.0)
            if 0 <= gx < grid.cols and 0 <= gy < grid.rows:
                assert grid._blocked[layer_idx, gy, gx], (
                    f"Cell at world ({wx}, 5.0) -> grid ({gx}, {gy}) within "
                    f"reduced clearance should remain blocked"
                )

    def test_no_relaxation_for_different_components(self):
        """Pads on different components should not have clearance relaxation."""
        rules = DesignRules(
            grid_resolution=0.05,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)

        # Place pads close enough that clearance zones overlap (1.5mm pitch,
        # 1.0mm pad width, clearance = 0.25mm each side -> zones overlap by
        # 0.5mm at the midpoint).
        pad1 = Pad(
            x=4.25,
            y=5.0,
            width=1.0,
            height=1.3,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            ref="Y1",
            pin="1",
        )
        pad2 = Pad(
            x=5.75,
            y=5.0,
            width=1.0,
            height=1.3,
            net=2,
            net_name="NET2",
            layer=Layer.F_CU,
            ref="R1",
            pin="1",
        )
        grid.add_pad(pad1)
        grid.add_pad(pad2)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        mid_gx, mid_gy = grid.world_to_grid(5.0, 5.0)

        # At the midpoint, the cell should still be blocked because the pads
        # are on different components -- no relaxation should have occurred.
        assert grid._blocked[layer_idx, mid_gy, mid_gx], (
            "Cell between different-component pads should remain blocked"
        )

    def test_no_relaxation_for_same_net(self):
        """Same-net pads (e.g., ground pads) should not trigger relaxation."""
        rules = DesignRules(
            grid_resolution=0.05,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)

        # Two pads on the same component AND same net
        pad1 = Pad(
            x=4.0,
            y=5.0,
            width=1.0,
            height=1.3,
            net=1,
            net_name="GND",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        pad2 = Pad(
            x=6.0,
            y=5.0,
            width=1.0,
            height=1.3,
            net=1,
            net_name="GND",
            layer=Layer.F_CU,
            ref="U1",
            pin="2",
        )
        grid.add_pad(pad1)

        # Count blocked cells at midpoint before second pad
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        mid_gx, mid_gy = grid.world_to_grid(5.0, 5.0)
        blocked_before = bool(grid._blocked[layer_idx, mid_gy, mid_gx])

        grid.add_pad(pad2)

        # Same-net pads already share clearance zones via the net check in
        # add_pad; the relaxation method skips them.  This test just verifies
        # no crash or unexpected behavior.
        # (Same-net cells are passable by the standard blocked-different-net check.)

    def test_component_pads_tracked(self, crystal_grid):
        """Verify _component_pads dict is populated correctly."""
        assert "Y1" in crystal_grid._component_pads
        assert len(crystal_grid._component_pads["Y1"]) == 2

    def test_relaxation_with_zero_net_pad_skipped(self):
        """Pads with net=0 (unconnected) should not trigger relaxation."""
        rules = DesignRules(
            grid_resolution=0.05,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)

        pad1 = Pad(
            x=4.0,
            y=5.0,
            width=1.0,
            height=1.3,
            net=0,
            net_name="",
            layer=Layer.F_CU,
            ref="Y1",
            pin="1",
        )
        pad2 = Pad(
            x=6.0,
            y=5.0,
            width=1.0,
            height=1.3,
            net=2,
            net_name="OSC_OUT",
            layer=Layer.F_CU,
            ref="Y1",
            pin="2",
        )
        grid.add_pad(pad1)
        grid.add_pad(pad2)

        # Should not crash and should not relax (net=0 pad is skipped)
        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        mid_gx, mid_gy = grid.world_to_grid(5.0, 5.0)
        # No assertion on blocked state -- just verify no exception

    def test_sot23_tight_pitch(self):
        """SOT-23 package with 0.95mm pitch should also benefit from relaxation."""
        rules = DesignRules(
            grid_resolution=0.05,
            trace_width=0.127,
            trace_clearance=0.127,
        )
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)

        # SOT-23: 0.6mm pads at 0.95mm pitch
        pad1 = Pad(
            x=5.0 - 0.95 / 2,
            y=5.0,
            width=0.6,
            height=1.0,
            net=1,
            net_name="BASE",
            layer=Layer.F_CU,
            ref="Q1",
            pin="1",
        )
        pad2 = Pad(
            x=5.0 + 0.95 / 2,
            y=5.0,
            width=0.6,
            height=1.0,
            net=2,
            net_name="EMITTER",
            layer=Layer.F_CU,
            ref="Q1",
            pin="2",
        )
        grid.add_pad(pad1)
        grid.add_pad(pad2)

        layer_idx = grid.layer_to_index(Layer.F_CU.value)
        mid_gx, mid_gy = grid.world_to_grid(5.0, 5.0)

        passable = 0
        for gy in range(max(0, mid_gy - 15), min(grid.rows, mid_gy + 16)):
            if not grid._blocked[layer_idx, gy, mid_gx]:
                passable += 1

        assert passable > 0, "SOT-23 corridor should have passable cells after relaxation"

    def test_through_hole_pads_relaxation(self):
        """Through-hole pads on the same component should also be relaxed.

        Post-#2915/#2940: cells in the same-component carve-out overlap can
        carry ``is_obstacle = True`` from the rect-aware first-touch fix.
        Issue #2961's fix keeps those cells ``_blocked = True`` so foreign
        nets cannot clip them, but the pathfinder's ``different_net = (cell.net
        != routing_net)`` mask still admits own-net traces.  Therefore we
        assert the post-fix invariant: cells in the carve-out are passable
        for an own-net trace via the pathfinder, not that ``_blocked == False``
        (which was a pre-#2915 implementation detail).
        """
        rules = DesignRules(
            grid_resolution=0.05,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)

        # Use 1.8mm pitch with 1.5mm pads so clearance zones clearly overlap.
        # Gap = 1.8 - 1.5 = 0.3mm, clearance = 0.25mm each side -> overlap.
        pad1 = Pad(
            x=4.1,
            y=5.0,
            width=1.5,
            height=1.5,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            ref="J1",
            pin="1",
            through_hole=True,
            drill=0.8,
        )
        pad2 = Pad(
            x=5.9,
            y=5.0,
            width=1.5,
            height=1.5,
            net=2,
            net_name="NET2",
            layer=Layer.F_CU,
            ref="J1",
            pin="2",
            through_hole=True,
            drill=0.8,
        )
        grid.add_pad(pad1)
        grid.add_pad(pad2)

        # Check that pad1's own net can reach corridor cells on every layer.
        # We use ``compute_expanded_blocked`` (the Python pathfinder mirror)
        # with radius=0 to isolate the per-cell decision.  For an own-net
        # trace (``cell.net == routing_net``), the pathfinder treats the
        # cell as passable regardless of ``is_obstacle`` -- this is the
        # same-net escape semantics preserved by Issues #2915/#2940/#2961.
        mid_gx, mid_gy = grid.world_to_grid(5.0, 5.0)
        for layer_idx in range(grid.num_layers):
            blocked_for_net1 = grid.compute_expanded_blocked(radius=0, net=1, allow_sharing=False)
            passable = 0
            for gy in range(max(0, mid_gy - 20), min(grid.rows, mid_gy + 21)):
                if not blocked_for_net1[layer_idx, gy, mid_gx]:
                    passable += 1
            assert passable > 0, (
                f"Through-hole corridor on layer {layer_idx} should have "
                f"passable cells for pad1's own net (net=1) after relaxation. "
                f"Same-net escape (#2452/#2880/#2908) must survive the "
                f"#2961 carve-out tightening."
            )

    def test_widely_spaced_pads_no_overlap(self):
        """Pads far apart should have no clearance overlap and no relaxation."""
        rules = DesignRules(
            grid_resolution=0.05,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        grid = RoutingGrid(width=20.0, height=10.0, rules=rules)

        pad1 = Pad(
            x=3.0,
            y=5.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET1",
            layer=Layer.F_CU,
            ref="Y1",
            pin="1",
        )
        pad2 = Pad(
            x=17.0,
            y=5.0,
            width=1.0,
            height=1.0,
            net=2,
            net_name="NET2",
            layer=Layer.F_CU,
            ref="Y1",
            pin="2",
        )
        grid.add_pad(pad1)
        grid.add_pad(pad2)

        # Pads are 14mm apart with ~0.8mm clearance zones each -- no overlap.
        # Just verify no crash.
        assert "Y1" in grid._component_pads
        assert len(grid._component_pads["Y1"]) == 2
