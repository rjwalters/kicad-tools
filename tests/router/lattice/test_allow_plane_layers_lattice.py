"""The lattice engine honours ``allow_plane_layers`` (Issue #6876).

Counterpart of ``test_reserve_plane_layers_6350.py``: with In1.Cu/In2.Cu
reserved (``allowed_layers=["F.Cu", "B.Cu"]``) a class that opts in to In2.Cu
may route there, and only there; a class without the field still declines.
"""

from __future__ import annotations

from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layer_advisories import reserve_plane_layers_allowed_layers
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

_OUTLINE = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
IN1, IN2 = 1, 2


def _pads() -> list[Pad]:
    return [
        Pad(x=x, y=15.0, width=1.0, height=1.0, net=1, net_name="SIG", layer=Layer.F_CU,
            ref=ref, pin="1")
        for x, ref in ((5.0, "A"), (35.0, "B"))
    ]  # fmt: skip


def _wall(*layers: Layer) -> list[Route]:
    segs = [
        Segment(x1=20.0, y1=-5.0, x2=20.0, y2=35.0, width=2.0, layer=lyr, net=2, net_name="N2")
        for lyr in layers
    ]
    return [Route(net=2, net_name="N2", segments=segs)]


def _rules() -> tuple[DesignRules, LayerStack]:
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    return DesignRules(allowed_layers=reserve_plane_layers_allowed_layers(stack)), stack


def _route(net_class: NetClassRouting | None):
    rules, stack = _rules()
    pf = LatticePathfinder(_OUTLINE, _pads(), rules, layer_stack=stack)
    conns = [((1, 0), *_pads(), net_class)]
    routes, stats = pf.route_netset(
        conns, fixed_copper=_wall(Layer.F_CU, Layer.B_CU), max_iterations=8
    )
    return pf, routes, stats


def _layers(routes: dict) -> set:
    return {s.layer for route in routes.values() for s in route.segments}


def test_accessor_unblocks_only_the_opted_in_plane() -> None:
    rules, stack = _rules()
    pf = LatticePathfinder(_OUTLINE, _pads(), rules, layer_stack=stack)
    opted = NetClassRouting(name="USB", allow_plane_layers=[IN2])
    assert pf._hard_avoided_layers(None) == frozenset({IN1, IN2})
    assert pf._hard_avoided_layers(NetClassRouting(name="Plain")) == frozenset({IN1, IN2})
    assert pf._hard_avoided_layers(opted) == frozenset({IN1})
    # Hard avoid wins over the opt-in.
    hard = NetClassRouting(
        name="HV", allow_plane_layers=[IN2], avoid_layers=[IN2], target_ampacity=1.0
    )
    assert pf._hard_avoided_layers(hard) == frozenset({IN1, IN2})


def test_preferred_plane_survives_when_opted_in() -> None:
    """#6854's ``preferred_layers`` steering can now point at the plane."""
    rules, stack = _rules()
    pf = LatticePathfinder(_OUTLINE, _pads(), rules, layer_stack=stack)
    pref_only = NetClassRouting(name="P", preferred_layers=[IN2])
    both = NetClassRouting(name="P", preferred_layers=[IN2], allow_plane_layers=[IN2])
    assert pf._preferred_layers(pref_only) == frozenset()
    assert pf._preferred_layers(both) == frozenset({IN2})


def test_control_without_opt_in_declines() -> None:
    pf, routes, stats = _route(NetClassRouting(name="Plain"))
    assert stats.routed == 0
    assert not _layers(routes) & {Layer.IN1_CU, Layer.IN2_CU}
    assert "layer-constrained" in pf.failure_reasons.get((1, 0), "")


def test_opted_in_class_crosses_on_in2() -> None:
    """Fails on main: the reserved In2.Cu was blocked for every class."""
    pf, routes, stats = _route(NetClassRouting(name="USB", allow_plane_layers=[IN2]))
    assert stats.routed == 1, f"declines: {pf.failure_reasons}"
    layers = _layers(routes)
    assert Layer.IN2_CU in layers, layers
    assert Layer.IN1_CU not in layers, layers


# ---------------------------------------------------------------------------
# Diff-pair tie step (#6866's ``pair_permitted_layers`` use)
# ---------------------------------------------------------------------------


def _tie_router(classes: dict):
    from tests.router.lattice.test_tie_layer_filter_6866 import _router

    return _router(allowed=["F.Cu", "B.Cu"], classes=classes)


def _tie_layers(router) -> dict[str, str]:
    return {
        route.net_name: route.segments[0].layer.kicad_name
        for _k, route, _e in router._lattice_pair_ties
    }


def test_tie_uses_the_plane_when_both_nets_opt_in() -> None:
    """Fails on main: In1.Cu is reserved, so the tie took B.Cu."""
    from dataclasses import replace

    from tests.router.lattice.test_coupled_pairs import _HS

    opted = replace(_HS, allow_plane_layers=[IN1])
    router = _tie_router({"D+": opted, "D-": opted})
    router._lattice_netset_connections()
    assert _tie_layers(router) == {"D+": "F.Cu", "D-": "In1.Cu"}


def test_tie_stays_off_the_plane_when_one_net_opts_in() -> None:
    from dataclasses import replace

    from tests.router.lattice.test_coupled_pairs import _HS

    router = _tie_router({"D+": replace(_HS, allow_plane_layers=[IN1]), "D-": _HS})
    router._lattice_netset_connections()
    layers = _tie_layers(router)
    assert set(layers) == {"D+", "D-"}
    assert set(layers.values()) <= {"F.Cu", "B.Cu"}
