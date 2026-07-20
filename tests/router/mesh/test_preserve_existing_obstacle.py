"""Mesh honors preserved (non-listed) copper as a hard obstacle (#4364).

Follow-up to #4355 (lattice/grid). The mesh negotiator tracked cross-net
copper only in a **per-pass** ``committed_by_layer`` dict that was reset empty
every pass and only ever held copper laid by *listed* nets earlier in the same
pass -- the preserved copper of NON-listed nets
(``Autorouter.existing_routes``, loaded under ``--nets`` /
``--preserve-existing`` after #4355) was never seeded. So a listed net's
octilinear fit saw no foreign trace and legally crossed it -> a
``clearance_segment_segment`` short.

``MeshPathfinder.route_netset`` now accepts ``fixed_copper`` (the preserved
``Route``s) and re-seeds each pass's ``committed_by_layer`` with it -- via the
same ``_route_obstacles_by_layer`` capsule conversion the commit loop uses --
so a negotiated net routes AROUND the fixed copper or is honestly declined; it
never emits copper overlapping the fixed net.

These mirror ``tests/router/lattice/test_preserve_existing_obstacle.py`` for
the mesh engine. The mesh engine HARD-REQUIRES the native C++ triangulation
(no Python fallback), so this whole module ``importorskip``s
``router_cpp`` -- run ``uv run kct build-native`` first or these silently skip.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.mesh.pathfinder import MeshPathfinder
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules

# The mesh engine calls ``router_cpp.constrained_delaunay`` with no Python
# fallback (``pathfinder.build()``); without the native extension these tests
# cannot exercise the fix at all.
router_cpp = pytest.importorskip("kicad_tools.router.router_cpp")

_OUTLINE = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]


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


def _wall_segment(layer: Layer, *, y1: float, y2: float, width: float = 0.5) -> Segment:
    """A vertical foreign-net (net 2) copper wall at x=10 on ``layer``."""
    return Segment(
        x1=10.0,
        y1=y1,
        x2=10.0,
        y2=y2,
        width=width,
        layer=layer,
        net=2,
        net_name="N2",
    )


def _shorts_against(
    routes: dict[object, Route],
    wall: Segment,
    *,
    trace_half: float,
    clearance: float,
) -> bool:
    """True if any routed segment overlaps ``wall`` below the required gap.

    Mirrors ``clearance_segment_segment``: a same-layer, cross-net segment whose
    centreline distance to the wall is under ``trace_half + wall_half +
    clearance`` is an emitted short.
    """
    wall_half = wall.width / 2.0
    gap = trace_half + wall_half + clearance
    wa = (wall.x1, wall.y1)
    wb = (wall.x2, wall.y2)
    for route in routes.values():
        for seg in route.segments:
            if seg.layer != wall.layer or seg.net == wall.net:
                continue
            d = seg_seg_dist((seg.x1, seg.y1), (seg.x2, seg.y2), wa, wb)
            if d < gap - 1e-6:
                return True
    return False


def _fixture():
    rules = DesignRules()
    stack = LayerStack.two_layer()
    # Net 1: two pads straddling the wall at x=10; the shortest route is a
    # straight F_CU trace at y=10 that crosses the wall dead-centre.
    pads = [_pad(2.0, 10.0, 1, ref="A"), _pad(18.0, 10.0, 1, ref="B")]
    conns = [((1, 0), pads[0], pads[1], None)]
    return rules, stack, pads, conns


def test_baseline_without_fixed_copper_shorts_through_wall() -> None:
    """Without ``fixed_copper`` the straight route crosses the wall (the bug)."""
    rules, stack, pads, conns = _fixture()
    wall = _wall_segment(Layer.F_CU, y1=2.0, y2=18.0)

    pf = MeshPathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes, stats = pf.route_netset(conns, max_iterations=6)

    # The net routes (straight, shortest) and -- because the mesh never saw the
    # wall's copper -- it crosses it: a segment-segment short.
    assert stats.routed == 1
    assert _shorts_against(
        routes, wall, trace_half=rules.trace_width / 2.0, clearance=rules.trace_clearance
    )


def test_fixed_copper_prevents_short_through_preserved_net() -> None:
    """Seeding the wall as ``fixed_copper`` -> the route detours; never a short."""
    rules, stack, pads, conns = _fixture()
    wall = _wall_segment(Layer.F_CU, y1=2.0, y2=18.0)

    pf = MeshPathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes, _stats = pf.route_netset(
        conns,
        fixed_copper=[Route(net=2, net_name="N2", segments=[wall])],
        max_iterations=6,
    )

    # Whether the net detours (via to B_CU, across, back) or is declined, it is
    # NEVER emitted overlapping the F_CU wall.
    assert not _shorts_against(
        routes, wall, trace_half=rules.trace_width / 2.0, clearance=rules.trace_clearance
    )


def test_fixed_copper_full_partition_declines_not_shorts() -> None:
    """A wall on EVERY layer (no via detour) -> honest decline, not a short."""
    rules, stack, pads, conns = _fixture()
    # Full-height walls (beyond the board on both ends) on both routing layers:
    # no same-layer detour and no via crossing is possible.
    walls = [
        _wall_segment(Layer.F_CU, y1=-2.0, y2=22.0),
        _wall_segment(Layer.B_CU, y1=-2.0, y2=22.0),
    ]
    fixed = [Route(net=2, net_name="N2", segments=walls)]

    pf = MeshPathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes, stats = pf.route_netset(conns, fixed_copper=fixed, max_iterations=6)

    # Reported unroutable rather than shorted.
    assert stats.routed == 0
    for wall in walls:
        assert not _shorts_against(
            routes, wall, trace_half=rules.trace_width / 2.0, clearance=rules.trace_clearance
        )


def test_fixed_copper_empty_is_byte_identical_noop() -> None:
    """``fixed_copper=None`` / ``[]`` leaves negotiation exactly as before."""
    rules, stack, pads, conns = _fixture()

    pf_none = MeshPathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes_none, stats_none = pf_none.route_netset(conns, max_iterations=6)

    pf_empty = MeshPathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes_empty, stats_empty = pf_empty.route_netset(conns, fixed_copper=[], max_iterations=6)

    assert stats_none.routed == stats_empty.routed == 1
    # Same key set and same emitted geometry (the seed set is empty either way).
    assert routes_none.keys() == routes_empty.keys()
    for key in routes_none:
        segs_a = [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in routes_none[key].segments]
        segs_b = [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in routes_empty[key].segments]
        assert segs_a == segs_b
