"""In-corridor lane assignment (portal narrowing + re-funnel) for P2.5 (#4274).

P2 shipped multi-net negotiation but a capacity-N portal still carried only ONE
path: the funnel is lane-blind and returns the same taut geodesic per net, so
committed copper blocks the next net and it declines.  P2.5 narrows each portal
opening by the copper already committed on it and re-funnels, so net k+1 gets a
*parallel* geodesic in the residual opening.

These tests cover, from the acceptance criteria:

* the residual-opening geometry (``segment_polygon_interval`` / ``merge_intervals``
  and ``NavMesh.corridor_to_portals(consumed=...)``);
* a hand-computed capacity-N portal carrying ``min(N, demand)`` distinct
  non-overlapping 45-legal lanes (modelled on ``test_portal_capacity.py``);
* the full-opening funnel staying byte-identical when nothing is committed
  (the P1/P2 single-net contract, and the ``--route-engine grid`` guarantee that
  the mesh package is only consulted for ``--route-engine mesh``);
* completion rising on a dense corridor where lanes ARE parallel-congested,
  with the triangulation still built once and zero net-vs-net crossings;
* the #3906 never-ship-a-short invariant: a lane that cannot clear declines
  (``None``) rather than crossing committed copper.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.mesh.funnel import string_pull
from kicad_tools.router.mesh.geometry import (
    merge_intervals,
    segment_polygon_interval,
    segments_intersect,
)
from kicad_tools.router.mesh.navmesh import NavMesh
from kicad_tools.router.mesh.obstacles import ObstacleModel
from kicad_tools.router.mesh.octilinear import octilinear_fit
from kicad_tools.router.mesh.pathfinder import (
    MeshPathfinder,
    _edge_consumed_bands,
    _segment_capsule,
)
from kicad_tools.router.primitives import Pad
from kicad_tools.router.quantize import is_45_aligned
from kicad_tools.router.rules import DesignRules

router_cpp = pytest.importorskip("kicad_tools.router.router_cpp")


def _pad(x: float, y: float, net: int, ref: str, w: float = 0.6, h: float = 0.6) -> Pad:
    return Pad(
        x=x, y=y, width=w, height=h, net=net, net_name=f"N{net}", layer=Layer.F_CU, ref=ref, pin="1"
    )


def _cross_net_intersections(routes: dict[object, object]) -> int:
    """Count intersecting DIFFERENT-net segments **on the same layer** (shorts).

    Layer-aware (issue #4276): with 2.5D via injection two nets may cross in the
    XY projection when they sit on different copper layers -- that is the whole
    point of dipping, not a short.  Only a same-layer crossing is a net-vs-net
    short, so the count keys on ``(net, layer)`` and compares within a layer.
    """
    segs: list[tuple[object, object, tuple[float, float], tuple[float, float]]] = []
    for key, route in routes.items():
        for s in route.segments:  # type: ignore[attr-defined]
            segs.append((key, s.layer, (s.x1, s.y1), (s.x2, s.y2)))
    count = 0
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if (
                segs[i][0] != segs[j][0]
                and segs[i][1] == segs[j][1]
                and segments_intersect(segs[i][2], segs[i][3], segs[j][2], segs[j][3])
            ):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Residual-opening geometry primitives.
# ---------------------------------------------------------------------------


def test_segment_polygon_interval_clips_edge_to_capsule() -> None:
    # A vertical portal edge from (0,0) to (0,10) and a committed capsule that
    # covers y in [4, 6] -> the consumed parametric interval is [0.4, 0.6].
    a, b = (0.0, 0.0), (0.0, 10.0)
    capsule = [(-1.0, 4.0), (1.0, 4.0), (1.0, 6.0), (-1.0, 6.0)]
    iv = segment_polygon_interval(a, b, capsule)
    assert iv is not None
    t0, t1 = iv
    assert math.isclose(t0, 0.4, abs_tol=1e-9)
    assert math.isclose(t1, 0.6, abs_tol=1e-9)


def test_segment_polygon_interval_none_when_disjoint() -> None:
    a, b = (0.0, 0.0), (0.0, 10.0)
    capsule = [(5.0, 4.0), (7.0, 4.0), (7.0, 6.0), (5.0, 6.0)]  # off to the side
    assert segment_polygon_interval(a, b, capsule) is None


def test_merge_intervals_unions_overlaps() -> None:
    assert merge_intervals([(0.1, 0.3), (0.25, 0.5), (0.8, 0.9)]) == [(0.1, 0.5), (0.8, 0.9)]
    assert merge_intervals([]) == []


# ---------------------------------------------------------------------------
# corridor_to_portals narrowing.
# ---------------------------------------------------------------------------


def _two_square_channel(
    channel: float,
) -> tuple[NavMesh, list[int], tuple[float, float], tuple[float, float]]:
    """Two unit squares sharing a vertical mid portal ``(2, 3)`` of length 10.

    Vertices: 0=(0,0) 1=(0,10) 2=(10,0) 3=(10,10) 4=(20,0) 5=(20,10).  The
    shared edge (2,3) is the vertical portal at x=10 spanning y in [0,10].
    """
    verts = [(0.0, 0.0), (0.0, 10.0), (10.0, 0.0), (10.0, 10.0), (20.0, 0.0), (20.0, 10.0)]
    tris = [(0, 2, 3), (0, 3, 1), (2, 4, 5), (2, 5, 3)]
    nm = NavMesh(verts, tris, channel=channel)
    start, goal = (1.0, 5.0), (19.0, 5.0)
    corridor = nm.astar(start, goal)
    assert corridor is not None
    assert (2, 3) in nm.corridor_portals(corridor)
    return nm, corridor, start, goal


def test_corridor_to_portals_without_consumed_is_unchanged() -> None:
    # The P1/P2 contract: no consumed model -> full openings, byte-identical.
    nm, corridor, start, goal = _two_square_channel(channel=0.4)
    plain = nm.corridor_to_portals(corridor, start, goal)
    also_plain = nm.corridor_to_portals(corridor, start, goal, consumed=None)
    assert plain == also_plain
    # An empty consumed map is likewise a no-op.
    assert nm.corridor_to_portals(corridor, start, goal, consumed={}) == plain


def test_corridor_to_portals_narrows_to_residual_opening() -> None:
    nm, corridor, start, goal = _two_square_channel(channel=0.4)
    # Consume the lower third of the portal (t in [0, 0.3]) -> the largest free
    # gap is [0.3, 1], so the returned portal opening starts at y=3 (s=0.3).
    consumed = {(2, 3): [(0.0, 0.3)]}
    portals = nm.corridor_to_portals(corridor, start, goal, consumed=consumed)
    # Find the (2,3) portal in the oriented list by matching x==10 endpoints.
    gate = [p for p in portals if abs(p[0][0] - 10.0) < 1e-9 and abs(p[1][0] - 10.0) < 1e-9]
    assert gate, "vertical gate portal not found"
    left, right = gate[0]
    ys = sorted((left[1], right[1]))
    # Opening no longer reaches y<3; it spans the residual [3, 10].
    assert ys[0] >= 3.0 - 1e-6
    assert ys[1] <= 10.0 + 1e-6


# ---------------------------------------------------------------------------
# Acceptance: a capacity-N portal carries N distinct non-overlapping lanes.
# ---------------------------------------------------------------------------


def test_capacity_portal_carries_multiple_parallel_lanes() -> None:
    """A hand-computed capacity-N portal carries ``min(N, demand)`` lanes.

    Successive nets share the same corridor; each consumes a band on the portal,
    and the re-funnel places the next net in the residual opening.  The lanes
    must be distinct, non-overlapping (centreline pitch >= trace + clearance),
    all inside the portal, and each 45-legal.
    """
    rules = DesignRules()
    pitch = rules.trace_width + rules.trace_clearance  # 0.4 mm: clean lane pitch
    nm, corridor, start, goal = _two_square_channel(channel=0.4)
    assert nm.capacity((2, 3)) >= 10  # a genuinely wide (capacity-N) portal

    clear = lambda _a, _b: True  # noqa: E731 - permissive obstacle model for 45-legality
    consumed: dict[tuple[int, int], list[tuple[float, float]]] = {}
    crossings: list[float] = []
    demand = 6
    for _lane in range(demand):
        portals = nm.corridor_to_portals(corridor, start, goal, consumed=consumed, pack="left")
        geodesic = string_pull(portals)
        fitted = octilinear_fit(geodesic, clear)
        assert fitted is not None and len(fitted) >= 2
        # Every emitted leg is 45-legal.
        for i in range(len(fitted) - 1):
            dx = fitted[i + 1][0] - fitted[i][0]
            dy = fitted[i + 1][1] - fitted[i][1]
            assert is_45_aligned(dx, dy)
        # Record where this lane crosses the portal (x == 10).
        cy = None
        for i in range(len(geodesic) - 1):
            (x1, y1), (x2, y2) = geodesic[i], geodesic[i + 1]
            if (x1 - 10.0) * (x2 - 10.0) <= 0 and x1 != x2:
                t = (10.0 - x1) / (x2 - x1)
                cy = y1 + t * (y2 - y1)
        assert cy is not None
        crossings.append(cy)
        # Commit this lane's copper and record the band it consumes on the portal.
        capsules = [
            c
            for c in (
                _segment_capsule(geodesic[i], geodesic[i + 1], pitch)
                for i in range(len(geodesic) - 1)
            )
            if c is not None
        ]
        bands = _edge_consumed_bands(nm, (2, 3), capsules)
        assert bands
        consumed.setdefault((2, 3), []).extend(bands)

    # min(N, demand) == demand distinct lanes were placed.
    assert len(crossings) == demand
    # All inside the portal.
    assert all(0.0 <= cy <= 10.0 for cy in crossings)
    # Monotone and non-overlapping: consecutive centrelines are at least a clean
    # lane pitch apart (their inflated copper capsules do not overlap).
    ordered = sorted(crossings)
    for lo, hi in zip(ordered, ordered[1:], strict=False):
        assert hi - lo >= pitch - 1e-6, f"lanes overlap: {ordered}"


# ---------------------------------------------------------------------------
# Completion rises on a dense corridor (parallel-congested), DRC-clean.
# ---------------------------------------------------------------------------


def _l_bend_fixture() -> tuple[list, list, list]:
    """Nets that all round the same reflex corner -> co-linear congestion.

    A square obstacle fills the lower-right, leaving an L-shaped free region.
    Five nets run from the left strip to the top strip; the funnel pulls each
    taut around the SAME inner corner, so without lane assignment they collapse
    onto one geodesic and committed copper blocks all but the first few.
    """
    outline = [(0.0, 0.0), (50.0, 0.0), (50.0, 50.0), (0.0, 50.0)]
    pads = [_pad(36.0, 6.0, 99, "OBS", w=28.0, h=28.0)]  # blocks x>=22, y<=20
    conns = []
    for k in range(5):
        left = _pad(8.0, 4.0 + 2.5 * k, k + 1, f"L{k}")
        top = _pad(42.0 - 2.5 * k, 44.0, k + 1, f"T{k}")
        pads += [left, top]
        conns.append(((k + 1, 0), left, top, None))
    return outline, pads, conns


def _greedy_straight_only(outline: list, pads: list, conns: list) -> int:
    """First-come greedy that avoids committed copper but never narrows portals.

    This is the P2 behaviour (full-opening funnel only): the fair baseline the
    lane-assigned negotiation must beat.
    """
    rules = DesignRules()
    pf = MeshPathfinder(outline, pads, rules)
    nm = pf.build()
    committed: list = []
    routed = 0
    ordered = sorted(conns, key=lambda c: math.hypot(c[1].x - c[2].x, c[1].y - c[2].y))
    for _key, s, e, _nc in ordered:
        keep = pf._keepouts(s.net, rules.trace_width / 2.0 + rules.trace_clearance)
        obst = ObstacleModel(outline, keep, pf.pours + committed)
        corridor = nm.astar(
            (s.x, s.y),
            (e.x, e.y),
            present_cost_factor=0.5,
            cost_congestion=2.0,
            congestion_threshold=0.3,
        )
        fit = (
            None
            if corridor is None
            else pf._fit_corridor(nm, corridor, (s.x, s.y), (e.x, e.y), obst, None)
        )
        if fit is not None:
            routed += 1
            committed.extend(pf._route_obstacles(pf._build_route(s, s.net, rules.trace_width, fit)))
    return routed


def test_lane_assignment_lifts_completion_on_dense_corridor() -> None:
    outline, pads, conns = _l_bend_fixture()
    baseline = _greedy_straight_only(outline, pads, conns)

    pf = MeshPathfinder(outline, pads, DesignRules())
    routes, stats = pf.route_netset(conns, max_iterations=8)

    # A real rise over the full-opening (P2) baseline...
    assert stats.routed > baseline, (
        f"lane assignment did not lift completion: {stats.routed} <= {baseline}"
    )
    # ...with the triangulation still built exactly once...
    assert stats.triangulation_calls == 1
    # ...and zero net-vs-net crossings (never ship a short).
    assert _cross_net_intersections(routes) == 0


# ---------------------------------------------------------------------------
# #3906: a blocked lane declines rather than shorts.
# ---------------------------------------------------------------------------


def test_blocked_lane_declines_never_shorts() -> None:
    """Two nets that must cross on a single layer: the second declines.

    Net A spans nearly the full board width; net B must cross it and cannot go
    around.  Lane assignment cannot manufacture a legal parallel lane here, so
    the octilinear fit against the true obstacle model declines (``None``) --
    the #3906 invariant.  Exactly one net routes and there is no crossing.
    """
    outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]
    pads = [
        _pad(1.0, 10.0, 1, "A1"),
        _pad(39.0, 10.0, 1, "A2"),
        _pad(20.0, 1.0, 2, "B1"),
        _pad(20.0, 19.0, 2, "B2"),
    ]
    conns = [((1, 0), pads[0], pads[1], None), ((2, 0), pads[2], pads[3], None)]
    pf = MeshPathfinder(outline, pads, DesignRules())
    routes, stats = pf.route_netset(conns, max_iterations=6)

    assert stats.routed == 1, "a blocked transverse lane must decline, not squeeze in"
    assert _cross_net_intersections(routes) == 0, "never ship a net-vs-net short"
    assert stats.triangulation_calls == 1
