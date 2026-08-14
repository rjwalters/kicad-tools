"""Pure-Python A* search-time pairwise (HV-isolation) avoidance (Issue #4507).

Epic #4431 Phase 2b armed only the C++ search (#4511): the pure-Python
fallback A* kept proposing HV-through-LV paths that the Phase-1 post-route
gate (``route_pairwise_violation``) then rejected -- so a net that fell back
on an HV board burned its budget instead of converging (observed directly by
PR #4780's convergence fixture, whose layer-agnostic arm "burns its resume
budget into the Python fallback and returns None").

This module pins the Python mirrors of the C++ search kernels:

* ``Router._cross_domain_trace_blocked`` / ``_cross_domain_via_blocked`` --
  the per-cell annulus kernels folded into ``_is_trace_blocked`` /
  ``_is_via_blocked`` (siblings of ``Pathfinder::cross_domain_trace_blocked``
  / ``cross_domain_via_blocked``);
* ``Router._pairwise_expanded_blocked`` -- the hot-loop bitmap widening
  OR-ed into ``compute_expanded_blocked``'s dilated mask (#2430), with the
  out-of-bounds-is-EMPTY dilation convention the C++ annulus uses ("OOB is
  scalar's job");
* the layer-scoped #4506 attach-zone waiver in both forms; and
* the headline convergence: a two-domain board where the HV-blind Python A*
  hugs the LV wall at the scalar floor, and the armed search detours to the
  full creepage requirement -- gate-clean, no post-route thrash.

Everything here runs WITHOUT the C++ extension -- that is the point.
"""

from __future__ import annotations

import math

import numpy as np

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pairwise_clearance import (
    AttachZone,
    build_pairwise_clearance_table,
    route_pairwise_violation,
)
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

# IEC 60664-1, PD2, material group IIIa @ 150 V -> 1.6 mm (same constant the
# C++ parity suite pins in ``test_pairwise_cpp_parity.py``).
IEC_150V_PD2_IIIA_MM = 1.6
DRU = 0.2
TRACE_WIDTH = 0.2

HV_NET = 1
LV_NET = 2
HV_TAP_NET = 3  # same 150 V domain as HV -> no widening between them

NET_NAMES = {"HV": HV_NET, "LV": LV_NET, "HV_TAP": HV_TAP_NET}
VOLTAGES = {"HV": 150.0, "LV": 0.0, "HV_TAP": 150.0}


def _rules(with_table: bool = True) -> DesignRules:
    rules = DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=DRU,
        via_diameter=0.6,
        via_clearance=DRU,
        grid_resolution=0.1,
    )
    if with_table:
        rules.pairwise_clearance = build_pairwise_clearance_table(VOLTAGES, dru=DRU)
    return rules


def _router_with_lv_wall(
    with_table: bool = True,
    *,
    wall_net: int = LV_NET,
    wall_net_name: str = "LV",
    wall_layers: tuple[Layer, ...] = (Layer.F_CU,),
    name_map: dict[str, int] | None = None,
    net_class_map: dict[str, NetClassRouting] | None = None,
) -> tuple[Router, RoutingGrid]:
    """A 30x20 board with a 6 x 0.6 mm foreign wall centred at (15, 10)."""
    rules = _rules(with_table)
    grid = RoutingGrid(width=30.0, height=20.0, rules=rules, layer_stack=LayerStack.two_layer())
    for layer in wall_layers:
        grid.add_pad(
            Pad(
                x=15.0,
                y=10.0,
                width=6.0,
                height=0.6,
                net=wall_net,
                net_name=wall_net_name,
                layer=layer,
            )
        )
    router = Router(grid, rules, diagonal_routing=True, net_class_map=net_class_map)
    router.set_net_name_to_id(dict(NET_NAMES) if name_map is None else name_map)
    return router, grid


def _grid_pos(grid: RoutingGrid, x: float, y: float) -> tuple[int, int]:
    return grid.world_to_grid(x, y)


