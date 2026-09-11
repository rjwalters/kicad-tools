"""Post-route hard layer-intent audit (issue #4979).

``NetClassRouting.avoid_layers`` becomes a HARD no-go set under
``--strict-layers`` (or a declared ``target_ampacity``).  The lattice engine
ignored it entirely until #4979 and shipped 2.6 mm ``/PGND`` copper onto a
forbidden ``In2.Cu`` reference plane with no hard failure reported anywhere.

The search-side fix lives in ``router/lattice/pathfinder.py`` (pinned by
``tests/router/lattice/test_hard_avoid_layers_4979.py``); this module pins the
independent POST-ROUTE gate that checks the copper the board actually
carries, plus the matrix-preference composition rule that keeps a soft
optimisation hint from ever pointing a net at a hard-blocked layer.
"""

from __future__ import annotations

from kicad_tools.router.layer_intent import (
    find_layer_intent_violations,
    forbidden_layer_names,
)
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import NetClassRouting

_F_CU, _IN1_CU, _IN2_CU, _B_CU = 0, 1, 2, 3


def _hv_class(**overrides) -> NetClassRouting:
    """The softstart rev-C ``HV`` class from the #4979 report."""
    kwargs = {
        "name": "HV",
        "trace_width": 2.6,
        "clearance": 0.4,
        "preferred_layers": [_F_CU, _B_CU],
        "avoid_layers": [_IN1_CU, _IN2_CU],
    }
    kwargs.update(overrides)
    return NetClassRouting(**kwargs)


def _seg(layer: Layer, *, net: int = 1, name: str = "/PGND", width: float = 2.6) -> Segment:
    return Segment(x1=0.0, y1=0.0, x2=5.0, y2=0.0, width=width, layer=layer, net=net, net_name=name)


def _via(layers: tuple[Layer, Layer], *, net: int = 1, name: str = "/PGND") -> Via:
    return Via(x=2.0, y=0.0, drill=0.3, diameter=0.6, layers=layers, net=net, net_name=name)


def _route(*, segments=(), vias=(), net: int = 1, name: str = "/PGND") -> Route:
    return Route(net=net, net_name=name, segments=list(segments), vias=list(vias))


def _stack() -> LayerStack:
    return LayerStack.four_layer_sig_gnd_pwr_sig()


# ---------------------------------------------------------------------------
# forbidden_layer_names
# ---------------------------------------------------------------------------


def test_forbidden_names_resolve_only_under_strict_layers() -> None:
    net_class_map = {"/PGND": _hv_class()}
    stack = _stack()

    assert forbidden_layer_names(net_class_map, strict_layers=False, layer_stack=stack) == {}
    assert forbidden_layer_names(net_class_map, strict_layers=True, layer_stack=stack) == {
        "/PGND": frozenset({"In1.Cu", "In2.Cu"})
    }


def test_ampacity_bearing_class_is_hard_without_the_flag() -> None:
    """A declared ``target_ampacity`` hardens ``avoid_layers`` on its own."""
    net_class_map = {"/PGND": _hv_class(target_ampacity=25.0)}
    assert forbidden_layer_names(net_class_map, strict_layers=False, layer_stack=_stack()) == {
        "/PGND": frozenset({"In1.Cu", "In2.Cu"})
    }


def test_exclusions_are_stack_positions_and_out_of_range_ones_are_dropped() -> None:
    """``avoid_layers`` entries are grid-layer INDICES, resolved per stack.

    Deliberately the same reading the search uses (both grid backends filter
    these indices out of the net's routable set), so the gate can never
    disagree with the engine about which physical layer an exclusion names.
    On a 2-layer stack index 1 is ``B.Cu`` and index 2 does not exist at all --
    the latter is dropped rather than invented (the vacuous-exclusion
    reporting question is issue #4685's, not this gate's).
    """
    net_class_map = {"/PGND": _hv_class()}
    assert forbidden_layer_names(
        net_class_map, strict_layers=True, layer_stack=LayerStack.two_layer()
    ) == {"/PGND": frozenset({"B.Cu"})}


# ---------------------------------------------------------------------------
# find_layer_intent_violations
# ---------------------------------------------------------------------------


def test_no_hard_intent_is_a_strict_no_op() -> None:
    """Without ``--strict-layers`` the same forbidden copper is not reported."""
    routed = [_route(segments=[_seg(Layer.IN2_CU)])]
    assert (
        find_layer_intent_violations(
            routed,
            net_class_map={"/PGND": _hv_class()},
            strict_layers=False,
            layer_stack=_stack(),
        )
        == []
    )


def test_routed_copper_on_a_forbidden_layer_is_a_new_violation() -> None:
    """The reported defect: 2.6 mm PGND copper committed onto In2.Cu."""
    routed = [_route(segments=[_seg(Layer.IN2_CU), _seg(Layer.F_CU)])]

    found = find_layer_intent_violations(
        routed,
        net_class_map={"/PGND": _hv_class()},
        strict_layers=True,
        layer_stack=_stack(),
    )

    assert len(found) == 1
    v = found[0]
    assert (v.net_name, v.layer, v.kind, v.inherited) == ("/PGND", "In2.Cu", "segment", False)
    assert v.width == 2.6
    assert "new" in v.describe()


