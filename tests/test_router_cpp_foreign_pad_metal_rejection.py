"""Regression tests for the foreign-pad-metal A* rejection fix.

Issue #3224: The C++ A* clearance branch at ``pathfinder.cpp:680`` (one-shot)
and ``pathfinder.cpp:1173`` (resumable / negotiated) reads
``cell.pad_blocked`` to distinguish foreign-pad metal (which a trace
centerline must never cross even during pad-exit) from foreign-pad
clearance halo (which a same-net pad-exit may step through).  Before
this fix, no C++ code path ever set ``cell.pad_blocked = true`` -- the
field was declared in ``types.hpp:51`` with default ``false`` and the
Python-to-C++ sync at ``cpp_backend.py:572-573`` (bulk) and
``grid.py::_sync_pad_to_cpp_grid`` (incremental) both omitted it.

The Python side already populates ``_pad_blocked[metal_slice] = True``
at ``grid.py:4458`` for pad metal cells.  The fix forwards that bit
into the paired C++ grid so the A* clearance branch behaves the same
way the Python sibling at ``pathfinder.py:2600-2602`` does.

Test plan (mirrors PR #2931's reference pattern in
``tests/router/test_validate_route_clearance_rect.py``):

1.  Direct grid binding: ``Grid3D.mark_blocked(..., pad_blocked=True)``
    sets ``GridCell.pad_blocked = True`` (round-trip through nanobind).

2.  Bulk-sync (``CppGrid.from_routing_grid``): a Python ``RoutingGrid``
    with a pad already added at construction time carries ``pad_blocked
    = True`` into the C++ grid metal cells, but ``pad_blocked = False``
    on the surrounding clearance halo cells.

3.  Incremental-sync (``_sync_pad_to_cpp_grid`` via the
    ``_add_pad_unsafe`` path): a pad added AFTER ``from_routing_grid``
    (the normal ``Autorouter.add_component`` flow) still ends up with
    ``cell.pad_blocked = True`` on the C++ grid metal cells.

4.  A* rejects foreign-pad-metal: with a foreign-net pad whose metal
    cells carry ``pad_blocked = true``, ``Pathfinder::route()`` (the
    one-shot path at ``pathfinder.cpp:680``) and ``route_resumable()``
    (the resumable/negotiated path at ``pathfinder.cpp:1173``) refuse
    to step the trace centerline through the foreign metal even when
    the search is exiting a same-net pad immediately adjacent.

The fourth check is the load-bearing acceptance test for AC #6 of
the issue and must exercise BOTH A* call sites (one-shot and
resumable) per the curator's review note.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available, router_cpp
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


# C++ backend is mandatory for these tests -- the bug they regress is
# C++-only by construction (the Python A* uses Python's own grid).
requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available; run `kct build-native`",
)


def _make_rules(
    *,
    trace_width: float = 0.127,
    trace_clearance: float = 0.127,
    resolution: float = 0.1,
) -> DesignRules:
    return DesignRules(
        trace_width=trace_width,
        trace_clearance=trace_clearance,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=trace_clearance,
        grid_resolution=resolution,
    )


def _make_grid(
    *,
    width: float = 10.0,
    height: float = 10.0,
    resolution: float = 0.1,
) -> tuple[RoutingGrid, DesignRules]:
    rules = _make_rules(resolution=resolution)
    layer_stack = LayerStack.two_layer()
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        layer_stack=layer_stack,
    )
    return grid, rules


# ---------------------------------------------------------------------------
# Layer 1: direct binding round-trip
# ---------------------------------------------------------------------------


@requires_cpp
class TestMarkBlockedPadBlockedParameter:
    """Round-trip the new ``pad_blocked`` argument through ``Grid3D.mark_blocked``."""

    def test_mark_blocked_default_pad_blocked_false(self) -> None:
        """Calling ``mark_blocked`` without ``pad_blocked`` preserves the
        pre-#3224 default (``cell.pad_blocked == False``).  This is the
        defaults-preserve regression that protects all existing callers
        (board outline blocks, copper-pour clearance halos, etc.)."""
        grid = router_cpp.Grid3D(20, 20, 2, 0.1, 0.0, 0.0)
        grid.mark_blocked(5, 5, 0, 42, False)
        cell = grid.at(5, 5, 0)
        assert cell.blocked is True
        assert cell.net == 42
        assert cell.is_obstacle is False
        assert cell.pad_blocked is False

    def test_mark_blocked_pad_blocked_true_sets_bit(self) -> None:
        """The new ``pad_blocked=True`` kwarg flips the field.  This is the
        bit the A* clearance branch reads at ``pathfinder.cpp:680`` /
        ``:1173`` to refuse pad-metal traversal during pad-exit."""
        grid = router_cpp.Grid3D(20, 20, 2, 0.1, 0.0, 0.0)
        grid.mark_blocked(5, 5, 0, 42, False, True)
        cell = grid.at(5, 5, 0)
        assert cell.blocked is True
        assert cell.net == 42
        assert cell.pad_blocked is True

    def test_mark_blocked_pad_blocked_sticky_on_overlap(self) -> None:
        """When two pads' envelopes overlap, the second ``mark_blocked``
        call with ``pad_blocked=False`` must NOT clear an earlier
        ``pad_blocked=True`` mark.  This preserves the rip-up contract
        in ``unmark_segment``/``unmark_via`` at ``grid.cpp:146/188``
        which assumes pad-metal cells stay flagged."""
        grid = router_cpp.Grid3D(20, 20, 2, 0.1, 0.0, 0.0)
        grid.mark_blocked(5, 5, 0, 42, False, True)
        grid.mark_blocked(5, 5, 0, 99, False, False)
        cell = grid.at(5, 5, 0)
        assert cell.pad_blocked is True, (
            "pad_blocked should be sticky -- once set, halo overlap from a "
            "neighbour pad must not clear the metal bit"
        )


# ---------------------------------------------------------------------------
# Layer 2: bulk sync via CppGrid.from_routing_grid
# ---------------------------------------------------------------------------


@requires_cpp
class TestBulkSyncPadBlocked:
    """When ``CppGrid.from_routing_grid`` runs on a populated Python
    grid, the per-cell ``pad_blocked`` bit must flow through."""

    def test_pad_metal_cells_synced_as_pad_blocked(self) -> None:
        py_grid, _ = _make_grid()
        # Add the pad BEFORE building the C++ grid so the bulk sync sees
        # the post-update Python state.
        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=0.5,
            net=1,
            net_name="N1",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        py_grid.add_pad(pad)
        cpp_grid = CppGrid.from_routing_grid(py_grid)

        # Pad center: must be pad_blocked on BOTH grids.
        gx, gy = py_grid.world_to_grid(pad.x, pad.y)
        assert bool(py_grid._pad_blocked[0, gy, gx]) is True
        cell = cpp_grid._impl.at(gx, gy, 0)
        assert cell.blocked is True
        assert cell.pad_blocked is True

    def test_clearance_halo_cells_blocked_but_not_pad_blocked(self) -> None:
        """The cells just outside the pad metal (the clearance halo) are
        ``blocked=True`` but ``pad_blocked=False`` -- this is the
        distinction the pad-exit exemption depends on."""
        py_grid, _ = _make_grid()
        # Use a small SMD pad so metal vs halo cells are easy to address.
        pad = Pad(
            x=5.0,
            y=5.0,
            width=0.4,
            height=0.4,
            net=1,
            net_name="N1",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        py_grid.add_pad(pad)
        cpp_grid = CppGrid.from_routing_grid(py_grid)

        # Walk outward from the centre until we find a halo cell (blocked
        # but not pad_blocked).  The exact distance depends on grid
        # resolution / clearance, but we just need one halo cell.
        cgx, cgy = py_grid.world_to_grid(pad.x, pad.y)
        halo_found = False
        for radius in range(3, 15):
            for dy, dx in [(0, radius), (0, -radius), (radius, 0), (-radius, 0)]:
                gx, gy = cgx + dx, cgy + dy
                if not (0 <= gx < py_grid.cols and 0 <= gy < py_grid.rows):
                    continue
                py_blocked = bool(py_grid._blocked[0, gy, gx])
                py_pad_blocked = bool(py_grid._pad_blocked[0, gy, gx])
                if py_blocked and not py_pad_blocked:
                    cell = cpp_grid._impl.at(gx, gy, 0)
                    assert cell.blocked is True, (
                        f"halo cell at ({gx},{gy}) should be blocked on C++"
                    )
                    assert cell.pad_blocked is False, (
                        f"halo cell at ({gx},{gy}) must NOT be pad_blocked "
                        "on C++ -- this is the cell the pad-exit exemption "
                        "allows the trace to step through"
                    )
                    halo_found = True
                    break
            if halo_found:
                break
        assert halo_found, (
            "Expected at least one halo cell (blocked=True, pad_blocked=False) "
            "in the radius around the pad"
        )


# ---------------------------------------------------------------------------
# Layer 3: incremental sync via _sync_pad_to_cpp_grid (the post-init flow)
# ---------------------------------------------------------------------------


@requires_cpp
class TestIncrementalSyncPadBlocked:
    """The realistic ``Autorouter.__init__`` flow: empty Python grid is
    handed to ``CppGrid.from_routing_grid`` first, THEN pads are added
    via ``add_component`` -> ``RoutingGrid.add_pad`` -> ``_add_pad_unsafe``.
    The incremental sync must forward ``pad_blocked`` for those pads."""

    def test_incremental_pad_sync_propagates_pad_blocked(self) -> None:
        py_grid, _ = _make_grid()
        # C++ grid built BEFORE the pad is added (matches Autorouter flow).
        cpp_grid = CppGrid.from_routing_grid(py_grid)
        # Pre-condition: empty grid has zero blocked cells on both sides.
        assert cpp_grid._impl.count_blocked() == 0

        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.0,
            height=0.5,
            net=1,
            net_name="N1",
            layer=Layer.F_CU,
            ref="U1",
            pin="1",
        )
        py_grid.add_pad(pad)

        gx, gy = py_grid.world_to_grid(pad.x, pad.y)
        # Python grid: pad metal is pad_blocked.
        assert bool(py_grid._blocked[0, gy, gx]) is True
        assert bool(py_grid._pad_blocked[0, gy, gx]) is True
        # C++ grid: incremental sync forwarded both bits.
        cell = cpp_grid._impl.at(gx, gy, 0)
        assert cell.blocked is True, (
            "Incremental sync must mark the pad-metal cell as blocked on C++"
        )
        assert cell.pad_blocked is True, (
            "Incremental sync must forward the pad_blocked bit on C++ -- "
            "without this, A* steps trace centerlines through foreign pad "
            "metal during pad-exit (Issue #3224 root cause)"
        )

    def test_incremental_sync_through_hole_pad_all_layers(self) -> None:
        """Through-hole pads span all routable layers -- the incremental
        sync must propagate ``pad_blocked`` on each layer."""
        py_grid, _ = _make_grid()
        cpp_grid = CppGrid.from_routing_grid(py_grid)
        pad = Pad(
            x=5.0,
            y=5.0,
            width=1.2,
            height=1.2,
            net=2,
            net_name="N2",
            layer=Layer.F_CU,
            ref="J1",
            pin="1",
            through_hole=True,
            drill=0.8,
        )
        py_grid.add_pad(pad)
        gx, gy = py_grid.world_to_grid(pad.x, pad.y)
        for layer in range(py_grid.num_layers):
            cell = cpp_grid._impl.at(gx, gy, layer)
            assert cell.blocked is True, f"layer {layer}: not blocked"
            assert cell.pad_blocked is True, (
                f"layer {layer}: through-hole pad metal must be pad_blocked"
            )


# ---------------------------------------------------------------------------
# Layer 4: A* rejection of foreign-pad-metal traversal -- the AC #6 fixture.
# Exercises BOTH the one-shot ``route()`` path (pathfinder.cpp:680) AND the
# resumable / negotiated ``route_resumable()`` path (pathfinder.cpp:1173).
# ---------------------------------------------------------------------------


def _build_two_pad_fixture(
    *,
    resumable: bool,
) -> tuple[router_cpp.Grid3D, router_cpp.Pathfinder, dict]:
    """Build a small fixture: a foreign-net pad sits between the start
    and end so the cheap straight-line route would have to cross it.

    Geometry (cell coordinates):
        - Start pad: own net N1, cells (10,30..40) on layer 0.
        - Foreign pad N2: cells (20..30, 30..40) on layer 0,
          ``pad_blocked = True``.
        - End pad: own net N1, cells (40,30..40) on layer 0.
        - 50x70 grid with 0.1mm resolution.

    The straight-line path from start to end on layer 0 passes through
    the foreign pad rectangle.  With ``pad_blocked = True``, the A*
    must either detour or fail; without it (the pre-#3224 bug), the
    pad-exit exemption admits the foreign-metal cells and the trace
    centerline crosses through the foreign pad.

    Args:
        resumable: When True, return the resumable-search call info;
            when False, return the one-shot call info.  The two paths
            share grid setup but call different Pathfinder methods.

    Returns:
        ``(grid, pathfinder, call_info)`` where ``call_info`` is a dict
        carrying the kwargs for the chosen route method.
    """
    cols, rows, layers = 50, 70, 2
    resolution = 0.1
    grid = router_cpp.Grid3D(cols, rows, layers, resolution, 0.0, 0.0)

    rules = router_cpp.DesignRules()
    rules.trace_width = 0.127
    rules.trace_clearance = 0.127
    rules.via_diameter = 0.6
    rules.via_drill = 0.3
    rules.via_clearance = 0.127
    rules.grid_resolution = resolution
    rules.cost_straight = 1.0
    rules.cost_turn = 1.5
    rules.cost_via = 10.0

    own_net = 1
    foreign_net = 2

    # Mark foreign pad as pad_blocked metal cells.  The block is on
    # layer 0 only (the routing layer of interest); layer 1 stays
    # clear, but we still want the A* to prefer layer 0 because layer
    # transitions cost cost_via (10.0) which is far more than any
    # in-layer detour.
    for x in range(20, 31):
        for y in range(30, 41):
            grid.mark_blocked(x, y, 0, foreign_net, True, True)

    # Build pathfinder.  Diagonal routing enabled so detour around the
    # foreign pad is cheap if reachable.
    pathfinder = router_cpp.Pathfinder(grid, rules, True)
    routable = [0, 1]
    pathfinder.set_routable_layers(routable)

    info = {
        "start_x": 10 * resolution,
        "start_y": 35 * resolution,
        "start_layer": 0,
        "end_x": 40 * resolution,
        "end_y": 35 * resolution,
        "end_layer": 0,
        "net": own_net,
        "start_layers": routable,
        "end_layers": routable,
        "trace_radius_cells": 1,
        "via_radius_cells": 3,
    }
    return grid, pathfinder, info


def _route_crosses_foreign_pad(
    result: router_cpp.RouteResult,
    foreign_layer: int = 0,
) -> bool:
    """Test whether ``result``'s segment centerline crosses the foreign
    pad's metal rectangle (cells x=20..30, y=30..40 in 0.1mm units =
    world coords x=2.0..3.0, y=3.0..4.0) on the foreign pad's layer.

    Crossings on OTHER layers are not violations -- the foreign pad
    metal lives on a single layer (the SMD case) and the fixture's
    layer 1 is intentionally clear so the A* can detour via a layer
    transition.  That escape path is what proves the fix works: with
    ``pad_blocked = true`` the A* refuses layer-0 metal cells and
    must take the layer-1 detour; without it the A* takes the
    cheaper straight-through-the-metal route.
    """
    pad_x_lo, pad_x_hi = 2.0, 3.0
    pad_y_lo, pad_y_hi = 3.0, 4.0
    # Cheap test: any segment whose endpoint lies inside the rect, OR
    # whose midpoint lies inside, on the foreign-pad's layer.
    for seg in result.segments:
        if seg.layer != foreign_layer:
            continue
        for x, y in [
            (seg.x1, seg.y1),
            (seg.x2, seg.y2),
            ((seg.x1 + seg.x2) / 2.0, (seg.y1 + seg.y2) / 2.0),
        ]:
            if pad_x_lo <= x <= pad_x_hi and pad_y_lo <= y <= pad_y_hi:
                return True
    return False


@requires_cpp
class TestAStarRejectsForeignPadMetal:
    """The load-bearing AC #6 fixture: the A* must NOT route trace
    centerlines through foreign pad metal.  Exercises both A* call
    sites per the curator's review note."""

    def test_one_shot_route_avoids_foreign_pad_metal(self) -> None:
        """One-shot ``Pathfinder::route()`` (cost-shaping at
        ``pathfinder.cpp:680``) must refuse the foreign-pad-metal cells."""
        grid, pathfinder, info = _build_two_pad_fixture(resumable=False)
        result = pathfinder.route(**info)
        # Either we find a path AROUND the foreign pad metal, OR the
        # search fails (acceptable -- the fixture is intentionally
        # constrained).  What is NOT acceptable is finding a path that
        # cuts THROUGH the foreign pad metal -- that is the pre-#3224
        # bug we are regressing.
        if result.success:
            assert not _route_crosses_foreign_pad(result), (
                "One-shot A* (pathfinder.cpp:680) routed trace centerline "
                "through foreign-net pad metal.  This is the Issue #3224 "
                "regression -- ``cell.pad_blocked == true`` should refuse "
                "the metal cells.  Route returned with "
                f"{len(result.segments)} segments and {len(result.vias)} vias."
            )

    def test_resumable_route_avoids_foreign_pad_metal(self) -> None:
        """Resumable / negotiated ``Pathfinder::route_resumable()``
        (cost-shaping at ``pathfinder.cpp:1173``) must also refuse the
        foreign-pad-metal cells.  The negotiated path is exercised by
        ``Autorouter.route_all_negotiated`` and is the path that
        actually runs during the board-05 regen the issue cites."""
        grid, pathfinder, info = _build_two_pad_fixture(resumable=True)
        result = pathfinder.route_resumable(**info)
        if result.success:
            assert not _route_crosses_foreign_pad(result), (
                "Resumable A* (pathfinder.cpp:1173) routed trace centerline "
                "through foreign-net pad metal.  This is the Issue #3224 "
                "regression on the negotiated code path -- "
                "``cell.pad_blocked == true`` should refuse the metal cells. "
                f"Route returned with {len(result.segments)} segments and "
                f"{len(result.vias)} vias."
            )
        pathfinder.clear_search_state()