# ---------------------------------------------------------------------------
# Per-cell annulus kernels (``_is_trace_blocked`` / ``_is_via_blocked``)
# ---------------------------------------------------------------------------


def test_trace_annulus_blocks_hv_near_lv_copper() -> None:
    """Scalar-clear, pairwise-short: the widened trace kernel hard-blocks.

    0.7 mm above the LV pad edge clears the 0.2 mm scalar rule by 3x but
    falls far short of the 1.6 mm creepage requirement.
    """
    router, grid = _router_with_lv_wall()
    gx, gy = _grid_pos(grid, 15.0, 9.0)  # 0.7 mm from the pad edge at y=9.7
    assert router._is_trace_blocked(gx, gy, 0, HV_NET) is True


def test_trace_annulus_clear_beyond_pairwise_requirement() -> None:
    router, grid = _router_with_lv_wall()
    gx, gy = _grid_pos(grid, 15.0, 7.0)  # 2.7 mm gap >= 1.6 mm + widths
    assert router._is_trace_blocked(gx, gy, 0, HV_NET) is False


def test_trace_annulus_dormant_without_table() -> None:
    """No voltage map -> the same position keeps its scalar verdict."""
    router, grid = _router_with_lv_wall(with_table=False)
    gx, gy = _grid_pos(grid, 15.0, 9.0)
    assert router._is_trace_blocked(gx, gy, 0, HV_NET) is False


def test_trace_annulus_dormant_without_name_map() -> None:
    """The projection is keyed by net id -- an empty map keeps it dormant."""
    router, grid = _router_with_lv_wall(name_map={})
    gx, gy = _grid_pos(grid, 15.0, 9.0)
    assert router._is_trace_blocked(gx, gy, 0, HV_NET) is False


def test_trace_annulus_same_domain_not_widened() -> None:
    """HV_TAP (150 V) vs an HV wall (150 V): |dV| = 0 -> no widening pair."""
    router, grid = _router_with_lv_wall(wall_net=HV_NET, wall_net_name="HV")
    gx, gy = _grid_pos(grid, 15.0, 9.0)
    assert router._is_trace_blocked(gx, gy, 0, HV_TAP_NET) is False


def test_trace_annulus_net0_copper_never_widened() -> None:
    """Net-0 blocked cells (pours / no-net obstacles) carry no domain."""
    router, grid = _router_with_lv_wall(wall_net=0, wall_net_name="")
    gx, gy = _grid_pos(grid, 15.0, 9.0)
    assert router._is_trace_blocked(gx, gy, 0, HV_NET) is False


def test_trace_annulus_other_layer_copper_ignored() -> None:
    """The trace kernel scans its own layer only (surface creepage)."""
    router, grid = _router_with_lv_wall(wall_layers=(Layer.B_CU,))
    gx, gy = _grid_pos(grid, 15.0, 9.0)
    assert router._is_trace_blocked(gx, gy, 0, HV_NET) is False


def test_via_annulus_blocks_and_clears() -> None:
    router, grid = _router_with_lv_wall()
    near = _grid_pos(grid, 15.0, 8.8)
    far = _grid_pos(grid, 15.0, 7.0)
    assert router._is_via_blocked(*near, 0, HV_NET) is True
    assert router._is_via_blocked(*far, 0, HV_NET) is False


def test_via_annulus_dormant_without_table() -> None:
    router, grid = _router_with_lv_wall(with_table=False)
    gx, gy = _grid_pos(grid, 15.0, 8.8)
    assert router._is_via_blocked(gx, gy, 0, HV_NET) is False


# ---------------------------------------------------------------------------
# Layer-scoped attach-zone waiver (#4506 / #4699 parity)
# ---------------------------------------------------------------------------

_ZONE_BBOX = (12.0, 8.0, 18.0, 12.0)  # covers the wall and the probe points


def _zone(net_layers: frozenset = frozenset()) -> AttachZone:
    return AttachZone(*_ZONE_BBOX, frozenset({"HV", "LV"}), net_layers)