def test_preserved_copper_is_reported_as_inherited() -> None:
    """Copper re-emitted from the input board indicts the board, not the run."""
    preserved = [_route(segments=[_seg(Layer.IN1_CU)])]

    found = find_layer_intent_violations(
        [],
        preserved,
        net_class_map={"/PGND": _hv_class()},
        strict_layers=True,
        layer_stack=_stack(),
    )

    assert [(v.layer, v.inherited) for v in found] == [("In1.Cu", True)]


def test_new_violations_sort_before_inherited_ones() -> None:
    routed = [_route(segments=[_seg(Layer.IN2_CU)])]
    preserved = [_route(segments=[_seg(Layer.IN1_CU)])]

    found = find_layer_intent_violations(
        routed,
        preserved,
        net_class_map={"/PGND": _hv_class()},
        strict_layers=True,
        layer_stack=_stack(),
    )

    assert [v.inherited for v in found] == [False, True]


def test_through_via_endpoints_are_judged_not_the_barrel() -> None:
    """An F->B through via is legal for an F/B-only net; a via LANDING on a
    forbidden layer is not.

    Judging the barrel instead would forbid every layer change an F/B-only net
    could ever make on a 4-layer board -- keeping a plane clear of a via barrel
    is an antipad/clearance concern DRC owns.
    """
    through = [_route(vias=[_via((Layer.F_CU, Layer.B_CU))])]
    buried = [_route(vias=[_via((Layer.F_CU, Layer.IN2_CU))])]
    kwargs = {
        "net_class_map": {"/PGND": _hv_class()},
        "strict_layers": True,
        "layer_stack": _stack(),
    }

    assert find_layer_intent_violations(through, **kwargs) == []
    found = find_layer_intent_violations(buried, **kwargs)
    assert [(v.kind, v.layer) for v in found] == [("via", "In2.Cu")]


def test_unconstrained_nets_are_untouched() -> None:
    """A net with no hard-avoided layers is never reported, on any layer."""
    routed = [_route(segments=[_seg(Layer.IN2_CU, net=2, name="/SIG_A")], net=2, name="/SIG_A")]

    assert (
        find_layer_intent_violations(
            routed,
            net_class_map={"/PGND": _hv_class()},
            strict_layers=True,
            layer_stack=_stack(),
        )
        == []
    )


def test_id_to_name_wins_over_a_stale_route_net_name() -> None:
    """A preserved route keyed by a stale id/name is still audited correctly.

    Same preference order ``find_pairwise_violations`` uses -- without it, a
    preserved route whose stored net name disagrees with this session's
    numbering would silently escape the gate.
    """
    preserved = [_route(segments=[_seg(Layer.IN2_CU)], net=7, name="STALE")]

    found = find_layer_intent_violations(
        [],
        preserved,
        net_class_map={"/PGND": _hv_class()},
        strict_layers=True,
        layer_stack=_stack(),
        id_to_name={7: "/PGND"},
    )

    assert [(v.net_name, v.layer, v.inherited) for v in found] == [("/PGND", "In2.Cu", True)]


# ---------------------------------------------------------------------------
# Matrix layer preferences compose with -- never override -- hard constraints
# ---------------------------------------------------------------------------


def _autorouter_with_net(net_name: str, net_class: NetClassRouting | None, *, strict: bool):
    from kicad_tools.router.core import Autorouter

    ar = Autorouter(width=50, height=50)
    ar.nets = {1: []}
    ar.net_names = {1: net_name}
    ar.rules.strict_layers = strict
    if net_class is not None:
        ar.net_class_map[net_name] = net_class
    return ar


def test_matrix_preference_never_selects_a_hard_avoided_layer() -> None:
    """A matrix assignment onto a forbidden layer is filtered, not applied."""
    ar = _autorouter_with_net("/PGND", _hv_class(), strict=True)

    ar._inject_matrix_layer_preferences({1: [_IN2_CU, _B_CU]})

    nc = ar.net_class_map["/PGND"]
    assert nc.preferred_layers == [_B_CU]
    assert nc.avoid_layers == [_IN1_CU, _IN2_CU]  # hard constraint survives


def test_matrix_preference_of_only_forbidden_layers_leaves_the_class_authored() -> None:
    """Nothing legal to prefer -> the user's class is left exactly as written."""
    authored = _hv_class()
    ar = _autorouter_with_net("/PGND", authored, strict=True)

    ar._inject_matrix_layer_preferences({1: [_IN1_CU, _IN2_CU]})

    assert ar.net_class_map["/PGND"] is authored


def test_matrix_preference_unchanged_without_a_hard_constraint() -> None:
    """Soft ``avoid_layers`` (no ``--strict-layers``, no ampacity) is untouched."""
    ar = _autorouter_with_net("/PGND", _hv_class(), strict=False)

    ar._inject_matrix_layer_preferences({1: [_IN2_CU]})

    assert ar.net_class_map["/PGND"].preferred_layers == [_IN2_CU]
