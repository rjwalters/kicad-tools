"""Localized per-link lattice build + per-link deadline (Issue #4472, epic #4465).

Phase 2 of ``kct route --complete``.  A completion pass that re-routes one
walled link used to pay a WHOLE-BOARD lattice build + a whole-board A* every
time (issue #4434's ">10 minutes without terminating").  Phase 2 restricts the
octilinear lattice build + per-layer static masks to a per-link bounding box
and bounds each link's search with a wall-clock budget.

These tests pin the four load-bearing properties:

1. ``Autorouter._snap_lattice_region`` snaps a requested box OUTWARD to the
   whole-board coarse grid, clamps to the board, and never degenerates.
2. ``_ensure_lattice_pathfinder`` builds over the localized box when one is
   stamped (and over the whole board otherwise).
3. The localized build is genuinely smaller (fewer lattice nodes) yet produces
   the IDENTICAL route to the un-localized build for an in-box connection --
   localization is a perf optimization, not a correctness change.
4. ``LatticePathfinder.route_netset(deadline=...)`` aborts on the deadline and
   returns the best routes so far (not falsely "converged").
"""

from __future__ import annotations

import time

from kicad_tools.router.core import Autorouter
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

_COARSE = 3.2  # LatticePathfinder default coarse cell (kept in sync via the signature)


def _pad(x: float, y: float, net: int, *, ref: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=f"N{net}",
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _route_signature(route) -> list[tuple]:
    """A comparable, order-stable geometry signature for a route."""
    segs = sorted(
        (
            round(s.x1, 6),
            round(s.y1, 6),
            round(s.x2, 6),
            round(s.y2, 6),
            round(s.width, 6),
            s.layer,
        )
        for s in route.segments
    )
    vias = sorted((round(v.x, 6), round(v.y, 6)) for v in route.vias)
    return [("seg", *t) for t in segs] + [("via", *t) for t in vias]


# ---------------------------------------------------------------------------
# 1. _snap_lattice_region: grid alignment, clamping, non-degeneracy.
# ---------------------------------------------------------------------------
class TestSnapLatticeRegion:
    def test_snaps_outward_to_coarse_grid(self):
        board = (0.0, 0.0, 100.0, 60.0)
        # Pad bbox (40,30)-(60,30) grown by 3 mm -> (37,27)-(63,33).
        box = (37.0, 27.0, 63.0, 33.0)
        lo_x, lo_y, hi_x, hi_y = Autorouter._snap_lattice_region(box, board, _COARSE)
        # Every edge lands on a multiple of coarse from the board origin (0,0).
        for v in (lo_x, lo_y, hi_x, hi_y):
            assert abs((v / _COARSE) - round(v / _COARSE)) < 1e-9
        # Outward snap: the snapped box CONTAINS the requested box.
        assert lo_x <= 37.0 and lo_y <= 27.0 and hi_x >= 63.0 and hi_y >= 33.0

    def test_clamped_to_board(self):
        board = (0.0, 0.0, 20.0, 20.0)
        # A box overflowing the board must clamp to the board extent.
        box = (-5.0, -5.0, 40.0, 40.0)
        lo_x, lo_y, hi_x, hi_y = Autorouter._snap_lattice_region(box, board, _COARSE)
        assert lo_x >= 0.0 and lo_y >= 0.0
        assert hi_x <= 20.0 and hi_y <= 20.0

    def test_never_degenerate(self):
        board = (0.0, 0.0, 20.0, 20.0)
        # A zero-area box still yields at least one coarse cell of extent.
        box = (10.0, 10.0, 10.0, 10.0)
        lo_x, lo_y, hi_x, hi_y = Autorouter._snap_lattice_region(box, board, _COARSE)
        assert hi_x > lo_x and hi_y > lo_y


# ---------------------------------------------------------------------------
# 2. _ensure_lattice_pathfinder honors the localized box.
# ---------------------------------------------------------------------------
def _lattice_router(region_world=None) -> Autorouter:
    r = Autorouter(100.0, 60.0, 0.0, 0.0, layer_stack=LayerStack.two_layer(), strategy="lattice")
    r._board_bbox = (0.0, 0.0, 100.0, 60.0)
    a1, a2 = _pad(40.0, 30.0, 1, ref="A1"), _pad(60.0, 30.0, 1, ref="A2")
    r.all_pads = [a1, a2]
    r.pads = {("A1", "1"): a1, ("A2", "1"): a2}
    r.nets = {1: [("A1", "1"), ("A2", "1")]}
    r.net_names = {1: "N1"}
    r._lattice_region_world = region_world
    return r


class TestEnsureLatticePathfinderLocalization:
    def test_whole_board_outline_by_default(self):
        pf = _lattice_router()._ensure_lattice_pathfinder()
        xs = [p[0] for p in pf.outline]
        ys = [p[1] for p in pf.outline]
        assert (min(xs), min(ys), max(xs), max(ys)) == (0.0, 0.0, 100.0, 60.0)

    def test_localized_outline_when_box_set(self):
        # Pad bbox + 3 mm margin, un-snapped, handed to the router.
        pf = _lattice_router((37.0, 27.0, 63.0, 33.0))._ensure_lattice_pathfinder()
        xs = [p[0] for p in pf.outline]
        ys = [p[1] for p in pf.outline]
        bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
        # Snapped to the coarse grid, strictly inside the whole board.
        assert bx0 > 0.0 and by0 > 0.0 and bx1 < 100.0 and by1 < 60.0
        # Contains the pads with margin.
        assert bx0 <= 40.0 and bx1 >= 60.0 and by0 <= 30.0 and by1 >= 30.0


# ---------------------------------------------------------------------------
# 3. Localized build == smaller lattice, IDENTICAL in-box route.
# ---------------------------------------------------------------------------
class TestLocalizationPreservesRoute:
    def _pads_and_conn(self):
        a1, a2 = _pad(40.0, 30.0, 1, ref="A1"), _pad(60.0, 30.0, 1, ref="A2")
        return [a1, a2], ((1, 0), a1, a2, None)

    def test_localized_lattice_has_fewer_nodes(self):
        pads, _conn = self._pads_and_conn()
        rules = DesignRules()
        full = LatticePathfinder(
            [(0.0, 0.0), (100.0, 0.0), (100.0, 60.0), (0.0, 60.0)],
            pads,
            rules,
            LayerStack.two_layer(),
        )
        box = Autorouter._snap_lattice_region(
            (37.0, 27.0, 63.0, 33.0), (0.0, 0.0, 100.0, 60.0), _COARSE
        )
        local = LatticePathfinder(
            [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])],
            pads,
            rules,
            LayerStack.two_layer(),
        )
        assert len(local.build().nodes) < len(full.build().nodes)

    def test_localized_route_identical_to_whole_board(self):
        pads, conn = self._pads_and_conn()
        rules = DesignRules()
        full = LatticePathfinder(
            [(0.0, 0.0), (100.0, 0.0), (100.0, 60.0), (0.0, 60.0)],
            pads,
            rules,
            LayerStack.two_layer(),
        )
        full_routes, full_stats = full.route_netset([conn], max_iterations=4)
        assert (1, 0) in full_routes, f"whole-board declined: {full.failure_reasons}"

        box = Autorouter._snap_lattice_region(
            (37.0, 27.0, 63.0, 33.0), (0.0, 0.0, 100.0, 60.0), _COARSE
        )
        local = LatticePathfinder(
            [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])],
            pads,
            rules,
            LayerStack.two_layer(),
        )
        local_routes, _ = local.route_netset([conn], max_iterations=4)
        assert (1, 0) in local_routes, f"localized declined: {local.failure_reasons}"

        # The snapped localized build shares the whole-board lattice's node
        # grid, so an in-box connection routes to the SAME geometry.
        assert _route_signature(local_routes[(1, 0)]) == _route_signature(full_routes[(1, 0)])
        assert full_stats.lattice_builds == 1