def test_trace_annulus_attach_zone_waives_widening() -> None:
    """A layer-agnostic zone (no ``net_layers``) waives the widening."""
    router, grid = _router_with_lv_wall()
    router.set_attach_zones((_zone(),))
    gx, gy = _grid_pos(grid, 15.0, 9.0)
    assert router._is_trace_blocked(gx, gy, 0, HV_NET) is False


def test_trace_annulus_attach_zone_waiver_is_layer_scoped() -> None:
    """A B.Cu-only zone must NOT waive the pair on F.Cu (#4699 / #4507).

    Same board, same probe point; only the zone's licensed layers differ.
    """
    b_cu_only = frozenset({("HV", frozenset({"B.Cu"})), ("LV", frozenset({"B.Cu"}))})
    router, grid = _router_with_lv_wall()
    router.set_attach_zones((_zone(b_cu_only),))
    gx, gy = _grid_pos(grid, 15.0, 9.0)
    assert router._is_trace_blocked(gx, gy, 0, HV_NET) is True

    f_cu_scoped = frozenset({("HV", frozenset({"F.Cu"})), ("LV", frozenset({"F.Cu"}))})
    router2, grid2 = _router_with_lv_wall()
    router2.set_attach_zones((_zone(f_cu_scoped),))
    assert router2._is_trace_blocked(gx, gy, 0, HV_NET) is False


def test_trace_annulus_zone_requires_both_member_nets() -> None:
    """A zone listing only one of the two nets never waives the pair."""
    router, grid = _router_with_lv_wall()
    router.set_attach_zones((AttachZone(*_ZONE_BBOX, frozenset({"HV", "OTHER"}), frozenset()),))
    gx, gy = _grid_pos(grid, 15.0, 9.0)
    assert router._is_trace_blocked(gx, gy, 0, HV_NET) is True


# ---------------------------------------------------------------------------
# Hot-loop bitmap widening (``_pairwise_expanded_blocked``)
# ---------------------------------------------------------------------------


def test_bitmap_dormant_without_table_or_map() -> None:
    router, _grid = _router_with_lv_wall(with_table=False)
    assert router._pairwise_expanded_blocked(HV_NET, router._trace_half_width_cells) is None

    router2, _grid2 = _router_with_lv_wall(name_map={})
    assert router2._pairwise_expanded_blocked(HV_NET, router2._trace_half_width_cells) is None


def test_bitmap_dormant_for_domainless_net() -> None:
    """A net in no widening pair gets the scalar bitmap unchanged."""
    router, _grid = _router_with_lv_wall(name_map={"HV": HV_NET, "LV": LV_NET, "OTHER": 7})
    assert router._pairwise_expanded_blocked(7, router._trace_half_width_cells) is None


def test_bitmap_widens_annulus_and_respects_pair_radius() -> None:
    router, grid = _router_with_lv_wall()
    extra = router._pairwise_expanded_blocked(HV_NET, router._trace_half_width_cells)
    assert extra is not None
    near = _grid_pos(grid, 15.0, 9.0)  # 0.7 mm gap < 1.6 mm requirement
    far = _grid_pos(grid, 15.0, 7.0)  # 2.7 mm gap: outside the pair radius
    assert bool(extra[0, near[1], near[0]]) is True
    assert bool(extra[0, far[1], far[0]]) is False


def test_bitmap_does_not_seal_the_board_edge() -> None:
    """Out-of-bounds is EMPTY for the pairwise ring ("OOB is scalar's job").

    ``RoutingGrid._dilate_blocked``'s fallback pads with ones; reusing it for
    the pairwise radius would seal a ``pair_r``-wide band along every board
    edge and wall off compliant detours.  Cells near the edge but far from
    any foreign copper must stay free.
    """
    router, grid = _router_with_lv_wall()
    extra = router._pairwise_expanded_blocked(HV_NET, router._trace_half_width_cells)
    assert extra is not None
    corner = _grid_pos(grid, 1.0, 1.0)  # ~11 mm from the wall, near the edge
    assert bool(extra[0, corner[1], corner[0]]) is False


