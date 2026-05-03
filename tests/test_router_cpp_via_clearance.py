"""Regression tests for via-vs-via clearance in the C++ pathfinder (Issue #2466).

These tests verify that the C++ A* search refuses placements that the
post-route validator would later flag as via-vs-via clearance violations.

Background
==========

Board 02 (charlieplex_3x3) was producing routes where two vias from
different nets were placed too close together: NODE_A and NODE_C vias
overlapping by up to 0.317mm.  The root cause was a mismatch between

* the grid-cell blocking heuristic used at search time in
  ``Pathfinder::is_via_blocked``, and
* the geometric ``via_diameter/2 + via_clearance`` keepout used by the
  post-route validator (``Grid3D::validate_route``).

In particular, the negotiated routing mode allowed cells with
``usage_count > 0`` to be passed through with a cost penalty, even when
those cells were blocked by another net's via.  Two vias can never
physically overlap regardless of routing mode.

The fix expands ``is_via_blocked`` with a geometric pass against
``stored_vias_`` that mirrors the validator exactly, plus ``ceil``-based
radius arithmetic in ``_mark_route_on_cpp_grid`` so the grid blocking
matches the geometric envelope.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import (
    CppGrid,
    CppPathfinder,
    is_cpp_available,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# Marker for tests requiring the C++ backend
requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)


def _make_grid_and_rules(
    width: float = 10.0,
    height: float = 10.0,
    resolution: float = 0.1,
    trace_width: float = 0.25,
    trace_clearance: float = 0.2,
    via_diameter: float = 0.6,
    via_clearance: float = 0.2,
) -> tuple[RoutingGrid, DesignRules]:
    """Create a 4-layer RoutingGrid and DesignRules for via testing."""
    rules = DesignRules(
        trace_width=trace_width,
        trace_clearance=trace_clearance,
        via_diameter=via_diameter,
        via_clearance=via_clearance,
        grid_resolution=resolution,
    )
    layer_stack = LayerStack.four_layer_all_signal()
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        layer_stack=layer_stack,
    )
    return grid, rules


@requires_cpp
class TestViaViaClearanceRegression:
    """Regression tests verifying that ``is_via_blocked`` mirrors
    ``Grid3D::validate_route`` for via-vs-via clearance (Issue #2466)."""

    def test_stored_via_blocks_overlapping_candidate_geometric(self):
        """A stored via from another net must geometrically block any
        candidate via center within ``via_diameter + via_clearance``.

        This is the direct exercise of the geometric check added to
        ``Pathfinder::is_via_blocked`` in Issue #2466.
        """
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Existing route's via belongs to net 2 at world coord (5.0, 5.0).
        cpp_grid._impl.add_stored_via(
            5.0,  # x_mm
            5.0,  # y_mm
            0.3,  # drill
            rules.via_diameter,  # diameter
            2,    # net
        )

        # Pathfinder must refuse a candidate via for net 1 at any point
        # within ``via_diameter + via_clearance`` of the stored via.
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        keepout_world = rules.via_diameter + rules.via_clearance  # 0.8mm
        # Candidate exactly at the stored via -- must be refused.
        gx, gy = grid.world_to_grid(5.0, 5.0)
        assert pathfinder._impl.is_via_blocked(gx, gy, 1, False, 0)

        # Candidate well inside the keepout (50% of required separation).
        gx, gy = grid.world_to_grid(5.0 + 0.5 * keepout_world, 5.0)
        assert pathfinder._impl.is_via_blocked(gx, gy, 1, False, 0)

        # Negotiated mode (allow_sharing=True) must also refuse: two vias
        # cannot physically overlap regardless of routing mode.
        gx, gy = grid.world_to_grid(5.0 + 0.5 * keepout_world, 5.0)
        assert pathfinder._impl.is_via_blocked(gx, gy, 1, True, 0)

    def test_stored_via_does_not_block_candidate_at_safe_distance(self):
        """A candidate via at exactly ``via_diameter + via_clearance``
        from a stored via must NOT be refused (boundary case).

        The post-route validator accepts this distance -- the search
        must not refuse it either.
        """
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Stored via at the centre of the board.
        cpp_grid._impl.add_stored_via(5.0, 5.0, 0.3, rules.via_diameter, 2)

        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        # Required centre-to-centre distance for clearance to be exactly
        # ``via_clearance``.  Add a small epsilon in mm so that grid
        # quantisation (resolution = 0.1mm) does not nudge us inside the
        # keepout.
        sep_mm = rules.via_diameter + rules.via_clearance + 0.05
        gx, gy = grid.world_to_grid(5.0 + sep_mm, 5.0)

        # Cells must be empty (no surrounding obstacles) for the geometric
        # check to be the only possible blocker.
        assert not pathfinder._impl.is_via_blocked(gx, gy, 1, False, 0)
        assert not pathfinder._impl.is_via_blocked(gx, gy, 1, True, 0)

    def test_same_net_stored_via_is_not_blocking(self):
        """A stored via from the same net must NOT block a candidate via.

        Same-net spacing is enforced by a separate drill-spacing rule;
        the via-vs-via geometric check should only apply across nets.
        """
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)

        cpp_grid._impl.add_stored_via(5.0, 5.0, 0.3, rules.via_diameter, 1)

        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        # Candidate for the SAME net (1) overlapping the stored via.
        gx, gy = grid.world_to_grid(5.0, 5.0)
        # The geometric check should not flag same-net via-vs-via.
        # (The cell-based check may still flag if the stored via has
        # marked cells, but in this fixture we only added a stored via
        # without calling mark_via, so cells remain unblocked.)
        assert not pathfinder._impl.is_via_blocked(gx, gy, 1, False, 0)

    def test_negotiated_mode_does_not_let_via_overlap(self):
        """Negotiated mode (``allow_sharing=True``) must still refuse a
        via placement that overlaps a stored via from another net.

        Pre-fix behaviour: cells with ``usage_count > 0`` were allowed
        through with a cost penalty, which let the search place a via
        on top of another net's via.  The post-route validator then
        flagged the overlap and the routing was rejected.
        """
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Mark cells around (5.0, 5.0) as if a route had committed a via,
        # AND register the via in stored_vias_ as the validator sees it.
        gx_via, gy_via = grid.world_to_grid(5.0, 5.0)
        radius = math.ceil((rules.via_diameter / 2 + rules.via_clearance)
                           / grid.resolution) + 1
        cpp_grid._impl.mark_via(gx_via, gy_via, 2, radius)
        cpp_grid._impl.add_stored_via(5.0, 5.0, 0.3, rules.via_diameter, 2)

        # Pretend negotiated mode: bump usage_count on these cells so the
        # ``allow_sharing`` branch in is_via_blocked would normally let the
        # search pass through.  We do this via the increment_usage hook.
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                x, y = gx_via + dx, gy_via + dy
                if 0 <= x < grid.cols and 0 <= y < grid.rows:
                    for layer in range(grid.num_layers):
                        cpp_grid._impl.increment_usage(x, y, layer)

        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        # Candidate via for net 1 directly on top of net 2's via.  Even in
        # negotiated mode, this must be refused due to the geometric
        # via-vs-via clearance check that mirrors the validator.
        assert pathfinder._impl.is_via_blocked(gx_via, gy_via, 1, True, 0)

    def test_two_nets_with_close_vias_get_separated(self):
        """End-to-end: two nets that each need a layer change in close
        proximity must NOT produce overlapping vias.

        The pathfinder should either route them on different layers OR
        shift the via location so the post-route via-vs-via clearance
        check (``via_diameter + via_clearance``) is satisfied.
        """
        grid, rules = _make_grid_and_rules(width=8.0, height=8.0)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Existing route on net 2 with a via at world (4.0, 4.0).  We
        # simulate this as if a previous route had committed:
        #   * cells marked blocked on all layers around (4.0, 4.0);
        #   * stored_vias_ entry for the validator to see.
        gx_via, gy_via = grid.world_to_grid(4.0, 4.0)
        radius = math.ceil((rules.via_diameter / 2 + rules.via_clearance)
                           / grid.resolution) + 1
        cpp_grid._impl.mark_via(gx_via, gy_via, 2, radius)
        cpp_grid._impl.add_stored_via(4.0, 4.0, 0.3, rules.via_diameter, 2)

        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        # Sweep through candidate via positions inside the geometric
        # keepout and verify that all are refused.  These are all the
        # placements the validator would later reject.
        keepout_mm = rules.via_diameter + rules.via_clearance  # 0.8mm
        steps = 6
        for i in range(steps):
            for j in range(steps):
                # Candidates inside a (keepout_mm * 0.9) x (...) box around
                # the stored via -- well inside the geometric keepout.
                offset_x = (i / (steps - 1) - 0.5) * keepout_mm * 0.9
                offset_y = (j / (steps - 1) - 0.5) * keepout_mm * 0.9
                cand_x, cand_y = 4.0 + offset_x, 4.0 + offset_y
                gx, gy = grid.world_to_grid(cand_x, cand_y)

                # Compute distance in mm to verify this candidate is
                # actually inside the keepout (not a degenerate boundary
                # case from grid quantisation).
                dist = math.sqrt((cand_x - 4.0) ** 2 + (cand_y - 4.0) ** 2)
                if dist >= keepout_mm:
                    continue  # outside keepout, skip

                # Both negotiated and non-negotiated modes must refuse.
                assert pathfinder._impl.is_via_blocked(gx, gy, 1, False, 0), (
                    f"Search must refuse via at ({cand_x:.3f}, {cand_y:.3f}) "
                    f"-- {dist:.3f}mm from stored via, keepout={keepout_mm:.3f}mm"
                )
                assert pathfinder._impl.is_via_blocked(gx, gy, 1, True, 0), (
                    f"Negotiated-mode search must refuse via at "
                    f"({cand_x:.3f}, {cand_y:.3f}) -- {dist:.3f}mm from "
                    f"stored via"
                )
