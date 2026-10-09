"""Issue #5398: bounded inward off-pad access for trapped Kelvin sense terminals.

Independent four-terminal Kelvin fixture (not board 05): a sense pin ``U71.1``
(net ``VSNS``) sits between two drive pins on a dense fine-pitch package whose
outward channel is already taken by the neighbours' escape vias.  ``VSNS`` is a
real Kelvin net -- shunt ``R82`` plus force/load terminals ``Q91`` / ``U94`` --
so :func:`detect_kelvin_topology` recognises it.

Covered: the recovery itself (an ordinary manufacturable via on the inward
side, topology unchanged), and adversarial controls -- foreign copper (track,
via, pad, inner-layer track beyond the landing, board edge / cut-out, unknown
holes, drill-to-drill spacing on its own), a shared force path (committed
and sibling same-net copper, another terminal's SMD or through-hole land), an
ineligible via, degenerate layer transitions, a non-Kelvin net, and the
sibling rip-up protection of a committed recovery.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.escape import (
    EscapeDirection,
    EscapeRoute,
    EscapeRouter,
    PackageInfo,
    PackageType,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.kelvin import detect_kelvin_topology
from kicad_tools.router.kelvin_escape import (
    kelvin_access_candidate_clear,
    kelvin_net_ids,
    recover_kelvin_escapes,
)
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def fixture(pad_height: float = 1.55, layer_stack=None, sense_name: str = "VSNS"):
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_clearance=0.2,
        via_diameter=0.6,
        via_drill=0.3,
        grid_resolution=0.05,
    )
    grid = RoutingGrid(width=20, height=20, rules=rules, layer_stack=layer_stack)
    router = EscapeRouter(grid, rules, component_holes=())
    pads = [
        Pad(
            x=10 + dx,
            y=10,
            width=0.3,
            height=pad_height,
            layer=Layer.F_CU,
            net=net,
            net_name=name,
            ref="U71",
            pin=str(net),
        )
        for dx, net, name in [(0, 1, sense_name), (-0.5, 2, "DRIVE_L"), (0.5, 3, "DRIVE_R")]
    ]
    terminals = [
        Pad(
            x=x,
            y=15,
            width=0.5,
            height=0.5,
            layer=Layer.F_CU,
            net=1,
            net_name=sense_name,
            ref=ref,
            pin="1",
        )
        for x, ref in [(3, "R82"), (6, "Q91"), (15, "U94")]
    ]
    for pad in pads + terminals:
        grid.add_pad(pad)
    package = PackageInfo(
        ref="U71",
        package_type=PackageType.QFP,
        center=(10, 9),
        pads=pads,
        pin_count=3,
        pin_pitch=0.5,
        bounding_box=(9.35, 9.225, 10.65, 10.775),
        is_dense=True,
    )
    # The sense pin's escape never left its land (trapped on F.Cu).
    surface = EscapeRoute(
        pad=pads[0],
        direction=EscapeDirection.NORTH,
        escape_point=(10, 10.7),
        escape_layer=Layer.F_CU,
        segments=[Segment(x1=10, y1=10, x2=10, y2=10.7, width=0.2, layer=Layer.F_CU, net=1)],
    )
    # The neighbours' outward escape vias fill the outward channel.
    siblings = [
        EscapeRoute(
            pad=p,
            direction=EscapeDirection.NORTH,
            escape_point=(p.x, 11.95),
            escape_layer=Layer.B_CU,
            via_pos=(p.x, 11.25),
            via=Via(
                x=p.x, y=11.25, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=p.net
            ),
            segments=[],
        )
        for p in pads[1:]
    ]
    return router, package, [surface, *siblings], pads + terminals


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def test_fixture_is_a_real_kelvin_net():
    _, _, _, pads = fixture()
    net_pads = [p for p in pads if p.net == 1]
    topology = detect_kelvin_topology(net_pads)
    assert topology is not None
    assert net_pads[topology.root_index].ref == "R82"
    assert kelvin_net_ids(pads) == frozenset({1})


def test_recovers_inward_without_changing_kelvin_star():
    router, package, escapes, pads = fixture()
    topology = detect_kelvin_topology([p for p in pads if p.net == 1])
    result = recover_kelvin_escapes(router, package, escapes, pads)
    recovered = result[0]
    assert recovered is not escapes[0]
    assert recovered.via is not None
    assert recovered.direction == EscapeDirection.SOUTH
    # Inward: under the package body, off the land.
    assert recovered.via.y < 9.225
    # The ordinary manufacturer via of the existing lateral rescue -- never a
    # microvia and never via-in-pad.
    assert recovered.via.diameter == pytest.approx(0.6)
    assert recovered.via.drill == pytest.approx(0.3)
    assert not recovered.via.in_pad
    assert not recovered.via.is_micro
    assert recovered.escape_layer != Layer.F_CU
    # Siblings untouched; Kelvin star (shunt root) unchanged.
    assert result[1:] == escapes[1:]
    assert detect_kelvin_topology([p for p in pads if p.net == 1]) == topology
    # The recovered copper itself passes the full predicate.
    assert kelvin_access_candidate_clear(router, recovered, result, pads, 0.2, replaced=escapes[0])


def test_outward_legal_access_is_left_to_the_general_router():
    """Only a genuinely trapped terminal is touched."""
    router, package, escapes, pads = fixture()
    for sibling in escapes[1:]:
        sibling.via = None
        sibling.via_pos = None
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert result[0] is escapes[0]


def test_illegal_outward_candidate_does_not_hide_legal_inward_access():
    router, package, escapes, pads = fixture()
    for sibling in escapes[1:]:
        sibling.via = None
        sibling.via_pos = None
    router.board_bounds = (0, 0, 20, 11.1)
    router.edge_clearance = 0.2
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert result[0].via is not None
    assert result[0].direction == EscapeDirection.SOUTH


def test_bounded_search_continues_after_full_clearance_rejects_first_candidate():
    router, package, escapes, pads = fixture(pad_height=1.6)
    # The first inward via (y=8.70) has 0.199 mm to this track; the next
    # allowed offset (y=8.65) clears it -- within the unchanged budget.
    escapes[1].segments.append(
        Segment(x1=10.599, y1=8.7, x2=11, y2=8.7, width=0.2, layer=Layer.F_CU, net=2)
    )
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert result[0].via is not None
    assert result[0].via.y == pytest.approx(8.65)


# ---------------------------------------------------------------------------
# Adversarial: foreign copper, edges, holes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocker", ["sibling_track", "sibling_via", "committed_track", "pad", "edge", "unknown_holes"]
)
def test_rejects_inward_physical_conflicts(blocker):
    router, package, escapes, pads = fixture()
    if blocker == "sibling_track":
        escapes[1].segments.append(
            Segment(x1=9, y1=8.7, x2=11, y2=8.7, width=0.2, layer=Layer.B_CU, net=2)
        )
    elif blocker == "sibling_via":
        escapes.append(
            EscapeRoute(
                pad=package.pads[1],
                direction=EscapeDirection.SOUTH,
                escape_point=(10, 8.7),
                escape_layer=Layer.B_CU,
                via=Via(
                    x=10, y=8.7, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=2
                ),
            )
        )
    elif blocker == "committed_track":
        router.grid.mark_route(
            Route(
                net=3,
                net_name="DRIVE_R",
                segments=[Segment(x1=8, y1=8.4, x2=12, y2=8.4, width=0.2, layer=Layer.B_CU, net=3)],
            )
        )
        # Make it a wall across every inward offset of the budget.
        router.grid.mark_route(
            Route(
                net=3,
                net_name="DRIVE_R",
                segments=[Segment(x1=8, y1=8.9, x2=12, y2=8.9, width=0.2, layer=Layer.B_CU, net=3)],
            )
        )
    elif blocker == "pad":
        pad = Pad(
            x=10,
            y=8.7,
            width=0.5,
            height=0.5,
            layer=Layer.F_CU,
            net=7,
            net_name="FOREIGN",
            ref="C97",
            pin="1",
        )
        pads.append(pad)
        router.grid.add_pad(pad)
    elif blocker == "edge":
        router.board_bounds = (0, 9, 20, 20)
        router.edge_clearance = 0.2
    else:
        router._component_holes = None
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert result[0] is escapes[0]


def test_rejects_inward_outline_cutout():
    router, package, escapes, pads = fixture()
    result = recover_kelvin_escapes(
        router, package, escapes, pads, edge_segments=[((9, 8.7), (11, 8.7))], edge_clearance=0.2
    )
    assert result[0] is escapes[0]


def test_inner_landing_rejects_foreign_copper_beyond_logical_transition():
    """The barrel is drilled through the board: copper on In2.Cu blocks it too."""
    router, package, escapes, pads = fixture(layer_stack=LayerStack.four_layer_all_signal())
    router.grid.routes.append(
        Route(99, "FOREIGN", segments=[Segment(5, 8.7, 15, 8.7, 2, Layer.IN2_CU, 99)])
    )
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert result[0] is escapes[0]


def test_inner_landing_is_used_on_a_four_layer_stack():
    router, package, escapes, pads = fixture(layer_stack=LayerStack.four_layer_all_signal())
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert result[0].via is not None
    assert result[0].escape_layer == Layer.IN1_CU


def test_inner_landing_keeps_ordinary_barrel_full_stack():
    """An ordinary escape via declares its drilled span, not its landing pair.

    Before the fix the lateral escape via was ``(F.Cu, In1.Cu)``; KiCad builds
    it as a through via, but every span-aware validator skipped In2.Cu/B.Cu,
    so a later foreign trace could be committed through the barrel (the
    native ``shorting_items`` seen on board 05).
    """
    router, package, escapes, pads = fixture(layer_stack=LayerStack.four_layer_all_signal())
    candidate = recover_kelvin_escapes(router, package, escapes, pads)[0]
    via = candidate.via
    assert via is not None
    assert candidate.escape_layer == Layer.IN1_CU
    assert via.layers == (Layer.F_CU, Layer.B_CU)
    assert any(s.layer == Layer.IN1_CU for s in candidate.segments)
    foreign = Segment(via.x - 1, via.y, via.x + 1, via.y, 0.2, Layer.IN2_CU, 99)
    router.grid.mark_route(Route(99, "FOREIGN", segments=[foreign]))
    assert not router.grid.validate_via_clearance(via, via.net)[0]
    assert '(layers "F.Cu" "B.Cu")' in via.to_sexp()


def _small_ring_via(x: float, y: float, net: int) -> Via:
    """A via whose copper ring is thin (annular 0.075 mm) around a 0.3 mm drill.

    Against the recovered 0.6/0.3 mm via, a centre distance ``d`` gives a
    copper gap of ``d - 0.525`` but a hole edge gap of ``d - 0.3``.  So for
    ``0.725 <= d < 0.8`` copper (and Kelvin branch isolation) clears the
    0.2 mm requirement while the holes violate the 0.5 mm ``min_hole_to_hole``
    -- only the drill-to-drill check can reject it.
    """
    return Via(x=x, y=y, diameter=0.45, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=net)


def _sibling_via_escape(pad: Pad, via: Via) -> EscapeRoute:
    return EscapeRoute(
        pad=pad,
        direction=EscapeDirection.SOUTH,
        escape_point=(via.x, via.y),
        escape_layer=Layer.B_CU,
        via=via,
    )


@pytest.mark.parametrize("same_net", [False, True], ids=["foreign_net", "same_net"])
def test_drill_spacing_is_enforced_when_copper_clears(same_net):
    """Pins the hole-to-hole check on its own: copper is legal, holes are not.

    Same-net copper may merge with ordinary vias, but holes never may -- and
    here the same-net neighbour also clears branch isolation (copper gap
    >= clearance), so the only thing standing between this candidate and a
    drill-spacing violation is ``hole_gap``.
    """
    from kicad_tools.router.clearance_shapes import copper_gap, hole_gap, via_shape

    router, package, escapes, pads = fixture()
    good = _recovered(router, package, escapes, pads)
    via = good.via
    assert via is not None and (via.x, via.y) == pytest.approx((10.0, 8.7))
    owner = pads[0] if same_net else package.pads[1]
    drill_gap = max(router.rules.min_drill_clearance, router.rules.min_hole_to_hole)

    near = _small_ring_via(via.x + 0.76, via.y, owner.net)
    assert copper_gap(via_shape(via), via_shape(near)) >= 0.2
    assert hole_gap(via_shape(via), via_shape(near)) < drill_gap
    blocked = [*escapes, _sibling_via_escape(owner, near)]
    assert not kelvin_access_candidate_clear(router, good, blocked, pads, 0.2, replaced=escapes[0])

    # Control: the same neighbour with legal hole spacing is accepted, so
    # the rejection above is the drill check and nothing else.
    far = _small_ring_via(via.x + 0.81, via.y, owner.net)
    assert hole_gap(via_shape(via), via_shape(far)) >= drill_gap
    clear = [*escapes, _sibling_via_escape(owner, far)]
    assert kelvin_access_candidate_clear(router, good, clear, pads, 0.2, replaced=escapes[0])


def test_same_net_overlapping_sibling_via_is_refused():
    """A same-net sibling via on the inward site is refused (it fails branch
    isolation as well as drill spacing; the drill check alone is pinned by
    :func:`test_drill_spacing_is_enforced_when_copper_clears`)."""
    router, package, escapes, pads = fixture()
    escapes.append(
        EscapeRoute(
            pad=pads[0],
            direction=EscapeDirection.SOUTH,
            escape_point=(10.1, 8.7),
            escape_layer=Layer.B_CU,
            via=Via(x=10.1, y=8.7, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=1),
        )
    )
    assert recover_kelvin_escapes(router, package, escapes, pads)[0] is escapes[0]


# ---------------------------------------------------------------------------
# Adversarial: Kelvin branch isolation (shared force path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location", ["committed", "sibling"])
def test_kelvin_access_cannot_merge_into_existing_force_branch(location):
    """A same-net force-path track on the inward side must not become the tap."""
    router, package, escapes, pads = fixture()
    force = Segment(x1=9, y1=9.0, x2=11, y2=9.0, width=0.2, layer=Layer.B_CU, net=1)
    if location == "committed":
        router.grid.mark_route(Route(net=1, net_name="VSNS", segments=[force]))
    else:
        escapes.append(
            EscapeRoute(
                pad=pads[-1],
                direction=EscapeDirection.SOUTH,
                escape_point=(11, 9.0),
                escape_layer=Layer.B_CU,
                segments=[force],
            )
        )
    assert recover_kelvin_escapes(router, package, escapes, pads)[0] is escapes[0]


def _same_net_terminal(x: float, y: float, *, through_hole: bool, size: float = 0.5) -> Pad:
    """Another terminal of the Kelvin net ``VSNS`` -- a force-side part, not
    the shunt (``detect_kelvin_topology`` still roots at ``R82``)."""
    return Pad(
        x=x,
        y=y,
        width=size,
        height=size,
        layer=Layer.F_CU,
        net=1,
        net_name="VSNS",
        ref="J5",
        pin="1",
        through_hole=through_hole,
        drill=0.3 if through_hole else 0.0,
    )


@pytest.mark.parametrize(
    "dx",
    [1.0, 1.05, 1.1, 1.2],
    ids=["overlap", "touching", "gap_0.05", "gap_0.15"],
)
def test_kelvin_access_cannot_land_on_same_net_through_hole_terminal(dx):
    """Regression (PR #6271 review): a same-net THT force terminal beside the
    inward site.  The old own-net through-hole exemption let the via touch it
    (dx=1.05 -> copper gap 0.0) or sit 0.05 / 0.15 mm from it (dx=1.1 / 1.2),
    merging the sense branch into the force terminal before the shunt."""
    from kicad_tools.router.clearance_shapes import copper_gap, pad_shape, via_shape

    router, package, escapes, pads = fixture()
    terminal = _same_net_terminal(10 + dx, 8.7, through_hole=True, size=1.5)
    pads.append(terminal)
    router.grid.add_pad(terminal)
    net_pads = [p for p in pads if p.net == 1]
    topology = detect_kelvin_topology(net_pads)
    assert topology is not None and net_pads[topology.root_index].ref == "R82"
    assert kelvin_net_ids(pads) == frozenset({1})

    result = recover_kelvin_escapes(router, package, escapes, pads)[0]
    if result.via is not None:  # any recovery must keep the branch isolated
        assert copper_gap(via_shape(result.via), pad_shape(terminal)) >= 0.2 - 1e-6
    assert result is escapes[0]


@pytest.mark.parametrize("through_hole", [False, True], ids=["smd", "tht"])
def test_predicate_rejects_via_next_to_another_same_net_terminal(through_hole):
    """Another terminal's land of the same net (SMD or THT) within clearance of
    the via is a branch merge -- rejected; moved just clear, accepted.

    The through-hole land is 1.5 mm so its drill stays >= ``min_hole_to_hole``
    from the via in both positions: only copper isolation can reject it.
    """
    from kicad_tools.router.clearance_shapes import copper_gap, pad_shape, via_shape

    router, package, escapes, pads = fixture()
    good = _recovered(router, package, escapes, pads)
    via = good.via
    assert via is not None
    size = 1.5 if through_hole else 0.5
    near_dx, far_dx = (1.1, 1.3) if through_hole else (0.6, 0.8)
    near = _same_net_terminal(via.x + near_dx, via.y, through_hole=through_hole, size=size)
    assert copper_gap(via_shape(via), pad_shape(near)) < 0.2
    assert not kelvin_access_candidate_clear(
        router, good, escapes, [*pads, near], 0.2, replaced=escapes[0]
    )
    far = _same_net_terminal(via.x + far_dx, via.y, through_hole=through_hole, size=size)
    assert copper_gap(via_shape(via), pad_shape(far)) >= 0.2
    assert kelvin_access_candidate_clear(
        router, good, escapes, [*pads, far], 0.2, replaced=escapes[0]
    )


def test_kelvin_access_cannot_land_beside_same_net_smd_terminal():
    """End-to-end: a same-net SMD terminal spanning the whole inward budget."""
    router, package, escapes, pads = fixture()
    terminal = Pad(
        x=10.6,
        y=8.0,
        width=0.5,
        height=2.0,
        layer=Layer.F_CU,
        net=1,
        net_name="VSNS",
        ref="J5",
        pin="1",
    )
    pads.append(terminal)
    router.grid.add_pad(terminal)
    assert kelvin_net_ids(pads) == frozenset({1})
    assert recover_kelvin_escapes(router, package, escapes, pads)[0] is escapes[0]


def test_recovered_via_clears_its_own_land():
    """The trapped pin's own SMT pad gets no exemption: the via is off-pad."""
    from kicad_tools.router.clearance_shapes import copper_gap, pad_shape, via_shape

    router, package, escapes, pads = fixture()
    good = _recovered(router, package, escapes, pads)
    assert good.via is not None
    assert copper_gap(via_shape(good.via), pad_shape(good.pad)) >= 0.2 - 1e-6


def test_same_net_merge_is_what_the_ordinary_validators_would_accept():
    """Control: the isolation rule is not vacuous -- plain clearance allows it."""
    router, package, escapes, pads = fixture()
    force = Segment(x1=9, y1=9.0, x2=11, y2=9.0, width=0.2, layer=Layer.B_CU, net=1)
    router.grid.mark_route(Route(net=1, net_name="VSNS", segments=[force]))
    via = Via(x=10, y=8.7, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=1)
    assert router.grid.validate_via_clearance(via, 1, 0.2)[0]


# ---------------------------------------------------------------------------
# Adversarial: process / layer eligibility
# ---------------------------------------------------------------------------


def _recovered(router, package, escapes, pads) -> EscapeRoute:
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert result[0].via is not None
    return result[0]


@pytest.mark.parametrize("defect", ["micro", "in_pad", "drill", "annular"])
def test_ineligible_via_is_never_selected(defect):
    from kicad_tools.router.mfr_limits import MfrLimits

    router, package, escapes, pads = fixture()
    good = _recovered(router, package, escapes, pads)
    via = good.via
    assert via is not None
    router._mfr_limits = MfrLimits(
        name="test", min_trace=0.1, min_clearance=0.1, min_via_drill=0.3, min_via_annular=0.15
    )
    assert kelvin_access_candidate_clear(router, good, escapes, pads, 0.2, replaced=escapes[0])
    if defect == "micro":
        via.is_micro = True
    elif defect == "in_pad":
        via.in_pad = True
    elif defect == "drill":
        via.drill = 0.25
        via.diameter = 0.55
    else:
        via.diameter = 0.5
    assert not kelvin_access_candidate_clear(router, good, escapes, pads, 0.2, replaced=escapes[0])


def test_bottom_source_rejects_degenerate_same_layer_via():
    old, package, escapes, pads = fixture()
    for pad in pads:
        pad.layer = Layer.B_CU
    for escape in escapes:
        for segment in escape.segments:
            segment.layer = Layer.B_CU
    escapes[0].escape_layer = Layer.B_CU
    grid = RoutingGrid(width=20, height=20, rules=old.rules)
    for pad in pads:
        grid.add_pad(pad)
    router = EscapeRouter(grid, old.rules, component_holes=())
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert result[0] is escapes[0]


# ---------------------------------------------------------------------------
# Ordinary nets are untouched
# ---------------------------------------------------------------------------


def test_name_without_shunt_does_not_activate_recovery():
    router, package, escapes, pads = fixture()
    pads = [p for p in pads if p.ref != "R82"]
    assert recover_kelvin_escapes(router, package, escapes, pads) == escapes


def test_non_sense_net_is_untouched():
    router, package, escapes, pads = fixture(sense_name="DATA0")
    assert kelvin_net_ids(pads) == frozenset()
    result = recover_kelvin_escapes(router, package, escapes, pads)
    assert all(a is b for a, b in zip(result, escapes, strict=True))


# ---------------------------------------------------------------------------
# Public prephase + sibling rip-up protection
# ---------------------------------------------------------------------------


def _autorouter(monkeypatch):
    from kicad_tools.router.core import Autorouter

    escape_router, package, escapes, pads = fixture()
    router = Autorouter(20, 20, rules=escape_router.rules, force_python=True)
    router.grid = escape_router.grid
    router._escape_router = escape_router
    router.pads = {pad.key: pad for pad in pads}
    router.all_pads = list(pads)
    monkeypatch.setattr(escape_router, "generate_escapes", lambda package: list(escapes))
    return router, package, pads


def test_public_prephase_commits_and_protects_the_recovery(monkeypatch):
    router, package, pads = _autorouter(monkeypatch)
    routes = router.generate_escape_routes([package])
    assert any(via.net == 1 for route in routes for via in route.vias)
    assert router._escape_pad_overrides[pads[0].key].layer != Layer.F_CU
    assert router._kelvin_access_protected_nets == {1}
    # Ordinary nets gain no protection.
    assert 2 not in router._kelvin_access_protected_nets
    assert 3 not in router._kelvin_access_protected_nets


def test_sibling_ripup_never_displaces_a_recovered_kelvin_net(monkeypatch):
    """A higher-priority BLOCKED_BY_COMPONENT failure on U71 tries to displace
    its siblings; the recovered Kelvin net must not be among the candidates."""
    from kicad_tools.router.failure_analysis import FailureCause

    router, package, pads = _autorouter(monkeypatch)
    router.generate_escape_routes([package])

    captured: list[set[int]] = []

    def fake_find(*, failed_net, blocking_components, candidate_nets):
        captured.append(set(candidate_nets))
        return set()

    monkeypatch.setattr(router, "_find_lower_priority_siblings_on_components", fake_find)

    class _Failure:
        net = 2
        failure_cause = FailureCause.BLOCKED_PATH
        blocking_components = ["U71"]

    router.routing_failures = [_Failure()]
    router._attempt_blocked_component_ripup(2)
    assert captured and 1 not in captured[-1]

    # Negotiated variant: the loop's own net_routes still carry net 1.
    net_routes = {1: [r for r in router.routes if r.net == 1], 3: [Route(3, "DRIVE_R")]}
    router._attempt_blocked_component_ripup_negotiated(
        2,
        neg_router=None,  # type: ignore[arg-type]
        net_routes=net_routes,
        pads_by_net={},
        ripup_history={},
        present_cost_factor=1.0,
        max_ripups_per_net=3,
    )
    assert 1 not in captured[-1]
    assert 3 in captured[-1]
