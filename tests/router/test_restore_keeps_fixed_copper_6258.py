"""Issue #6258: post-route passes must still see copper kept by --preserve-existing.

On the board 06 assembled LVDS demo, ``_restore_route_grid`` (run right before
the trace optimizer) unmarked every route in ``grid.routes`` -- including the
fixed copper ``load_pcb_for_routing`` registers for ``--preserve-existing`` --
and re-marked only ``router.routes``.  The GND plane-stitching vias vanished
from the via R-tree and the raster, so ``compress_staircase`` ran OUT4 straight
through one on B.Cu (a 0.312 mm ``shorting_items``), and ``kct route`` still
reported ``Nets routed: 8/8``.
"""

from __future__ import annotations

import pytest

from kicad_tools.cli.route_cmd import _restore_route_grid
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import GridCollisionChecker, make_collision_checker
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules

GND, SIG, OTHER = 2, 1, 3


def _router() -> Autorouter:
    return Autorouter(
        20,
        20,
        rules=DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.15,
            via_diameter=0.6,
            via_drill=0.3,
            via_clearance=0.15,
        ),
        force_python=True,
    )


def _stitching_via() -> Route:
    """A GND plane via and its pad stub, as ``load_pcb_for_routing`` keeps them."""
    via = Via(x=10.0, y=10.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=GND)
    stub = Segment(8.8, 10.0, 10.0, 10.0, 0.3, Layer.F_CU, GND, "GND")
    return Route(net=GND, net_name="GND", segments=[stub], vias=[via])


def _load_fixed(router: Autorouter, route: Route) -> None:
    # Mirrors load_pcb_for_routing's --preserve-existing registration.
    router.grid.mark_route(route)
    router.grid._mark_route_on_cpp_cells(route)
    router.existing_routes.append(route)


def _routed(net: int, y: float) -> Route:
    return Route(net=net, net_name=f"N{net}", segments=[Segment(2, y, 18, y, 0.2, Layer.B_CU, net)])


def _through_via():
    """A B.Cu track whose centreline passes 0.088 mm from the via centre."""
    return (7.0, 13.088, 13.0, 7.088, Layer.B_CU, 0.2, SIG)


@pytest.mark.parametrize("checker", ["auto", "grid"])
def test_restore_keeps_preserved_via_visible_to_optimizer(checker):
    router = _router()
    fixed = _stitching_via()
    _load_fixed(router, fixed)
    routed = [_routed(SIG, 3.0), _routed(OTHER, 17.0)]
    for route in routed:
        router._mark_route(route)
    router.routes = list(routed)

    _restore_route_grid(router, router.routes)

    assert fixed in router.grid.routes, "fixed copper dropped from the grid"
    assert all(route in router.grid.routes for route in routed)
    assert len(router.grid.routes) == 3
    assert router.routes == routed  # fixed copper is never made rippable
    make = make_collision_checker if checker == "auto" else GridCollisionChecker
    assert not make(router.grid).path_is_clear(*_through_via())
    # A path well clear of the via is still accepted.
    assert make(router.grid).path_is_clear(2.0, 5.0, 18.0, 5.0, Layer.B_CU, 0.2, SIG)


def test_restore_is_idempotent_for_fixed_copper():
    router = _router()
    fixed = _stitching_via()
    _load_fixed(router, fixed)
    routed = _routed(SIG, 3.0)
    router._mark_route(routed)
    router.routes = [routed]

    _restore_route_grid(router, router.routes)
    _restore_route_grid(router, router.routes)

    assert router.grid.routes.count(fixed) == 1
    assert sum(v is fixed.vias[0] for v in router.grid._via_rtree_items.values()) <= 1
    assert not make_collision_checker(router.grid).path_is_clear(*_through_via())


def test_routed_track_through_preserved_via_is_demoted_not_counted():
    router = _router()
    _load_fixed(router, _stitching_via())
    shorting = Route(
        net=SIG,
        net_name="N1",
        segments=[Segment(7.0, 13.088, 13.0, 7.088, 0.2, Layer.B_CU, SIG)],
    )
    clean = _routed(OTHER, 17.0)
    for route in (shorting, clean):
        router._mark_route(route)
    router.routes = [shorting, clean]

    assert router.revalidate_committed_copper_or_demote() == [SIG]
    assert router.routes == [clean]
    assert shorting not in router.grid.routes


def test_short_between_two_kept_routes_is_not_blamed_on_routing():
    router = _router()
    _load_fixed(router, _stitching_via())
    # A second kept net whose input copper already overlaps the GND via.
    _load_fixed(
        router,
        Route(
            net=OTHER,
            net_name="N3",
            segments=[Segment(7.0, 13.088, 13.0, 7.088, 0.2, Layer.B_CU, OTHER)],
        ),
    )
    routed = _routed(SIG, 3.0)
    router._mark_route(routed)
    router.routes = [routed]

    assert router.demote_routing_short_nets() == []
    assert router.routes == [routed]