def test_bitmap_attach_zone_clears_pairwise_contribution_layer_scoped() -> None:
    router, grid = _router_with_lv_wall(wall_layers=(Layer.F_CU, Layer.B_CU))
    f_cu_scoped = frozenset({("HV", frozenset({"F.Cu"})), ("LV", frozenset({"F.Cu"}))})
    router.set_attach_zones((_zone(f_cu_scoped),))
    extra = router._pairwise_expanded_blocked(HV_NET, router._trace_half_width_cells)
    assert extra is not None
    gx, gy = _grid_pos(grid, 15.0, 9.0)  # inside the zone bbox
    assert bool(extra[0, gy, gx]) is False  # waived on the licensed layer
    assert bool(extra[1, gy, gx]) is True  # NOT waived on B.Cu


# ---------------------------------------------------------------------------
# Per-net-class copper extents (Issue #4793)
# ---------------------------------------------------------------------------
#
# The widened radius is measured from the ROUTED net's own copper half-extent:
# ``pair_r = ceil((half_mm + required) / res)``.  Taking ``half_mm`` from the
# global ``rules`` under-widens a wide class, so the Python search proposes
# cells the (authoritative) post-route gate then rejects -- rip-up/retry churn
# on exactly the HV boards the search-time gate exists to converge.  The C++
# kernels have always measured from the per-net extents ``cpp_backend`` pushes
# in via ``set_search_pair_widths``; these fixtures pin the Python mirror.
#
# Geometry (0.1 mm cells, HV<->LV requirement 1.6 mm, wall blocked out to
# y = 9.4 once its DRU halo is counted):
#
#   trace, global 0.2 mm width -> pair_r = ceil((0.10 + 1.6)/0.1) = 17 cells
#   trace, class  1.0 mm width -> pair_r = ceil((0.50 + 1.6)/0.1) = 21 cells
#   via,   global 0.6 mm diam  -> pair_r = ceil((0.30 + 1.6)/0.1) = 19 cells
#   via,   class  1.2 mm diam  -> pair_r = ceil((0.60 + 1.6)/0.1) = 22 cells
#
# so y = 7.5 (19 cells out) and y = 7.3 (21 cells out) sit in the band that
# only the class-width radius reaches.

WIDE_TRACE_WIDTH = 1.0  # 5x the 0.2 mm global default
WIDE_VIA_SIZE = 1.2  # 2x the 0.6 mm global default
_WIDE_TRACE_PROBE_Y = 7.5  # 19 cells: inside class-r 21, outside global-r 17
_WIDE_VIA_PROBE_Y = 7.3  # 21 cells: inside class-r 22, outside global-r 19


def _wide_class() -> dict[str, NetClassRouting]:
    return {
        "HV": NetClassRouting(
            name="HV",
            trace_width=WIDE_TRACE_WIDTH,
            clearance=DRU,
            via_size=WIDE_VIA_SIZE,
        )
    }


def _default_width_class() -> dict[str, NetClassRouting]:
    """A class that merely restates the global widths (must be a no-op)."""
    return {"HV": NetClassRouting(name="HV", trace_width=TRACE_WIDTH, clearance=DRU, via_size=0.6)}


def test_trace_annulus_uses_net_class_trace_width() -> None:
    """A wide-class HV net blocks a cell the global width would let through."""
    global_router, grid = _router_with_lv_wall()
    gx, gy = _grid_pos(grid, 15.0, _WIDE_TRACE_PROBE_Y)
    assert global_router._cross_domain_trace_blocked(gx, gy, 0, HV_NET, 3) is False

    wide_router, _grid = _router_with_lv_wall(net_class_map=_wide_class())
    assert wide_router._cross_domain_trace_blocked(gx, gy, 0, HV_NET, 3) is True


