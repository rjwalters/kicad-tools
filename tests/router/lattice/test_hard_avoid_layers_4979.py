"""``--strict-layers`` hard-blocks avoided layers in the LATTICE engine (#4979).

Before this fix the lattice A* (``--route-engine lattice``, the
``--complete`` default) never read ``NetClassRouting.avoid_layers`` /
``hard_avoided_layer_indices`` at all -- the search only ever consulted
``net_class.trace_width`` / ``.clearance`` (issue #4271) for copper
geometry.  A net whose class declared ``avoid_layers=["In1.Cu", "In2.Cu"]``
with ``preferred_layers=["F.Cu", "B.Cu"]`` and ran under ``--strict-layers``
could still be committed onto the forbidden inner layer whenever an outer
detour was congested or blocked -- exactly the softstart rev-C PGND report
(29 segments + 2 vias landed on In2.Cu, the selected 0.5 oz reference
plane, with no hard layer-intent failure reported).

These are fast unit-level checks (a handful of pads, no full board or
``kicad-cli``) on a synthetic 4-layer (F.Cu/In1.Cu/In2.Cu/B.Cu, all-signal)
board, pinning the two acceptance scenarios from #4979:

1. A legal outer-layer (F.Cu/B.Cu-only) path is available -> the net routes
   normally, entirely off the hard-avoided inner layers.
2. The ONLY path is an inner-layer shortcut (a foreign-net wall blocks
   every outer-layer crossing but leaves the inner layers open) -> under
   ``--strict-layers`` the connection DECLINES (a layer-constrained
   residual); the baseline (no hard block) proves the wall genuinely forces
   an inner-layer detour, so the decline is attributable to the layer
   constraint, not an unrelated geometric dead end.
"""

from __future__ import annotations

from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

_OUTLINE = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]

# Grid-layer indices for ``LayerStack.four_layer_all_signal()``.
_F_CU, _IN1_CU, _IN2_CU, _B_CU = 0, 1, 2, 3


def _pgnd_class(*, hard: bool) -> NetClassRouting:
    """The softstart rev-C PGND class from the #4979 report.

    ``hard=False`` mirrors a net class with no hard layer intent (the
    pre-#4979 no-op baseline); ``hard=True`` is the reported
    ``avoid_layers``/``preferred_layers`` pair.
    """
    return NetClassRouting(
        name="HV",
        trace_width=0.6,
        clearance=0.2,
        preferred_layers=[_F_CU, _B_CU] if hard else None,
        avoid_layers=[_IN1_CU, _IN2_CU] if hard else None,
    )


def _pad(x: float, y: float, net: int, *, ref: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name="PGND",
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _stack() -> LayerStack:
    return LayerStack.four_layer_all_signal()


def _segment_layers(routes: dict) -> set:
    return {s.layer for route in routes.values() for s in route.segments}


# ---------------------------------------------------------------------------
# 1. Legal outer path available -> routes normally, never touches In1/In2.
# ---------------------------------------------------------------------------


def test_strict_layers_legal_outer_path_routes_on_outer_layers_only() -> None:
    pads = [_pad(5.0, 15.0, 1, ref="A"), _pad(35.0, 15.0, 1, ref="B")]
    rules = DesignRules(strict_layers=True)
    net_class = _pgnd_class(hard=True)
    conns = [((1, 0), pads[0], pads[1], net_class)]

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=_stack())
    routes, stats = pf.route_netset(conns, max_iterations=6)

    assert stats.routed == 1, f"declines: {pf.failure_reasons}"
    layers = _segment_layers(routes)
    assert layers, "expected at least one emitted segment"
    assert layers <= {Layer.F_CU, Layer.B_CU}, f"copper landed on a hard-avoided layer: {layers}"
    assert not routes[(1, 0)].vias, "no layer change should be needed for a direct F.Cu path"


def test_without_strict_layers_the_same_class_is_still_a_no_op_when_unset() -> None:
    """Control: a class with no ``avoid_layers`` is unaffected (byte-identical)."""
    pads = [_pad(5.0, 15.0, 1, ref="A"), _pad(35.0, 15.0, 1, ref="B")]
    rules = DesignRules(strict_layers=True)
    net_class = _pgnd_class(hard=False)
    conns = [((1, 0), pads[0], pads[1], net_class)]

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=_stack())
    routes, stats = pf.route_netset(conns, max_iterations=6)

    assert stats.routed == 1
    assert pf._hard_avoided_layers(net_class) == frozenset()


