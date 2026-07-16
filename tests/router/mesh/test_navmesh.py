"""Navmesh A* + funnel integration tests over a real poly2tri mesh (#4268).

Exercises the whole substrate below the octilinear fit: poly2tri CDT with a
hole -> triangle-dual portal-midpoint A* -> oriented portals -> funnel.  The
geodesic must route around the interior hole without crossing its interior.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.mesh.funnel import string_pull
from kicad_tools.router.mesh.geometry import dist
from kicad_tools.router.mesh.navmesh import NavMesh

router_cpp = pytest.importorskip("kicad_tools.router.router_cpp")


def _point_strictly_in_rect(p, xmin, ymin, xmax, ymax, eps=1e-6):
    return (xmin + eps) < p[0] < (xmax - eps) and (ymin + eps) < p[1] < (ymax - eps)


def _leg_crosses_hole_interior(a, b, hole, samples=50):
    # Sample the leg; if any interior sample lands strictly inside the hole
    # the geodesic clipped the obstacle (a real violation, not a corner graze).
    xmin, ymin, xmax, ymax = hole
    for k in range(1, samples):
        t = k / samples
        p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        if _point_strictly_in_rect(p, xmin, ymin, xmax, ymax):
            return True
    return False


def test_funnel_routes_around_interior_hole() -> None:
    outer = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    hole = [(6.0, 6.0), (14.0, 6.0), (14.0, 14.0), (6.0, 14.0)]
    hole_rect = (6.0, 6.0, 14.0, 14.0)
    start, goal = (2.0, 2.0), (18.0, 18.0)

    verts, tris = router_cpp.constrained_delaunay(outer, [hole], [start, goal])
    assert verts and tris

    nm = NavMesh([tuple(v) for v in verts], [tuple(t) for t in tris])
    corridor = nm.astar(start, goal)
    assert corridor is not None

    portals = nm.corridor_to_portals(corridor, start, goal)
    path = string_pull(portals)

    assert path[0] == start
    assert path[-1] == goal
    # The straight diagonal is blocked by the hole, so the path must detour.
    length = sum(dist(path[i], path[i + 1]) for i in range(len(path) - 1))
    assert length > dist(start, goal) + 1e-6
    # And no leg may cut through the hole interior.
    for i in range(len(path) - 1):
        assert not _leg_crosses_hole_interior(path[i], path[i + 1], hole_rect)


def test_astar_returns_none_when_goal_walled_off() -> None:
    # A hole spanning the full width splits the board; the far side is
    # unreachable, so A* must report no corridor.
    outer = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    wall = [(0.0, 9.0), (20.0, 9.0), (20.0, 11.0), (0.0, 11.0)]
    start, goal = (10.0, 4.0), (10.0, 16.0)
    verts, tris = router_cpp.constrained_delaunay(outer, [wall], [start, goal])
    nm = NavMesh([tuple(v) for v in verts], [tuple(t) for t in tris])
    assert nm.astar(start, goal) is None


def test_portal_midpoint_cost_is_near_optimal_in_open_region() -> None:
    # No hole: the funnel across a wide-open board collapses to the straight
    # line (the portal-midpoint A* corridor does not force a detour).
    outer = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
    start, goal = (3.0, 3.0), (37.0, 27.0)
    verts, tris = router_cpp.constrained_delaunay(outer, [], [start, goal])
    nm = NavMesh([tuple(v) for v in verts], [tuple(t) for t in tris])
    corridor = nm.astar(start, goal)
    assert corridor is not None
    path = string_pull(nm.corridor_to_portals(corridor, start, goal))
    length = sum(dist(path[i], path[i + 1]) for i in range(len(path) - 1))
    assert math.isclose(length, dist(start, goal), rel_tol=1e-6)