def test_via_annulus_uses_net_class_via_size() -> None:
    """Same defect, via analogue: ``via_size`` overrides ``via_diameter``."""
    global_router, grid = _router_with_lv_wall()
    gx, gy = _grid_pos(grid, 15.0, _WIDE_VIA_PROBE_Y)
    assert global_router._cross_domain_via_blocked(gx, gy, 0, HV_NET, 3) is False

    wide_router, _grid = _router_with_lv_wall(net_class_map=_wide_class())
    assert wide_router._cross_domain_via_blocked(gx, gy, 0, HV_NET, 3) is True


def test_bitmap_uses_net_class_trace_width() -> None:
    """The hot-loop dilation bitmap widens by the class width too."""
    global_router, grid = _router_with_lv_wall()
    gx, gy = _grid_pos(grid, 15.0, _WIDE_TRACE_PROBE_Y)
    global_extra = global_router._pairwise_expanded_blocked(HV_NET, 3)
    assert global_extra is not None
    assert bool(global_extra[0, gy, gx]) is False

    wide_router, _grid = _router_with_lv_wall(net_class_map=_wide_class())
    wide_extra = wide_router._pairwise_expanded_blocked(HV_NET, 3)
    assert wide_extra is not None
    assert bool(wide_extra[0, gy, gx]) is True


def test_class_width_equal_to_global_is_a_no_op() -> None:
    """No behaviour change when the class merely restates the global widths."""
    plain_router, grid = _router_with_lv_wall()
    same_router, _grid = _router_with_lv_wall(net_class_map=_default_width_class())
    for y in (7.3, 7.5, 7.7, 7.9, 9.0):
        gx, gy = _grid_pos(grid, 15.0, y)
        assert plain_router._cross_domain_trace_blocked(
            gx, gy, 0, HV_NET, 3
        ) == same_router._cross_domain_trace_blocked(gx, gy, 0, HV_NET, 3)
        assert plain_router._cross_domain_via_blocked(
            gx, gy, 0, HV_NET, 3
        ) == same_router._cross_domain_via_blocked(gx, gy, 0, HV_NET, 3)

    plain_extra = plain_router._pairwise_expanded_blocked(HV_NET, 3)
    same_extra = same_router._pairwise_expanded_blocked(HV_NET, 3)
    assert plain_extra is not None and same_extra is not None
    assert bool(np.array_equal(plain_extra, same_extra))


def test_unmapped_net_id_falls_back_to_global_width() -> None:
    """An id with no name in the projection keeps the pre-#4793 extent.

    ``state.id_to_name`` is the only bridge from net *id* to the class map;
    when it has no entry the resolver must degrade to ``rules.trace_width``
    (the same default the C++ kernels use before ``set_search_pair_widths``).
    """
    router, grid = _router_with_lv_wall(net_class_map=_wide_class(), name_map=dict(NET_NAMES))
    # HV_TAP is in the projection but carries no net class -> global width.
    gx, gy = _grid_pos(grid, 15.0, _WIDE_TRACE_PROBE_Y)
    assert router._cross_domain_trace_blocked(gx, gy, 0, HV_TAP_NET, 3) is False
    near = _grid_pos(grid, 15.0, 7.9)  # 15 cells: inside the global 17
    assert router._cross_domain_trace_blocked(*near, 0, HV_TAP_NET, 3) is True


# ---------------------------------------------------------------------------
# Headline: the pure-Python fallback CONVERGES on a two-domain board
# ---------------------------------------------------------------------------

# Tall LV wall: x in [14.7, 15.3], y in [3, 17], both layers.  The only way
# across is around a wall tip, so the passing distance equals the enforced
# clearance -- the discriminating geometry (a short wall lets the A* detour
# arbitrarily wide by accident).
_WALL_RECT = (14.7, 15.3, 3.0, 17.0)
_REQUIRED = IEC_150V_PD2_IIIA_MM


