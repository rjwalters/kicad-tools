"""The trace optimizer must not join Kelvin branches outside the shunt (#5398).

On the board 05 DRV8301 stress fixture every ISENSE net left the router as a
clean star, but the post-route optimizer rewrote one ISENSE_A+ branch into a
straight line over a sibling branch about 6 mm from the shunt pad.  These
fixtures reproduce that with a 45-degree corner chamfer: the force branch's
corner is cut and the chamfer lands on the sense branch's copper.
"""

from shapely.geometry import LineString, box
from shapely.ops import unary_union

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import (
    OptimizationConfig,
    TraceOptimizer,
    make_collision_checker,
    optimize_routes_grid_synced,
)
from kicad_tools.router.optimizer import trace as trace_module
from kicad_tools.router.optimizer.kelvin_gate import kelvin_branch_gate
from kicad_tools.router.primitives import Route, Segment

SHUNT = (2.0, 10.0)
SHUNT_METAL = box(1.6, 9.6, 2.4, 10.4)
FORCE = [(2, 10), (8, 10), (8, 14)]


def _route(net_name: str, points) -> Route:
    segments = [
        Segment(a[0], a[1], b[0], b[1], 0.2, Layer.F_CU, net=1, net_name=net_name)
        for a, b in zip(points, points[1:], strict=False)
    ]
    return Route(net=1, net_name=net_name, segments=segments)


def _board(net_name: str, sense_points, sense_pad: float = 0.4, force_end=(8, 14)) -> Autorouter:
    router = Autorouter(20, 20, force_python=True, physics_enabled=False)
    for ref, (x, y), size in (
        ("R1", SHUNT, 0.8),  # shunt: the Kelvin root
        ("Q1", force_end, 0.8),  # force terminal
        ("U1", sense_points[-1], sense_pad),  # sense terminal
    ):
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": size,
                    "height": size,
                    "net": 1,
                    "net_name": net_name,
                }
            ],
        )
    routes = [_route(net_name, FORCE), _route(net_name, sense_points)]
    for route in routes:
        router.grid.mark_route(route)
    router.routes = routes
    return router


def _optimize(router: Autorouter) -> None:
    config = OptimizationConfig(
        merge_collinear=True,
        eliminate_zigzags=True,
        compress_staircase=True,
        convert_45_corners=True,
        corner_chamfer_size=0.5,
        minimize_vias=True,
    )
    optimizer = TraceOptimizer(config=config, collision_checker=make_collision_checker(router.grid))
    optimize_routes_grid_synced(router, optimizer)


def _copper(route: Route):
    return unary_union(
        [LineString([s.start, s.end]).buffer(s.width / 2) for s in route.segments]
    ).difference(SHUNT_METAL)


def _branches_touch(router: Autorouter) -> bool:
    force, sense = router.routes
    return _copper(force).intersects(_copper(sense))


# The sense branch comes down to (7.55, 10.3): clear of the force branch's
# 90-degree corner at (8, 10), but inside the 0.5 mm chamfer that replaces it.
SENSE_UNDER_CORNER = [(2, 10), (2, 12.6), (7.55, 12.6), (7.55, 10.3)]


def test_unguarded_optimizer_joins_kelvin_branches(monkeypatch):
    """Control: without the gate the chamfer lands on the sense branch."""
    monkeypatch.setattr(trace_module, "kelvin_branch_gate", lambda router: None)
    router = _board("ISENSE_TEST", SENSE_UNDER_CORNER)
    assert not _branches_touch(router)
    _optimize(router)
    assert _branches_touch(router)


def test_gate_keeps_kelvin_branches_apart():
    router = _board("ISENSE_TEST", SENSE_UNDER_CORNER)
    assert not _branches_touch(router)
    _optimize(router)
    assert not _branches_touch(router)
    assert router._kelvin_optimize_vetoes == 1
    # The vetoed force branch keeps its original corner.
    force = router.routes[0]
    assert [(s.x1, s.y1) for s in force.segments] + [force.segments[-1].end] == FORCE


def test_gate_reports_veto(capsys):
    _optimize(_board("ISENSE_TEST", SENSE_UNDER_CORNER))
    assert "Kelvin branch isolation: 1 route(s)" in capsys.readouterr().err