# ---------------------------------------------------------------------------
# 4. route_netset(deadline=...) aborts and returns best-so-far.
# ---------------------------------------------------------------------------
class TestRouteNetsetDeadline:
    def _pf(self):
        a1, a2 = _pad(40.0, 30.0, 1, ref="A1"), _pad(60.0, 30.0, 1, ref="A2")
        return LatticePathfinder(
            [(0.0, 0.0), (100.0, 0.0), (100.0, 60.0), (0.0, 60.0)],
            [a1, a2],
            DesignRules(),
            LayerStack.two_layer(),
        ), ((1, 0), a1, a2, None)

    def test_past_deadline_aborts_immediately(self):
        pf, conn = self._pf()
        started = time.monotonic()
        routes, stats = pf.route_netset([conn], max_iterations=8, deadline=time.monotonic() - 1.0)
        elapsed = time.monotonic() - started
        # No pass ran to completion; nothing routed; NOT falsely converged.
        assert routes == {}
        assert stats.routed == 0
        assert stats.converged is False
        assert elapsed < 1.0

    def test_generous_deadline_matches_unbudgeted(self):
        pf, conn = self._pf()
        budgeted, s_budget = pf.route_netset(
            [conn], max_iterations=4, deadline=time.monotonic() + 3600.0
        )
        pf2, conn2 = self._pf()
        unbudgeted, s_plain = pf2.route_netset([conn2], max_iterations=4)
        assert (1, 0) in budgeted and (1, 0) in unbudgeted
        assert s_budget.converged == s_plain.converged
        assert _route_signature(budgeted[(1, 0)]) == _route_signature(unbudgeted[(1, 0)])