def _route_two_domain_board(with_table: bool):
    rules = _rules(with_table)
    grid = RoutingGrid(width=30.0, height=20.0, rules=rules, layer_stack=LayerStack.two_layer())
    start = Pad(x=3.0, y=10.0, width=0.6, height=0.6, net=HV_NET, net_name="HV", layer=Layer.F_CU)
    end = Pad(x=27.0, y=10.0, width=0.6, height=0.6, net=HV_NET, net_name="HV", layer=Layer.F_CU)
    grid.add_pad(start)
    grid.add_pad(end)
    for layer in (Layer.F_CU, Layer.B_CU):
        grid.add_pad(
            Pad(x=15.0, y=10.0, width=0.6, height=14.0, net=LV_NET, net_name="LV", layer=layer)
        )
    nc = NetClassRouting(name="HV", trace_width=TRACE_WIDTH, clearance=DRU)
    router = Router(grid, rules, diagonal_routing=True, net_class_map={"HV": nc})
    router.set_net_name_to_id({"HV": HV_NET, "LV": LV_NET})
    route = router.route(start, end, net_class=nc)
    return router, route


def _min_edge_gap(route) -> float:
    x1, x2, y1, y2 = _WALL_RECT
    best = float("inf")
    for seg in route.segments:
        for i in range(101):
            t = i / 100.0
            px = seg.x1 + t * (seg.x2 - seg.x1)
            py = seg.y1 + t * (seg.y2 - seg.y1)
            cx = min(max(px, x1), x2)
            cy = min(max(py, y1), y2)
            best = min(best, math.hypot(px - cx, py - cy))
    return best - TRACE_WIDTH / 2.0


def test_python_search_two_domain_board_converges() -> None:
    """Headline (#4507): the pure-Python A* converges on an HV board.

    The SAME geometry, routed by the SAME Python search, with and without the
    pairwise table:

    * WITHOUT the table the HV-blind search rounds the LV wall tip at the
      scalar floor -- far below the 1.6 mm creepage requirement (this is the
      route the Phase-1 gate would reject, i.e. the pre-#4507 thrash).
    * WITH the table the search detours to the full requirement and the
      post-route gate accepts it first try.
    """
    blind_router, blind_route = _route_two_domain_board(with_table=False)
    assert blind_route is not None
    assert _min_edge_gap(blind_route) < _REQUIRED  # the discriminating hug

    armed_router, armed_route = _route_two_domain_board(with_table=True)
    assert armed_route is not None, "domain-aware Python search failed to converge"
    assert _min_edge_gap(armed_route) >= _REQUIRED - 1e-3
    # The Phase-1 acceptance gate agrees with the search verdict.
    assert (
        route_pairwise_violation(
            armed_route,
            HV_NET,
            armed_router.grid.routes,
            armed_router.rules.pairwise_clearance,
            id_to_name={HV_NET: "HV", LV_NET: "LV"},
        )
        is None
    )


# ---------------------------------------------------------------------------
# cpp_backend fallback threading (#4507)
# ---------------------------------------------------------------------------


def test_cpp_backend_setter_forwards_name_map_to_fallback_router() -> None:
    """``CppPathfinder.set_net_name_to_id`` keeps the fallback in lock-step.

    Without the forward the fallback Router's domain projection is empty and
    its search-time kernels stay dormant (HV-blind) -- exactly the state this
    slice removes.  White-box: inject a live fallback router and assert the
    setter propagates.  This does not require the C++ extension.
    """
    from kicad_tools.router.cpp_backend import CppPathfinder

    router, _grid = _router_with_lv_wall(with_table=False, name_map={})
    backend = CppPathfinder.__new__(CppPathfinder)  # no C++ construction
    backend._net_name_to_id = {}
    backend._pairwise_cpp_payload = None
    backend._pairwise_cpp_installed = False
    backend._py_router = router
    CppPathfinder.set_net_name_to_id(backend, NET_NAMES)
    assert router._net_name_to_id == NET_NAMES


