"""Grid re-marking after mutating post-route passes (Issue #3507).

The post-route optimizer/nudge family mutates route geometry without
re-marking the routing grid:

* ``TraceOptimizer.optimize_route`` builds NEW Route objects; call sites
  historically replaced ``router.routes`` wholesale and left the grid
  reflecting the pre-optimization copper.
* ``drc_verify_and_nudge`` mutates Segment/Via objects IN PLACE.

Either way, downstream grid consumers (the passes' own collision
checking, targeted repair re-routes such as board 06's transactional
solo re-route, future nets in multi-pass flows) operated on a stale
occupancy picture.  These tests cover the resync machinery added by
issue #3507:

* ``RoutingGrid.resync_route_occupancy`` -- the explicit resync API
  (replacement pairs, in-place snapshots, removals/insertions).
* ``optimize_routes_grid_synced`` -- the grid-transactional optimizer
  loop used by all ``kct route`` call sites and the board recipes.
* ``drc_verify_and_nudge`` -- now snapshots geometry at entry and
  resyncs the grid on every exit path (the synthetic "mutating pass
  followed by a grid-consistency assertion" from the issue's AC).
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import (
    OptimizationConfig,
    TraceOptimizer,
    optimize_routes_grid_synced,
)
from kicad_tools.router.primitives import Route, Segment


def _make_router() -> Autorouter:
    """Empty 40x40mm router; routes are constructed and marked manually."""
    return Autorouter(width=40.0, height=40.0)


def _make_route(
    net: int,
    name: str,
    points: list[tuple[float, float]],
    width: float = 0.25,
) -> Route:
    segments = [
        Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=width,
            layer=Layer.F_CU,
            net=net,
            net_name=name,
        )
        for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False)
    ]
    return Route(net=net, net_name=name, segments=segments)


def _commit(router: Autorouter, route: Route) -> None:
    """Mark + register a route the way the routing loop does."""
    router.grid.mark_route(route)
    router.routes.append(route)


def _centerline_cells(grid, seg: Segment) -> set[tuple[int, int, int]]:
    return grid._get_segment_cells(seg)


def _assert_marked(grid, route: Route) -> None:
    """Every centerline cell of every segment must be blocked."""
    for seg in route.segments:
        for gx, gy, layer_idx in _centerline_cells(grid, seg):
            cell = grid.grid[layer_idx][gy][gx]
            assert cell.blocked, (
                f"net {route.net}: cell ({gx},{gy},L{layer_idx}) of segment "
                f"({seg.x1},{seg.y1})-({seg.x2},{seg.y2}) is NOT marked"
            )


def _assert_unmarked(grid, seg: Segment) -> None:
    """Every centerline cell of a ripped segment must be free again."""
    for gx, gy, layer_idx in _centerline_cells(grid, seg):
        cell = grid.grid[layer_idx][gy][gx]
        assert not cell.blocked, (
            f"stale cell ({gx},{gy},L{layer_idx}) of segment "
            f"({seg.x1},{seg.y1})-({seg.x2},{seg.y2}) is STILL marked"
        )


def _grid_route_geometries(grid) -> list[tuple[int, tuple]]:
    return sorted((r.net, tuple((s.x1, s.y1, s.x2, s.y2) for s in r.segments)) for r in grid.routes)


def _router_route_geometries(router) -> list[tuple[int, tuple]]:
    return sorted(
        (r.net, tuple((s.x1, s.y1, s.x2, s.y2) for s in r.segments)) for r in router.routes
    )


class TestResyncRouteOccupancy:
    """Direct tests of the RoutingGrid.resync_route_occupancy API."""

    def test_replacement_pair_unmarks_old_and_marks_new(self):
        """Optimizer shape: (old_route, new_route) replacement."""
        router = _make_router()
        old = _make_route(1, "A", [(5.0, 10.0), (20.0, 10.0)])
        _commit(router, old)

        # Replacement geometry far away from the original.
        new = _make_route(1, "A", [(5.0, 30.0), (20.0, 30.0)])
        router.routes = [new]

        changed = router.grid.resync_route_occupancy([(old, new)])
        assert changed == 1

        _assert_unmarked(router.grid, old.segments[0])
        _assert_marked(router.grid, new)
        # Bookkeeping swapped: grid.routes mirrors router.routes.
        assert _grid_route_geometries(router.grid) == _router_route_geometries(router)

    def test_in_place_mutation_with_snapshot(self):
        """Nudge shape: live object mutated, snapshot carries old geometry."""
        router = _make_router()
        live = _make_route(2, "B", [(5.0, 10.0), (20.0, 10.0)])
        _commit(router, live)

        snapshot = live.copy_geometry()
        # Mutate IN PLACE (what drc_verify_and_nudge does).
        live.segments[0].y1 = 25.0
        live.segments[0].y2 = 25.0

        changed = router.grid.resync_route_occupancy([(snapshot, live)])
        assert changed == 1

        _assert_unmarked(router.grid, snapshot.segments[0])
        _assert_marked(router.grid, live)
        # The live object stays the single grid.routes entry (no twin).
        assert len(router.grid.routes) == 1
        assert router.grid.routes[0] is live

    def test_noop_when_geometry_unchanged(self):
        router = _make_router()
        live = _make_route(3, "C", [(5.0, 10.0), (20.0, 10.0)])
        _commit(router, live)
        snapshot = live.copy_geometry()

        changed = router.grid.resync_route_occupancy([(snapshot, live)])
        assert changed == 0
        _assert_marked(router.grid, live)

    def test_removal_and_insertion_pairs(self):
        """Connectivity-invariant revert shape: (route, None) / (None, route)."""
        router = _make_router()
        regressed = _make_route(4, "D", [(5.0, 10.0), (20.0, 10.0)])
        _commit(router, regressed)

        restored = _make_route(4, "D", [(5.0, 30.0), (20.0, 30.0)])
        router.routes = [restored]

        changed = router.grid.resync_route_occupancy([(regressed, None), (None, restored)])
        assert changed == 2
        _assert_unmarked(router.grid, regressed.segments[0])
        _assert_marked(router.grid, restored)
        assert _grid_route_geometries(router.grid) == _router_route_geometries(router)

    def test_same_net_sibling_cells_survive_resync(self):
        """Step 1's net-guarded unmark may clear cells under an unchanged
        sibling route of the same net; step 4 must re-mark them."""
        router = _make_router()
        # Two routes of the SAME net crossing at (10, 10).
        sibling = _make_route(5, "E", [(10.0, 5.0), (10.0, 15.0)])
        mutated = _make_route(5, "E", [(5.0, 10.0), (15.0, 10.0)])
        _commit(router, sibling)
        _commit(router, mutated)

        snapshot = mutated.copy_geometry()
        mutated.segments[0].y1 = 12.0
        mutated.segments[0].y2 = 12.0

        router.grid.resync_route_occupancy([(snapshot, mutated)])

        # The unchanged sibling's copper (including the old crossing
        # point) must still be marked.
        _assert_marked(router.grid, sibling)
        _assert_marked(router.grid, mutated)

    def test_foreign_net_cells_untouched(self):
        router = _make_router()
        foreign = _make_route(6, "F", [(5.0, 20.0), (20.0, 20.0)])
        mutated = _make_route(7, "G", [(5.0, 10.0), (20.0, 10.0)])
        _commit(router, foreign)
        _commit(router, mutated)

        snapshot = mutated.copy_geometry()
        mutated.segments[0].y1 = 30.0
        mutated.segments[0].y2 = 30.0

        router.grid.resync_route_occupancy([(snapshot, mutated)])
        _assert_marked(router.grid, foreign)
        _assert_marked(router.grid, mutated)
        _assert_unmarked(router.grid, snapshot.segments[0])

    def test_segment_rtree_rebuilt_from_current_geometry(self):
        router = _make_router()
        grid = router.grid
        if not getattr(grid, "_rtree_available", False):
            return  # rtree optional dependency not installed
        live = _make_route(8, "H", [(5.0, 10.0), (20.0, 10.0), (20.0, 20.0)])
        _commit(router, live)
        assert grid._seg_rtree_count == 2

        snapshot = live.copy_geometry()
        # In-place mutation invalidates the stored envelopes...
        live.segments[0].y1 = 25.0
        live.segments[0].y2 = 25.0
        # ...and the resync rebuilds the index wholesale.
        grid.resync_route_occupancy([(snapshot, live)])
        assert grid._seg_rtree_count == 2
        indexed = {id(s) for items in grid._seg_rtree_items.values() for s in items.values()}
        assert indexed == {id(s) for s in live.segments}


class TestOptimizeRoutesGridSynced:
    """Grid-transactional optimizer loop (the kct-route call-site shape)."""

    def test_grid_consistent_after_optimize(self):
        router = _make_router()
        # Two collinear segments -- merge_collinear collapses them, so the
        # optimized Route has different segment objects/geometry lists.
        route = _make_route(1, "A", [(5.0, 10.0), (12.0, 10.0), (20.0, 10.0)])
        _commit(router, route)

        optimizer = TraceOptimizer(config=OptimizationConfig(merge_collinear=True))
        optimize_routes_grid_synced(router, optimizer)

        assert len(router.routes) == 1
        # The pass mutated the route (3 points -> 1 merged segment).
        assert len(router.routes[0].segments) == 1
        # Grid bookkeeping mirrors router.routes and all copper is marked.
        assert _grid_route_geometries(router.grid) == _router_route_geometries(router)
        _assert_marked(router.grid, router.routes[0])

    def test_unchanged_route_keeps_identity(self):
        router = _make_router()
        # Single segment: nothing for the optimizer to change.
        route = _make_route(2, "B", [(5.0, 10.0), (20.0, 10.0)])
        _commit(router, route)

        optimizer = TraceOptimizer(config=OptimizationConfig(merge_collinear=True))
        optimize_routes_grid_synced(router, optimizer)

        # Identity preserved so grid.routes and router.routes stay the
        # same object (no stale twin for later resyncs to re-mark).
        assert router.routes[0] is route
        assert router.grid.routes[0] is route


class TestNudgePassGridConsistency:
    """Synthetic AC test: a mutating pass followed by a grid-consistency
    assertion (drc_verify_and_nudge resyncs on every exit path)."""

    def test_in_place_mutating_pass_resyncs_grid(self):
        from kicad_tools.router.drc_nudge import DRCNudgeResult, drc_verify_and_nudge

        router = _make_router()
        live = _make_route(1, "A", [(5.0, 10.0), (20.0, 10.0)])
        _commit(router, live)
        original = live.copy_geometry()

        def fake_impl(router, **_kwargs):
            # Simulate a nudge: move the segment IN PLACE by 5mm.
            seg = router.routes[0].segments[0]
            seg.y1 += 5.0
            seg.y2 += 5.0
            result = DRCNudgeResult()
            result.segments_nudged = 1
            return result

        with patch(
            "kicad_tools.router.drc_nudge._drc_verify_and_nudge_impl",
            side_effect=fake_impl,
        ):
            result = drc_verify_and_nudge(router)

        assert result.segments_nudged == 1
        # Grid-consistency assertion: the pre-mutation copper is gone
        # and the post-mutation copper is marked.
        _assert_unmarked(router.grid, original.segments[0])
        _assert_marked(router.grid, router.routes[0])

    def test_resync_runs_even_when_pass_raises(self):
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        router = _make_router()
        live = _make_route(1, "A", [(5.0, 10.0), (20.0, 10.0)])
        _commit(router, live)
        original = live.copy_geometry()

        def exploding_impl(router, **_kwargs):
            seg = router.routes[0].segments[0]
            seg.y1 += 5.0
            seg.y2 += 5.0
            raise RuntimeError("mid-pass failure")

        with patch(
            "kicad_tools.router.drc_nudge._drc_verify_and_nudge_impl",
            side_effect=exploding_impl,
        ):
            with contextlib.suppress(RuntimeError):
                drc_verify_and_nudge(router)

        # Even on the exception path the grid reflects the mutated copper.
        _assert_unmarked(router.grid, original.segments[0])
        _assert_marked(router.grid, router.routes[0])

    def test_real_nudge_pass_no_violations_is_noop(self):
        """End-to-end: the real pass on a clean board leaves the grid
        consistent (and does not disturb it)."""
        from kicad_tools.router.drc_nudge import drc_verify_and_nudge

        router = _make_router()
        a = _make_route(1, "A", [(5.0, 10.0), (20.0, 10.0)])
        b = _make_route(2, "B", [(5.0, 30.0), (20.0, 30.0)])
        _commit(router, a)
        _commit(router, b)

        result = drc_verify_and_nudge(router)
        assert result.initial_violations == 0
        _assert_marked(router.grid, a)
        _assert_marked(router.grid, b)
        assert len(router.grid.routes) == 2