# ---------------------------------------------------------------------------
# 2. Only an inner-layer shortcut exists -> fail closed, never ship it.
# ---------------------------------------------------------------------------


def _outer_wall() -> list[Segment]:
    """A foreign-net (net 2) wall blocking EVERY outer-layer crossing.

    Full-height, on BOTH F.Cu and B.Cu, at x=20 -- between the two pads.
    In1.Cu / In2.Cu carry no wall copper at all, so the only way across is
    an inner-layer via detour.
    """
    return [
        Segment(
            x1=20.0, y1=-5.0, x2=20.0, y2=35.0, width=2.0, layer=Layer.F_CU, net=2, net_name="N2"
        ),
        Segment(
            x1=20.0, y1=-5.0, x2=20.0, y2=35.0, width=2.0, layer=Layer.B_CU, net=2, net_name="N2"
        ),
    ]


def _fixture():
    pads = [_pad(5.0, 15.0, 1, ref="A"), _pad(35.0, 15.0, 1, ref="B")]
    rules = DesignRules(strict_layers=True)
    wall = _outer_wall()
    fixed = [Route(net=2, net_name="N2", segments=wall)]
    return pads, rules, wall, fixed


def test_baseline_wall_forces_an_inner_layer_detour() -> None:
    """Sanity: WITHOUT a hard block the wall genuinely forces an inner-layer
    via detour (proves scenario 2's wall is a real inner-only shortcut, not
    just an impossible route)."""
    pads, rules, _wall, fixed = _fixture()
    conns = [((1, 0), pads[0], pads[1], None)]  # no net_class -> no hard block

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=_stack())
    routes, stats = pf.route_netset(conns, fixed_copper=fixed, max_iterations=8)

    assert stats.routed == 1, f"declines: {pf.failure_reasons}"
    layers = _segment_layers(routes)
    assert layers & {Layer.IN1_CU, Layer.IN2_CU}, (
        f"expected the wall to force an inner-layer detour, got layers={layers}"
    )


def test_strict_layers_inner_only_shortcut_declines_not_ships() -> None:
    """The reported defect, pinned: under ``--strict-layers`` the ONLY path
    (an inner-layer shortcut) is refused -- the connection declines with a
    layer-constrained reason instead of writing the forbidden copper."""
    pads, rules, _wall, fixed = _fixture()
    net_class = _pgnd_class(hard=True)
    conns = [((1, 0), pads[0], pads[1], net_class)]

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=_stack())
    routes, stats = pf.route_netset(conns, fixed_copper=fixed, max_iterations=8)

    # Never shipped as partial progress: the connection is simply absent.
    assert stats.routed == 0
    assert (1, 0) not in routes
    layers = _segment_layers(routes)
    assert not (layers & {Layer.IN1_CU, Layer.IN2_CU})

    # Honest, layer-attributed decline -- not a bare "no-path".
    reason = pf.failure_reasons.get((1, 0), "")
    assert "layer-constrained" in reason, f"unexpected decline reason: {reason!r}"


# ---------------------------------------------------------------------------
# 3. A hard block must not WALL THE NET IN: F<->B is still reachable.
# ---------------------------------------------------------------------------