# ---------------------------------------------------------------------------
# Hot-loop bitmap cache (Issue #4794)
# ---------------------------------------------------------------------------
#
# ``_pairwise_expanded_blocked`` used to pay a full-grid ``np.isin`` plus a
# full-grid dilation *per non-dormant foreign domain, per route() call*, even
# though nothing about the foreign copper had changed between calls.  The
# masks are now memoized per ``(foreign_dom, pair_r)`` and stamped with
# ``RoutingGrid.occupancy_generation``, which every write to
# ``grid._blocked``/``grid._net`` advances.
#
# The tests below pin the three things that make that safe: the cache is a
# real hit (no recompute), it is DROPPED the moment foreign copper is
# committed through the production commit path, and its key separates net
# classes whose resolved widths differ.


class _DilationCounter:
    """Wrap ``Router._dilate_zero_padded`` to count real recomputations."""

    def __init__(self, router: Router) -> None:
        self.calls = 0
        self._inner = router._dilate_zero_padded
        router._dilate_zero_padded = self._wrapped  # type: ignore[method-assign]

    def _wrapped(self, mask: np.ndarray, radius: int) -> np.ndarray:
        self.calls += 1
        return self._inner(mask, radius)


def _lv_trace(y: float = 5.0) -> Route:
    """A committed LV trace far from the wall, at ``y`` on F.Cu."""
    return Route(
        net=LV_NET,
        net_name="LV",
        segments=[
            Segment(
                x1=3.0,
                y1=y,
                x2=7.0,
                y2=y,
                width=TRACE_WIDTH,
                layer=Layer.F_CU,
                net=LV_NET,
                net_name="LV",
            )
        ],
    )


def test_bitmap_cache_serves_repeat_calls_without_recomputing() -> None:
    router, _grid = _router_with_lv_wall()
    radius = router._trace_half_width_cells
    counter = _DilationCounter(router)

    first = router._pairwise_expanded_blocked(HV_NET, radius)
    assert first is not None
    assert counter.calls == 1

    second = router._pairwise_expanded_blocked(HV_NET, radius)
    assert second is not None
    assert counter.calls == 1, "second call recomputed instead of hitting the cache"
    assert bool(np.array_equal(first, second))


def test_bitmap_cache_is_invalidated_by_a_foreign_route_commit() -> None:
    """Stale-mask regression guard -- the whole point of Issue #4794.

    Commits go through ``grid.mark_route`` (what ``Autorouter._mark_route``
    calls in production), NOT a hand-poked array write, so this test fails on
    a cache with no invalidation and passes on the shipped one.
    """
    router, grid = _router_with_lv_wall()
    radius = router._trace_half_width_cells
    probe_x, probe_y = _grid_pos(grid, 5.0, 5.0)  # ~11 mm from the wall

    before = router._pairwise_expanded_blocked(HV_NET, radius)
    assert before is not None
    assert bool(before[0, probe_y, probe_x]) is False

    generation_before = grid.occupancy_generation
    grid.mark_route(_lv_trace())
    assert grid.occupancy_generation > generation_before

    after = router._pairwise_expanded_blocked(HV_NET, radius)
    assert after is not None
    assert bool(after[0, probe_y, probe_x]) is True, "served a stale pairwise mask"
    assert not bool(np.array_equal(before, after))


def test_bitmap_cache_key_separates_two_classes_in_one_domain() -> None:
    """HV (1.0 mm class) and HV_TAP (global 0.2 mm) share a domain, not a mask."""
    gx, gy = _grid_pos(_router_with_lv_wall()[1], 15.0, _WIDE_TRACE_PROBE_Y)

    router, _grid = _router_with_lv_wall(net_class_map=_wide_class())
    wide = router._pairwise_expanded_blocked(HV_NET, 3)
    narrow = router._pairwise_expanded_blocked(HV_TAP_NET, 3)
    assert wide is not None and narrow is not None
    assert bool(wide[0, gy, gx]) is True
    assert bool(narrow[0, gy, gx]) is False

    # Order must not matter: whichever class populates the cache first, the
    # other still gets its own radius.
    reversed_router, _g = _router_with_lv_wall(net_class_map=_wide_class())
    narrow_first = reversed_router._pairwise_expanded_blocked(HV_TAP_NET, 3)
    wide_second = reversed_router._pairwise_expanded_blocked(HV_NET, 3)
    assert narrow_first is not None and wide_second is not None
    assert bool(np.array_equal(narrow, narrow_first))
    assert bool(np.array_equal(wide, wide_second))


