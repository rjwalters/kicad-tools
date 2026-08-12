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

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pairwise_clearance import (
    AttachZone,
    build_pairwise_clearance_table,
    route_pairwise_violation,
)
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
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
    router = Router(grid, rules, diagonal_routing=True)
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
