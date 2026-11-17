"""The lattice engine honours ``--reserve-plane-layers`` (issue #6350).

``--layers 4`` reserves In1.Cu/In2.Cu as reference planes by default: the CLI
sets ``DesignRules.allowed_layers = ["F.Cu", "B.Cu"]`` and the grid pathfinder
and C++ backend hard-block every other layer.  Before #6350 the lattice A*
never read ``allowed_layers``, so ``--route-engine lattice`` routed signal
nets on the planes (board 05: 325 segments on In1/In2) and
``--no-reserve-plane-layers`` / ``--layers 4-all`` were byte-identical no-ops.

Fast synthetic checks on the stack ``--layers 4`` resolves to
(``four_layer_sig_gnd_pwr_sig``), modelled on ``test_hard_avoid_layers_4979``.
"""

from __future__ import annotations

from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layer_advisories import (
    plane_layer_signal_violations,
    reserve_plane_layers_allowed_layers,
)
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules

_OUTLINE = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
_INNER = {Layer.IN1_CU, Layer.IN2_CU}


def _pad(x: float, y: float, *, ref: str) -> Pad:
    return Pad(
        x=x, y=y, width=1.0, height=1.0, net=1, net_name="SIG", layer=Layer.F_CU, ref=ref, pin="1"
    )


def _pads() -> list[Pad]:
    return [_pad(5.0, 15.0, ref="A"), _pad(35.0, 15.0, ref="B")]


def _wall(*layers: Layer) -> list[Route]:
    """Foreign-net (net 2) full-height wall at x=20 on ``layers`` only."""
    segs = [
        Segment(x1=20.0, y1=-5.0, x2=20.0, y2=35.0, width=2.0, layer=lyr, net=2, net_name="N2")
        for lyr in layers
    ]
    return [Route(net=2, net_name="N2", segments=segs)]


def _reserved_rules(stack: LayerStack) -> DesignRules:
    allowed = reserve_plane_layers_allowed_layers(stack)
    assert allowed == ["F.Cu", "B.Cu"]
    return DesignRules(allowed_layers=allowed)


def _route(rules: DesignRules, stack: LayerStack, fixed: list[Route]):
    pf = LatticePathfinder(_OUTLINE, _pads(), rules, layer_stack=stack)
    conns = [((1, 0), *_pads(), None)]
    routes, stats = pf.route_netset(conns, fixed_copper=fixed, max_iterations=8)
    return pf, routes, stats


def _segment_layers(routes: dict) -> set:
    return {s.layer for route in routes.values() for s in route.segments}


def test_accessor_blocks_the_reserved_planes_for_netless_class() -> None:
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    pf = LatticePathfinder(_OUTLINE, _pads(), _reserved_rules(stack), layer_stack=stack)
    assert pf._hard_avoided_layers(None) == frozenset({1, 2})


def test_reserved_planes_inner_only_shortcut_declines() -> None:
    """Walls on F.Cu and B.Cu: the only path is via the planes -> decline."""
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    pf, routes, stats = _route(_reserved_rules(stack), stack, _wall(Layer.F_CU, Layer.B_CU))

    assert stats.routed == 0
    assert not (_segment_layers(routes) & _INNER)
    assert "layer-constrained" in pf.failure_reasons.get((1, 0), "")


def test_reservation_off_takes_the_inner_detour_again() -> None:
    """Control for the test above (``--no-reserve-plane-layers``): the wall
    really forces an inner detour, so the decline is the reservation's."""
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    _pf, routes, stats = _route(DesignRules(), stack, _wall(Layer.F_CU, Layer.B_CU))

    assert stats.routed == 1
    assert _segment_layers(routes) & _INNER


def test_four_layer_all_signal_has_no_reservation() -> None:
    """``--layers 4-all``: no PLANE layer -> ``allowed_layers is None``."""
    stack = LayerStack.four_layer_all_signal()
    assert reserve_plane_layers_allowed_layers(stack) is None
    _pf, routes, stats = _route(DesignRules(), stack, _wall(Layer.F_CU, Layer.B_CU))
    assert stats.routed == 1
    assert _segment_layers(routes) & _INNER


def test_reserved_planes_still_allow_f_to_b_through_via() -> None:
    """Wall on F.Cu only: the net crosses on B.Cu via a through via that
    steps over both planes, with zero copper on In1/In2."""
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    pf, routes, stats = _route(_reserved_rules(stack), stack, _wall(Layer.F_CU))

    assert stats.routed == 1, f"declines: {pf.failure_reasons}"
    layers = _segment_layers(routes)
    assert Layer.B_CU in layers
    assert not (layers & _INNER), f"signal copper on a reserved plane: {layers}"
    assert routes[(1, 0)].vias
    assert not plane_layer_signal_violations(list(routes.values()), stack)


def test_allowed_layers_mutation_after_construction_is_honoured() -> None:
    """The escalation ladder mutates the shared ``rules`` in place between
    rungs; the pathfinder must read ``allowed_layers`` at call time."""
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    rules = DesignRules()
    pf = LatticePathfinder(_OUTLINE, _pads(), rules, layer_stack=stack)
    assert pf._hard_avoided_layers(None) == frozenset()

    rules.allowed_layers = reserve_plane_layers_allowed_layers(stack)
    assert pf._hard_avoided_layers(None) == frozenset({1, 2})

    conns = [((1, 0), *_pads(), None)]
    routes, stats = pf.route_netset(
        conns, fixed_copper=_wall(Layer.F_CU, Layer.B_CU), max_iterations=8
    )
    assert stats.routed == 0
    assert not (_segment_layers(routes) & _INNER)


def test_sig_sig_gnd_pwr_stack_blocks_only_in2() -> None:
    """``--layers 4-sig`` (``four_layer_sig_sig_gnd_pwr``): B.Cu is MIXED, so
    only In2.Cu (GND) is reserved.  Since that is an inner layer the through
    via stays legal; the F.Cu-walled net routes without touching In2.Cu."""
    stack = LayerStack.four_layer_sig_sig_gnd_pwr()
    allowed = reserve_plane_layers_allowed_layers(stack)
    assert allowed == ["F.Cu", "In1.Cu", "B.Cu"]
    rules = DesignRules(allowed_layers=allowed)
    pf, routes, stats = _route(rules, stack, _wall(Layer.F_CU))

    assert pf._hard_avoided_layers(None) == frozenset({2})
    assert stats.routed == 1, f"declines: {pf.failure_reasons}"
    assert Layer.IN2_CU not in _segment_layers(routes)
    assert routes[(1, 0)].vias