def test_bitmap_cache_matches_a_forced_recompute_across_a_full_sequence() -> None:
    """Parity over hit / class-miss / invalidation, cached vs uncached."""
    cached_router, cached_grid = _router_with_lv_wall(net_class_map=_wide_class())
    plain_router, plain_grid = _router_with_lv_wall(net_class_map=_wide_class())
    # Force every call on ``plain_router`` to recompute from the live grid.
    plain_router._pairwise_mask_cache = lambda state: None  # type: ignore[assignment,method-assign]

    def _compare(net: int, radius: int) -> None:
        cached = cached_router._pairwise_expanded_blocked(net, radius)
        plain = plain_router._pairwise_expanded_blocked(net, radius)
        assert cached is not None and plain is not None
        assert bool(np.array_equal(cached, plain))

    _compare(HV_NET, 3)  # cold
    _compare(HV_NET, 3)  # cache hit
    _compare(HV_TAP_NET, 3)  # miss: different resolved class width
    _compare(HV_NET, 4)  # miss: different scalar radius
    for grid in (cached_grid, plain_grid):
        grid.mark_route(_lv_trace())
    _compare(HV_NET, 3)  # invalidated by the commit above
    _compare(HV_TAP_NET, 3)


def test_bitmap_cache_is_untouched_on_the_dormant_paths() -> None:
    """``pairwise_clearance is None`` (and friends) add zero bookkeeping."""
    no_table, _grid = _router_with_lv_wall(with_table=False)
    assert no_table._pairwise_expanded_blocked(HV_NET, no_table._trace_half_width_cells) is None
    assert no_table._pairwise_extra_cache == {}
    assert no_table._pairwise_extra_cache_stamp is None

    domainless, _g2 = _router_with_lv_wall(name_map={"HV": HV_NET, "LV": LV_NET, "OTHER": 7})
    assert domainless._pairwise_expanded_blocked(7, domainless._trace_half_width_cells) is None
    assert domainless._pairwise_extra_cache == {}
    assert domainless._pairwise_extra_cache_stamp is None


def test_bitmap_result_is_never_a_live_cache_entry() -> None:
    """A caller mutating the returned bitmap must not poison the cache."""
    router, _grid = _router_with_lv_wall()
    radius = router._trace_half_width_cells
    first = router._pairwise_expanded_blocked(HV_NET, radius)
    assert first is not None
    first[:] = True

    second = router._pairwise_expanded_blocked(HV_NET, radius)
    assert second is not None
    assert not bool(np.all(second))


def test_bitmap_cache_does_not_bake_in_the_attach_zone_waiver() -> None:
    """The zone waiver is per-net; the cached base mask is not.

    HV and HV_TAP share a domain AND (no class map) a resolved width, so they
    share a cache entry -- but only HV is named in the zone, so only HV may
    see the widening waived.
    """
    router, grid = _router_with_lv_wall()
    router.set_attach_zones((_zone(),))  # licenses HV <-> LV only
    radius = router._trace_half_width_cells
    gx, gy = _grid_pos(grid, 15.0, 9.0)

    waived = router._pairwise_expanded_blocked(HV_NET, radius)
    assert waived is not None
    assert bool(waived[0, gy, gx]) is False

    unwaived = router._pairwise_expanded_blocked(HV_TAP_NET, radius)
    assert unwaived is not None
    assert bool(unwaived[0, gy, gx]) is True