def test_ordinary_net_still_optimized():
    """A non-Kelvin net with the same copper is optimized exactly as before."""
    router = _board("PWM_TEST", SENSE_UNDER_CORNER)
    assert kelvin_branch_gate(router) is None
    _optimize(router)
    assert _branches_touch(router)  # same-net merge is fine on ordinary nets
    assert not hasattr(router, "_kelvin_optimize_vetoes")


def test_kelvin_rewrite_without_new_contact_is_accepted():
    """The gate only blocks contact; it does not freeze Kelvin routes."""
    far_sense = [(2, 10), (2, 12.6), (6.0, 12.6), (6.0, 11.4)]
    router = _board("ISENSE_TEST", far_sense)
    _optimize(router)
    assert router._kelvin_optimize_vetoes == 0
    assert len(router.routes[0].segments) == 3  # force corner was chamfered
    assert not _branches_touch(router)


def test_inherited_contact_is_not_a_veto():
    """Copper the router already joined is reported elsewhere, not vetoed here."""
    touching_sense = [(2, 10), (2, 12.6), (7.55, 12.6), (7.55, 10.05)]
    router = _board("ISENSE_TEST", touching_sense)
    assert _branches_touch(router)
    _optimize(router)
    assert router._kelvin_optimize_vetoes == 0


def test_rewrite_onto_another_terminals_pad_is_vetoed():
    """#6889: contact with a sibling terminal's pad counts, not just its route."""
    # U1's 0.6 mm pad at (7.55, 10.45) sits in the chamfer's sweep; the sense
    # route comes straight down onto it and stays clear of the chamfer.
    sense = [(2, 10), (2, 12.6), (7.55, 12.6), (7.55, 10.45)]
    router = _board("ISENSE_TEST", sense, sense_pad=0.6)
    gate = kelvin_branch_gate(router)
    sense_route = router.routes[1]
    chamfered = _route("ISENSE_TEST", [(2, 10), (7.5, 10), (8, 10.5), (8, 14)])
    contacts = gate._contacts(chamfered, [sense_route])
    assert not any(kind == "route" for kind, _ in contacts)  # only the pad is hit
    sense_pad = next(pad for pad in router.pads.values() if pad.ref == "U1")
    assert ("pad", sense_pad.key) in contacts
    _optimize(router)
    assert router._kelvin_optimize_vetoes == 1
    force = router.routes[0]
    assert [(s.x1, s.y1) for s in force.segments] + [force.segments[-1].end] == FORCE


# #6889 contact location.  The force branch already touches the sense branch
# just outside the shunt (inherited from the router).  A rewrite that runs the
# force branch along the sense branch pushes the shared copper ~2.6 mm out,
# so the sense tap would read more of the force path's IR drop.
SENSE_UP = [(2, 10), (2, 14), (10, 14)]
NEAR_CONTACT = [(2, 10), (2.25, 10.6), (6, 10.6), (6, 6)]
FAR_CONTACT = [(2, 10), (2.25, 10.6), (2.15, 13), (6, 13), (6, 6)]


def _location_case():
    router = _board("ISENSE_TEST", SENSE_UP, force_end=(6, 6))
    gate = kelvin_branch_gate(router)
    sense = _route("ISENSE_TEST", SENSE_UP)
    return gate, sense


def test_inherited_contact_pushed_away_from_shunt_is_vetoed():
    gate, sense = _location_case()
    near, far = _route("ISENSE_TEST", NEAR_CONTACT), _route("ISENSE_TEST", FAR_CONTACT)
    # Same contact set either way: only its location differs.
    assert gate._contacts(near, [sense]).keys() == gate._contacts(far, [sense]).keys()
    assert not gate.accepts(near, far, [sense])
    assert gate.vetoes == 1


def test_inherited_contact_pulled_toward_shunt_is_accepted():
    gate, sense = _location_case()
    near, far = _route("ISENSE_TEST", NEAR_CONTACT), _route("ISENSE_TEST", FAR_CONTACT)
    assert gate.accepts(far, near, [sense])
    assert gate.vetoes == 0
