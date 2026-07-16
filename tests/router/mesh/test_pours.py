"""Pours-as-obstacles for mesh-router P2 (#4269).

P1 modeled pad keep-outs only; P2 adds filled-copper pour polygons BOTH as
poly2tri mesh holes (so corridors route around them) and as obstacle-model
polygons (so the 45-fit declines any leg entering a pour).  These tests prove
the new segment-vs-polygon clearance predicate and that a net whose pour-blind
P1 corridor cut straight through a pour now routes cleanly around it.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.mesh.geometry import segment_intersects_polygon
from kicad_tools.router.mesh.obstacles import ObstacleModel
from kicad_tools.router.mesh.pathfinder import MeshPathfinder
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

pytest.importorskip("kicad_tools.router.router_cpp")

# A pour occupying the middle band of a 40x40 board, leaving open space above
# and below.  A horizontal net at y=20 crosses it head-on.
_POUR = [(15.0, 17.0), (25.0, 17.0), (25.0, 23.0), (15.0, 23.0)]
_OUTLINE = [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)]


def _pad(x: float, y: float, ref: str) -> Pad:
    return Pad(
        x=x, y=y, width=1.0, height=1.0, net=1, net_name="SIG", layer=Layer.F_CU, ref=ref, pin="1"
    )


def _route_crosses_pour(route) -> bool:
    return any(
        segment_intersects_polygon((s.x1, s.y1), (s.x2, s.y2), _POUR) for s in route.segments
    )


def test_segment_vs_polygon_predicate() -> None:
    # A leg through the pour interior collides; one skirting above it is clear.
    assert segment_intersects_polygon((10.0, 20.0), (30.0, 20.0), _POUR)  # straight through
    assert not segment_intersects_polygon((10.0, 30.0), (30.0, 30.0), _POUR)  # above the pour
    # An endpoint inside the pour also counts as a hit.
    assert segment_intersects_polygon((20.0, 20.0), (35.0, 35.0), _POUR)


def test_obstacle_model_rejects_leg_entering_pour() -> None:
    model = ObstacleModel(_OUTLINE, [], pours=[_POUR])
    assert not model.is_clear((10.0, 20.0), (30.0, 20.0))  # crosses pour -> not clear
    assert model.is_clear((10.0, 30.0), (30.0, 30.0))  # above pour -> clear


def test_obstacle_model_backward_compatible_without_pours() -> None:
    # P1 call site: ObstacleModel(outline, keepouts) with no pours is unchanged.
    model = ObstacleModel(_OUTLINE, [])
    assert model.is_clear((10.0, 20.0), (30.0, 20.0))  # no pour -> the leg is clear


def test_pour_blind_p1_path_crosses_but_p2_routes_around() -> None:
    rules = DesignRules()
    a, b = _pad(5.0, 20.0, "R1"), _pad(35.0, 20.0, "R2")

    # P1 behaviour (no pour modeling): the straight corridor cuts through the
    # pour -- a short to the pour net.
    p1 = MeshPathfinder(_OUTLINE, [a, b], rules)
    p1_route = p1.route(a, b)
    assert p1_route is not None and p1_route.segments
    assert _route_crosses_pour(p1_route), "precondition: the pour-blind path shorts through"

    # P2 behaviour (pour modeled): the net still routes, but the emitted copper
    # detours around the pour and no segment enters it.
    p2 = MeshPathfinder(_OUTLINE, [a, b], rules, pours=[_POUR])
    p2_route = p2.route(a, b)
    assert p2_route is not None and p2_route.segments, "the pour-crossing net must still route"
    assert not _route_crosses_pour(p2_route), "P2 copper must clear the pour"
    # And it actually connects the two pads.
    assert p2_route.segments[0].x1 == a.x and p2_route.segments[0].y1 == a.y
    assert p2_route.segments[-1].x2 == b.x and p2_route.segments[-1].y2 == b.y


def test_pour_is_a_mesh_hole() -> None:
    # The pour enters the static triangulation as a hole: the navmesh has no
    # triangle whose centroid falls inside the pour interior.
    a, b = _pad(5.0, 20.0, "R1"), _pad(35.0, 20.0, "R2")
    pf = MeshPathfinder(_OUTLINE, [a, b], DesignRules(), pours=[_POUR])
    nm = pf.build()
    assert nm.triangles
    for cx, cy in nm._centroids:
        inside = 15.0 < cx < 25.0 and 17.0 < cy < 23.0
        assert not inside, "no triangle centroid may fall inside the pour hole"
