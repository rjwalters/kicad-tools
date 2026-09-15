"""Routing must use authored skew limits even with looser pair-type defaults."""

from types import SimpleNamespace

import pytest

from kicad_tools.router.diffpair import detect_differential_pairs
from kicad_tools.router.diffpair_routing import DiffPairRouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import NetClassRouting


def _fixture(limit, mapping_style):
    pair = detect_differential_pairs({1: "DQS_P", 2: "DQS_N"})[0]
    classes = {
        "DQS_P": NetClassRouting(name="P", skew_tolerance_mm=0.075),
        "DQS_N": NetClassRouting(name="N", skew_tolerance_mm=limit),
    }
    routes = {
        net: [
            Route(
                net=net,
                net_name=name,
                segments=[Segment(0, net, length, net, 0.15, Layer.F_CU, net, name)],
            )
        ]
        for net, name, length in [(1, "DQS_P", 10.0), (2, "DQS_N", 10.2)]
    }
    autorouter = SimpleNamespace(route_net=routes.__getitem__)
    if mapping_style == "per_net":
        autorouter.net_class_map = classes
    else:
        autorouter.net_class_routing = {nc.name: nc for nc in classes.values()}
        autorouter.net_to_class = {net: nc.name for net, nc in classes.items()}
    router = DiffPairRouter.__new__(DiffPairRouter)
    router.autorouter = autorouter
    return router, pair, classes


@pytest.mark.parametrize("mapping_style", ["per_net", "per_class"])
def test_independent_fallback_warns_at_authored_limit(mapping_style):
    router, pair, _ = _fixture(0.05, mapping_style)
    original_rules = pair.rules
    assert original_rules.max_length_delta == 1.0
    routes, warning = router.route_differential_pair_independent(pair)
    assert len(routes) == 2
    assert warning is not None  # .2mm would pass the old 1mm default.
    assert warning.max_allowed == 0.05
    assert pair.rules.max_length_delta == 0.05
    assert original_rules.max_length_delta == 1.0  # Shared defaults stay unchanged.


def test_coupled_entry_uses_authored_limit_before_constructing_geometry(monkeypatch):
    router, pair, _ = _fixture(0.05, "per_net")

    def inspect_constraints(received):
        assert received.rules.max_length_delta == 0.05
        return None

    monkeypatch.setattr(router, "_get_pair_pads", inspect_constraints)
    assert router.route_differential_pair_coupled(pair) == ([], None)


@pytest.mark.parametrize("existing,authored,expected", [(0.025, 0.05, 0.025), (1.0, 0.0, 0.0)])
def test_stricter_pair_and_exact_match_constraints_are_preserved(existing, authored, expected):
    router, pair, _ = _fixture(authored, "per_net")
    pair.rules.max_length_delta = existing
    router.route_differential_pair_independent(pair)
    assert pair.rules.max_length_delta == expected


def test_unset_authored_limits_preserve_configured_rules():
    router, pair, classes = _fixture(None, "per_net")
    classes["DQS_P"].skew_tolerance_mm = None
    original_rules = pair.rules
    _, warning = router.route_differential_pair_independent(pair)
    assert warning is None
    assert pair.rules is original_rules
    assert pair.rules.max_length_delta == 1.0
