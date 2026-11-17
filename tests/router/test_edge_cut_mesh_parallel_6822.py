"""Interior Edge.Cuts loops on the mesh engine and the parallel grid rebuild (#6822).

#6043 (PR #6821) stopped the *grid* router laying copper across an interior
``Edge.Cuts`` window.  Two paths still ignored such windows at any edge
clearance, positive included:

1. ``--route-engine mesh`` built its ``MeshPathfinder`` from the outline's
   bounding box only, so a board window was never a hole in the navmesh and
   the net was routed straight across it.
2. ``ParallelRouter.route_parallel`` rebuilds the grid with
   ``thread_safe=True`` and replayed pads and rule areas but not the edge
   keepout, so parallel routing could cross the board edge or a cutout.

The boards are the synthetic 40x30 mm fixtures from the #6043 suite: one
net whose straight line runs through the window.  Assertions are exact
geometry, independent of the router's own helpers.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.edge_keepout import edge_keepout_radius
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.mesh.obstacles import ObstacleModel
from kicad_tools.router.primitives import Route
from tests.router.test_edge_cut_floor_6043 import (
    ON_CUT,
    _assert_no_copper_on_cuts,
    _board,
    _pt_seg,
    _routed,
    _rules,
    _seg_seg,
)

pytestmark = pytest.mark.skipif(
    not is_cpp_available(), reason="mesh triangulation needs the C++ router backend"
)

CLEARANCES = [
    pytest.param(None, id="ec-none"),
    pytest.param(0.0, id="ec-0"),
    pytest.param(0.3, id="ec-0.3"),
]

# The window loops of the #6043 fixtures (the outer outline is 0,0 -> 40,30).
WINDOW_EDGES = {
    "rect": lambda c, d: min(c[0], d[0]) >= 15.0 - 1e-6 and max(c[0], d[0]) <= 25.0 + 1e-6,
    "circle": lambda c, d: math.hypot(c[0] - 20.0, c[1] - 15.0) <= 5.0 + 1e-3,
}


def _load(tmp_path: Path, text: str, *, ec, strategy: str = "grid"):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(text)
    router, _net_map = load_pcb_for_routing(
        str(pcb),
        rules=_rules(0.1),
        use_pcb_rules=False,
        validate_drc=False,
        edge_clearance=ec,
        strategy=strategy,
    )
    return router


def _window_edges(router, window: str):
    keep = WINDOW_EDGES[window]
    edges = [(c, d) for c, d in router._edge_segments if keep(c, d)]
    assert edges, "fixture must expose the window's edges"
    return edges


def _assert_window_clearance(routes: list[Route], edges, required_centreline) -> None:
    """Every trace centreline keeps ``required_centreline(seg)`` from the window."""
    for route in routes:
        for seg in route.segments:
            need = required_centreline(seg.width / 2)
            for c, d in edges:
                dist = _seg_seg((seg.x1, seg.y1), (seg.x2, seg.y2), c, d)
                assert dist >= need - 1e-4, (
                    f"trace ({seg.x1:.3f},{seg.y1:.3f})->({seg.x2:.3f},{seg.y2:.3f}) "
                    f"centreline {dist:.4f} mm from the window, needs {need:.4f}"
                )
        for via in route.vias:
            need = required_centreline(via.diameter / 2)
            for (cx, cy), (dx, dy) in edges:
                dist = _pt_seg(via.x, via.y, cx, cy, dx, dy)
                assert dist >= need - 1e-4


# --- mesh engine ------------------------------------------------------------


@pytest.mark.parametrize("window", ["rect", "circle"])
@pytest.mark.parametrize("ec", CLEARANCES)
def test_mesh_routes_around_an_interior_window(tmp_path, ec, window):
    router = _load(tmp_path, _board(window), ec=ec, strategy="mesh")
    router.route_all(suppress_no_timeout_warning=True)
    routes = list(router.routes)

    assert _routed(routes), "the single net must still route around the window"
    _assert_no_copper_on_cuts(routes, router._edge_segments)

    edges = _window_edges(router, window)
    if ec:
        # Copper keeps the full edge clearance from the cut.
        _assert_window_clearance(routes, edges, lambda half: half + ec)
    else:
        # No clearance resolved: the #6043 floor (a centreline radius) holds.
        floor = edge_keepout_radius(router)
        assert floor is not None and floor > 0
        _assert_window_clearance(routes, edges, lambda half: max(floor, half))


@pytest.mark.parametrize(
    ("clearance", "floor"),
    [pytest.param(0.3, 0.0, id="ec-0.3"), pytest.param(0.0, 0.37, id="floor-0.37")],
)
def test_mesh_via_sites_stay_off_the_window(clearance, floor):
    """The via-injection gate refuses a via inside or too close to a cutout."""
    from kicad_tools.router.mesh.pathfinder import MeshPathfinder

    outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
    window = [(15.0, 10.0), (25.0, 10.0), (25.0, 20.0), (15.0, 20.0)]
    pf = MeshPathfinder(outline, [], _rules(0.1), cutouts=[window], cutout_floor=floor)
    pf.edge_clearance = clearance
    radius = pf.rules.via_diameter / 2  # 0.3 mm
    # Centre-to-cut a via needs: radius + clearance, or the floor (centreline).
    need = max(radius + clearance, floor)

    def allowed(site):
        return pf._via_allowed_at(site, 1, radius, (0, 1), {})

    assert not allowed((20.0, 15.0)), "via inside the window"
    assert not allowed((15.0 - need + 0.05, 15.0)), "via too close to the window"
    assert allowed((15.0 - need - 0.05, 15.0))
    assert allowed((5.0, 5.0))


def test_obstacle_model_rejects_a_leg_across_a_cutout():
    outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
    window = [(15.0, 10.0), (25.0, 10.0), (25.0, 20.0), (15.0, 20.0)]
    model = ObstacleModel(outline, [], half=0.1, cutouts=[window], cutout_clearance=0.3)
    assert not model.is_clear((10.0, 15.0), (30.0, 15.0))  # straight across
    assert not model.is_clear((17.0, 15.0), (23.0, 15.0))  # wholly inside
    assert not model.is_clear((10.0, 20.3), (30.0, 20.3))  # 0.2 mm copper gap
    assert model.is_clear((10.0, 20.45), (30.0, 20.45))  # 0.35 mm copper gap
    # No cutouts: byte-identical to the pre-#6822 model.
    assert ObstacleModel(outline, [], half=0.1).is_clear((10.0, 15.0), (30.0, 15.0))


def test_interior_edge_loops_finds_windows_but_not_the_outline_or_islands():
    from kicad_tools.router.mesh.pathfinder import interior_edge_loops

    def ring(x0, y0, x1, y1):
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return list(zip(pts, pts[1:] + pts[:1], strict=True))

    outer = ring(0, 0, 40, 30)
    window = ring(10, 10, 30, 20)
    island = ring(18, 13, 22, 17)
    assert interior_edge_loops(outer) == []
    loops = interior_edge_loops(outer + window + island)
    assert len(loops) == 1
    xs = sorted({x for x, _ in loops[0]})
    assert xs == [10.0, 30.0]


# --- parallel grid rebuild -------------------------------------------------


@pytest.mark.parametrize("ec", CLEARANCES)
def test_parallel_rebuild_keeps_the_edge_keepout(tmp_path, ec):
    router = _load(tmp_path, _board("rect"), ec=ec)
    assert not router.grid.thread_safe
    radius = edge_keepout_radius(router)
    assert radius is not None and radius > 0

    routes = router.route_all_parallel(max_workers=2)

    grid = router.grid
    assert grid.thread_safe, "route_parallel must have rebuilt the grid"
    gx, gy = grid.world_to_grid(*ON_CUT["rect"])
    blocked = np.asarray(grid._blocked)
    for layer in grid.get_routable_indices():
        assert blocked[layer, gy, gx], "the rebuilt grid lost the edge keepout"

    assert _routed(routes)
    _assert_no_copper_on_cuts(routes, router._edge_segments)
