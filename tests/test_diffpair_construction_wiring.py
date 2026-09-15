"""The coupled pre-pass reaches geometric construction in production (#5333).

A qualified constructed pair is worth nothing if the routing pipeline never
asks for one.  These controls drive the real ``route_all_with_diffpairs`` entry
point on a pair the joint search cannot complete and assert that construction
is invoked inside the pair's EXISTING wall-clock window with its own bounded
ledgers -- and that a constructed pair is actually committed.
"""

import time
from types import SimpleNamespace

import pytest

from kicad_tools.router import pair_construction
from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.diffpair_routing import (
    CONSTRUCTION_BODY_ATTEMPTS,
    CONSTRUCTION_DEPARTURE_ITERATIONS,
)
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")

_PER_PAIR_TIMEOUT = 45.0


def _router(*, barrier: bool) -> Autorouter:
    """A fine-pitch pair whose only route needs a layer change."""
    rules = DesignRules(
        trace_width=0.15,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.1,
        manufacturer="jlcpcb",
    )
    nc = NetClassRouting(
        name="HighSpeed",
        trace_width=0.15,
        clearance=0.15,
        coupled_routing=True,
        skew_tolerance_mm=0.05,
        coupled_continuity_threshold=0.85,
    )
    router = Autorouter(
        width=16.0,
        height=16.0,
        rules=rules,
        net_class_map={"HS_P": nc, "HS_N": nc},
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
    )
    for ref, x, y in (("U1", 2.0, 2.0), ("J1", 3.5, 10.0)):
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": 0.3,
                    "height": 0.3,
                    "net": 1,
                    "net_name": "HS_P",
                },
                {
                    "number": "2",
                    "x": x,
                    "y": y + 0.6,
                    "width": 0.3,
                    "height": 0.3,
                    "net": 2,
                    "net_name": "HS_N",
                },
            ],
        )
    if barrier:
        wall = Route(
            net=99,
            net_name="BARRIER",
            segments=[
                Segment(
                    x1=0.2,
                    y1=6.0,
                    x2=15.8,
                    y2=6.0,
                    width=0.4,
                    layer=Layer.F_CU,
                    net=99,
                    net_name="BARRIER",
                )
            ],
        )
        router.grid.mark_route(wall)
        router.routes.append(wall)
    return router


def _config() -> DifferentialPairConfig:
    return DifferentialPairConfig(
        enabled=True,
        spacing=0.4,
        per_pair_timeout=_PER_PAIR_TIMEOUT,
        per_pair_max_iterations=20000,
    )


def test_construction_runs_inside_the_existing_pair_window(monkeypatch):
    router = _router(barrier=True)
    seen = []
    original = pair_construction.construct_pair_routes

    def record(router_arg, finder, pair, pads, budget, **kwargs):
        seen.append((budget, time.monotonic(), dict(kwargs)))
        return original(router_arg, finder, pair, pads, budget, **kwargs)

    monkeypatch.setattr(pair_construction, "construct_pair_routes", record)
    started = time.monotonic()
    router.route_all_with_diffpairs(_config(), non_diffpair_strategy=list)
    assert seen, "the pre-pass never reached geometric construction"
    budget, called_at, kwargs = seen[0]
    # The construction deadline lives inside the pair's own window; no budget
    # is renewed, and the native/body ledgers are the documented bounds.
    assert called_at <= budget.deadline <= started + _PER_PAIR_TIMEOUT + 5.0
    assert budget.iterations_used <= CONSTRUCTION_DEPARTURE_ITERATIONS
    assert budget.bodies_used <= CONSTRUCTION_BODY_ATTEMPTS
    assert kwargs["num_copper_layers"] == router.grid.num_layers
    assert kwargs["board_thickness_mm"] > 0


def test_constructed_pair_is_committed_with_a_real_layer_change():
    router = _router(barrier=True)
    routes, _warnings = router.route_all_with_diffpairs(_config(), non_diffpair_strategy=list)
    pair_routes = [route for route in routes if route.net in (1, 2)]
    assert {route.net for route in pair_routes} == {1, 2}
    for route in pair_routes:
        assert len({segment.layer for segment in route.segments}) > 1
        assert route.vias


def test_blind_or_buried_via_policy_declines_construction(monkeypatch):
    """Physical length measurement assumes the ordinary through-via span."""
    router = _router(barrier=True)
    router.via_rules = SimpleNamespace(allow_blind=True, allow_buried=False)

    def unexpected(*args, **kwargs):
        raise AssertionError("constructed a pair under a partial-span via policy")

    monkeypatch.setattr(pair_construction, "construct_pair_routes", unexpected)
    router.route_all_with_diffpairs(_config(), non_diffpair_strategy=list)