def test_hard_block_still_allows_an_f_to_b_via_step_over() -> None:
    """Blocking both inner layers must not strand the net on one outer layer.

    The wall here is on F.Cu ONLY, so the single legal route is "escape on
    F.Cu, via down to B.Cu, cross, come back".  The lattice's via model hops
    between ADJACENT layers, so a naive hard block on In1/In2 would make
    F.Cu -> B.Cu unreachable and decline a connection the grid backend routes
    without complaint (its via loop offers every routable layer directly).
    The search therefore steps OVER a forbidden layer to the next allowed one:
    one through via, zero copper on In1/In2.
    """
    pads = [_pad(5.0, 15.0, 1, ref="A"), _pad(35.0, 15.0, 1, ref="B")]
    rules = DesignRules(strict_layers=True)
    f_only_wall = [
        Segment(
            x1=20.0, y1=-5.0, x2=20.0, y2=35.0, width=2.0, layer=Layer.F_CU, net=2, net_name="N2"
        )
    ]
    fixed = [Route(net=2, net_name="N2", segments=f_only_wall)]
    net_class = _pgnd_class(hard=True)
    conns = [((1, 0), pads[0], pads[1], net_class)]

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=_stack())
    routes, stats = pf.route_netset(conns, fixed_copper=fixed, max_iterations=8)

    assert stats.routed == 1, f"declines: {pf.failure_reasons}"
    layers = _segment_layers(routes)
    assert Layer.B_CU in layers, f"expected a B.Cu crossing, got {layers}"
    assert layers <= {Layer.F_CU, Layer.B_CU}, f"copper on a hard-avoided layer: {layers}"
    assert routes[(1, 0)].vias, "an F<->B crossing needs at least one via"
    for via in routes[(1, 0)].vias:
        assert set(via.layers) <= {Layer.F_CU, Layer.B_CU}, (
            f"via terminates on a hard-avoided layer: {via.layers}"
        )


def test_hard_blocked_outer_layer_forbids_the_through_via_entirely() -> None:
    """A blocked OUTER layer must drop vias, not just via LANDINGS.

    The lattice emits every layer change as a through via spanning the whole
    stack (``_emit``), so the via's annular ring is real copper on BOTH outer
    layers regardless of which layers the path uses.  If an outer layer is
    hard-avoided, no via this engine can emit is legal -- the search must
    route planar or decline.  Without this the engine would honour the
    constraint in its own state space while its emission broke it, and the
    post-route layer-intent gate would fail the run on copper the router had
    just decided was legal.
    """
    pads = [_pad(5.0, 15.0, 1, ref="A"), _pad(35.0, 15.0, 1, ref="B")]
    rules = DesignRules(strict_layers=True)
    # Only B.Cu (the through via's far endpoint) is off-limits; the inner
    # layers are free, so a naive "never LAND on a blocked layer" rule would
    # happily dip F -> In1 -> F and emit an F<->B through via anyway.
    net_class = NetClassRouting(
        name="HV",
        trace_width=0.6,
        clearance=0.2,
        preferred_layers=[_F_CU, _IN1_CU, _IN2_CU],
        avoid_layers=[_B_CU],
    )
    f_only_wall = [
        Segment(
            x1=20.0, y1=-5.0, x2=20.0, y2=35.0, width=2.0, layer=Layer.F_CU, net=2, net_name="N2"
        )
    ]
    fixed = [Route(net=2, net_name="N2", segments=f_only_wall)]
    conns = [((1, 0), pads[0], pads[1], net_class)]

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=_stack())
    routes, stats = pf.route_netset(conns, fixed_copper=fixed, max_iterations=8)

    assert stats.routed == 0, (
        "a via was emitted whose annulus lands on the hard-avoided B.Cu: "
        f"{[v.layers for r in routes.values() for v in r.vias]}"
    )
    assert "layer-constrained" in pf.failure_reasons.get((1, 0), "")


def test_hard_avoided_layers_resolves_avoid_layers_under_strict_flag() -> None:
    """Unit-level pin on the new accessor itself (mirrors ``_conn_geometry``)."""
    rules_soft = DesignRules(strict_layers=False)
    rules_strict = DesignRules(strict_layers=True)
    net_class = _pgnd_class(hard=True)
    pads = [_pad(5.0, 15.0, 1, ref="A"), _pad(35.0, 15.0, 1, ref="B")]

    pf_soft = LatticePathfinder(_OUTLINE, pads, rules_soft, layer_stack=_stack())
    assert pf_soft._hard_avoided_layers(net_class) == frozenset()  # soft default preserved

    pf_strict = LatticePathfinder(_OUTLINE, pads, rules_strict, layer_stack=_stack())
    assert pf_strict._hard_avoided_layers(net_class) == frozenset({_IN1_CU, _IN2_CU})
    assert pf_strict._hard_avoided_layers(None) == frozenset()
