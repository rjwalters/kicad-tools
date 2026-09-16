"""The actual match-group caller retains each pair's coupling policy."""

import pytest

from kicad_tools.router import coordinated_tuning
from kicad_tools.router.core import Autorouter
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.match_group_length import MatchGroup
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting
from tests.test_match_group_opposite_loop import fixture


@pytest.mark.parametrize("coupled", [False, True])
def test_scalar_reference_does_not_replace_authored_pair_coupling(monkeypatch, coupled):
    _, routes = fixture()
    host = routes[3].segments[0]
    routes[5] = Route(
        net=5,
        net_name="scalar",
        segments=[Segment(host.x1, 90, host.x2, 90, host.width, host.layer, net=5)],
    )
    router = Autorouter(
        width=100,
        height=100,
        force_python=True,
        rules=DesignRules(grid_resolution=0.1, trace_clearance=0.15),
    )
    router.net_names = {net: route.net_name for net, route in routes.items()}
    pair_class = NetClassRouting(
        name="pair", coupled_routing=coupled, length_critical=True, intra_pair_clearance=0.1
    )
    router.net_class_map = {
        routes[1].net_name: pair_class,
        routes[2].net_name: pair_class,
        "scalar": NetClassRouting(name="scalar", coupled_routing=False, length_critical=True),
    }
    router.restore_route_snapshot(list(routes.values()))
    group = MatchGroup("mixed", [5], pair_ids=[(1, 2)], reference_net_id=5, tolerance=0.05)
    calls = []
    original = coordinated_tuning.coordinated_pair_loop

    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(coordinated_tuning, "coordinated_pair_loop", observe)
    result = router.apply_match_group_tuning(detected_groups=[group], verbose=False)["mixed"]
    assert bool(calls) is coupled
    if coupled:
        for net in (1, 2):
            route, report = result[net]
            assert report.success
            assert LengthTracker.calculate_route_length(route) == pytest.approx(14)
        assert LengthTracker.calculate_route_length(result[1][0]) == pytest.approx(
            LengthTracker.calculate_route_length(result[2][0])
        )
