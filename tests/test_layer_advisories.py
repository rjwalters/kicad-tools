"""Unit tests for route-time layer-selection advisories (Issue #4314, #5014).

Exercises :mod:`kicad_tools.router.layer_advisories`, the warn-floor guards
that surface silent ``kct route`` footguns:

* **Tier 1** -- ``--layers auto`` picks a signal-on-inner stack while the
  net-class-map declares ``is_pour_net`` classes (``detect_layer_stack``
  never sees that intent).
* **Tier 2** -- a ``target_ampacity`` net whose required internal-copper
  width is unroutable on an inner layer, predicting the post-route ampacity
  DRC failure at route time.
* **Tier 3** (Issue #5014) -- a resolved stack declares ``PLANE`` layers
  (e.g. ``--layers 4``) but ``--reserve-plane-layers`` was not passed, so
  ``LayerDefinition.is_routable`` leaves those reference planes fully
  signal-eligible.

The Tier-2 required-width number is asserted to equal the post-route
ampacity DRC's number (both call ``width_for_current`` with the identical
``inner_copper_oz`` / ``layer="internal"`` shape), and the drift-prevention
contract (no warnings when the map declares neither pour nets nor
``target_ampacity``) is pinned so the guards stay silent on ordinary boards.
"""

from __future__ import annotations

from kicad_tools.core.types import CopperLayer
from kicad_tools.manufacturers import get_profile
from kicad_tools.router.layer_advisories import (
    AmpacityLayerConflict,
    PlaneLayerSignalViolation,
    ampacity_inner_layer_conflicts,
    declared_pour_net_names,
    is_external_layer,
    non_plane_layer_names,
    plane_layer_names,
    plane_layer_reservation_advisory,
    plane_layer_signal_violations,
    pour_net_blind_auto_warning,
    reserve_plane_layers_allowed_layers,
    stack_routes_signal_on_inner,
)
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.validate.rules.ampacity import AmpacityRule

# --- Fixtures -----------------------------------------------------------


def _mfr_rules(layers: int = 4, copper_oz: float = 1.0):
    """The manufacturer DesignRules the post-route ampacity DRC resolves."""
    return get_profile("jlcpcb").get_design_rules(layers=layers, copper_oz=copper_oz)


def _pour_and_ampacity_map() -> dict[str, NetClassRouting]:
    """The reporter's net-class-map: two pour nets + a 15 A HV_HICUR class."""
    return {
        "GND": NetClassRouting(name="GND", is_pour_net=True),
        "+3.3V": NetClassRouting(name="+3.3V", is_pour_net=True),
        "HV_HICUR": NetClassRouting(name="HV_HICUR", target_ampacity=15.0, trace_width=2.6),
    }


# --- is_external_layer mirrors the DRC ----------------------------------


def test_is_external_layer_matches_drc_split():
    """External/internal classification is byte-identical to the DRC's."""
    for layer in ("F.Cu", "B.Cu"):
        assert is_external_layer(layer) is True
        assert AmpacityRule._is_external_layer(layer) is True
    for layer in ("In1.Cu", "In2.Cu", "In3.Cu"):
        assert is_external_layer(layer) is False
        assert AmpacityRule._is_external_layer(layer) is False


# --- stack_routes_signal_on_inner ---------------------------------------


def test_signal_on_inner_true_for_sig_sig_and_all_signal():
    assert stack_routes_signal_on_inner(LayerStack.four_layer_sig_sig_gnd_pwr()) is True
    assert stack_routes_signal_on_inner(LayerStack.four_layer_all_signal()) is True


def test_signal_on_inner_false_for_plane_aware_and_two_layer():
    # --layers 4 (SIG-GND-PWR-SIG): inner layers are PLANE, not signal.
    assert stack_routes_signal_on_inner(LayerStack.four_layer_sig_gnd_pwr_sig()) is False
    # 2-layer: both layers are outer, so there is no inner layer at all.
    assert stack_routes_signal_on_inner(LayerStack.two_layer()) is False


# --- declared_pour_net_names --------------------------------------------


def test_declared_pour_net_names_sorted():
    assert declared_pour_net_names(_pour_and_ampacity_map()) == ["+3.3V", "GND"]


def test_declared_pour_net_names_empty_when_no_pour_or_none():
    assert declared_pour_net_names(None) == []
    assert declared_pour_net_names({"SIG": NetClassRouting(name="SIG")}) == []


# --- Tier 1: pour-net-blind auto ----------------------------------------


def test_tier1_warning_fires_for_auto_signal_on_inner_with_pour_nets():
    msg = pour_net_blind_auto_warning(
        LayerStack.four_layer_sig_sig_gnd_pwr(), _pour_and_ampacity_map()
    )
    assert msg is not None
    assert "--layers 4" in msg
    # Names both pour nets so the operator knows which layers were meant.
    assert "GND" in msg and "+3.3V" in msg


def test_tier1_silent_when_stack_reserves_inner_layers():
    # Plane-aware stack (as --layers 4 selects) never strands plane intent.
    assert (
        pour_net_blind_auto_warning(
            LayerStack.four_layer_sig_gnd_pwr_sig(), _pour_and_ampacity_map()
        )
        is None
    )


def test_tier1_silent_when_no_pour_nets_declared():
    plain = {"SIG": NetClassRouting(name="SIG")}
    assert pour_net_blind_auto_warning(LayerStack.four_layer_sig_sig_gnd_pwr(), plain) is None


# --- Tier 2: ampacity-vs-inner-layer conflict ---------------------------


def test_tier2_conflict_fires_and_number_matches_drc():
    rules = _mfr_rules()
    stack = LayerStack.four_layer_sig_sig_gnd_pwr()
    conflicts = ampacity_inner_layer_conflicts(_pour_and_ampacity_map(), rules, stack)

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert isinstance(conflict, AmpacityLayerConflict)
    assert conflict.net_name == "HV_HICUR"
    assert conflict.current_a == 15.0

    # The route-time required internal width must equal the post-route DRC's
    # required width to the last digit (same width_for_current call shape).
    drc_required = AmpacityRule({"HV_HICUR": 15.0})._required_width_mm(15.0, rules, external=False)
    assert conflict.required_internal_width_mm == drc_required

    # The message carries the DRC-consistent number and copper weight.
    assert f"{drc_required:.3f}mm" in conflict.message
    assert f"{rules.inner_copper_oz}oz internal" in conflict.message
    assert "--layers 4" in conflict.message


def test_tier2_silent_on_plane_aware_stack():
    # With inner layers reserved for planes there is no inner signal layer to
    # strand the high-current net on -> no route-time warning (matches the
    # "--layers 4 -> no new warnings" manual acceptance).
    assert (
        ampacity_inner_layer_conflicts(
            _pour_and_ampacity_map(), _mfr_rules(), LayerStack.four_layer_sig_gnd_pwr_sig()
        )
        == []
    )


def test_tier2_skips_pour_net_with_ampacity():
    # A class that is itself a pour net is auto-skipped by the router (becomes
    # a plane fill, not routed signal), so it must not raise a false positive
    # even if it declares target_ampacity.
    ncm = {
        "GND": NetClassRouting(name="GND", is_pour_net=True, target_ampacity=15.0, trace_width=0.2),
    }
    assert (
        ampacity_inner_layer_conflicts(ncm, _mfr_rules(), LayerStack.four_layer_sig_sig_gnd_pwr())
        == []
    )


def test_tier2_silent_when_trace_width_satisfies_requirement():
    # If the class's trace_width already meets the internal requirement, the
    # router will lay conforming copper -> no conflict.
    rules = _mfr_rules()
    required = AmpacityRule({"HV_HICUR": 15.0})._required_width_mm(15.0, rules, external=False)
    ncm = {
        "HV_HICUR": NetClassRouting(
            name="HV_HICUR", target_ampacity=15.0, trace_width=required + 1.0
        ),
    }
    assert ampacity_inner_layer_conflicts(ncm, rules, LayerStack.four_layer_sig_sig_gnd_pwr()) == []


# --- Drift-prevention contract ------------------------------------------


def test_no_warnings_when_no_pour_nets_and_no_ampacity():
    """The baseline board (no pour nets, no target_ampacity) stays silent."""
    plain = {
        "SIG_A": NetClassRouting(name="SIG_A"),
        "SIG_B": NetClassRouting(name="SIG_B", trace_width=0.15),
    }
    stack = LayerStack.four_layer_sig_sig_gnd_pwr()  # even a signal-on-inner stack
    assert pour_net_blind_auto_warning(stack, plain) is None
    assert ampacity_inner_layer_conflicts(plain, _mfr_rules(), stack) == []


def test_empty_and_none_maps_are_noops():
    stack = LayerStack.four_layer_sig_sig_gnd_pwr()
    for ncm in (None, {}):
        assert pour_net_blind_auto_warning(stack, ncm) is None
        assert ampacity_inner_layer_conflicts(ncm, _mfr_rules(), stack) == []


# --- route_cmd wiring (both callsites share this helper) ----------------


def _args(**overrides):
    from argparse import Namespace

    base = {
        "manufacturer": "jlcpcb",
        "copper_oz": 1.0,
        "_loaded_net_class_map": _pour_and_ampacity_map(),
    }
    base.update(overrides)
    return Namespace(**base)


def test_route_cmd_helper_emits_both_tiers_to_stderr(capsys):
    from kicad_tools.cli.route_cmd import _warn_layer_selection_advisories

    _warn_layer_selection_advisories(_args(), LayerStack.four_layer_sig_sig_gnd_pwr(), is_auto=True)
    err = capsys.readouterr().err
    # Tier 1 (auto pour-net-blind) and Tier 2 (ampacity) both present.
    assert "--layers auto selected" in err
    assert "HV_HICUR" in err and "65.479mm" in err
    assert "--layers 4" in err


def test_route_cmd_helper_tier1_suppressed_when_not_auto(capsys):
    from kicad_tools.cli.route_cmd import _warn_layer_selection_advisories

    # Explicit --layers 4-sig: Tier 1 (auto-only) is silent, but Tier 2 still
    # fires because the stack routes signal on inner layers.
    _warn_layer_selection_advisories(
        _args(), LayerStack.four_layer_sig_sig_gnd_pwr(), is_auto=False
    )
    err = capsys.readouterr().err
    assert "--layers auto selected" not in err
    assert "HV_HICUR" in err


def test_route_cmd_helper_silent_on_plane_stack(capsys):
    from kicad_tools.cli.route_cmd import _warn_layer_selection_advisories

    # --layers 4 (plane-aware): no new warnings at all.
    _warn_layer_selection_advisories(
        _args(), LayerStack.four_layer_sig_gnd_pwr_sig(), is_auto=False
    )
    assert capsys.readouterr().err == ""


def test_route_cmd_helper_silent_for_baseline_map(capsys):
    from kicad_tools.cli.route_cmd import _warn_layer_selection_advisories

    # Drift guard: a map with no pour nets and no target_ampacity is silent
    # even under auto on a signal-on-inner stack.
    args = _args(_loaded_net_class_map={"SIG": NetClassRouting(name="SIG")})
    _warn_layer_selection_advisories(args, LayerStack.four_layer_sig_sig_gnd_pwr(), is_auto=True)
    assert capsys.readouterr().err == ""


def test_route_cmd_helper_handles_missing_net_class_map(capsys):
    from argparse import Namespace

    from kicad_tools.cli.route_cmd import _warn_layer_selection_advisories

    # No _loaded_net_class_map attribute at all -> pure no-op, no crash.
    args = Namespace(manufacturer="jlcpcb", copper_oz=1.0)
    _warn_layer_selection_advisories(args, LayerStack.four_layer_sig_sig_gnd_pwr(), is_auto=True)
    assert capsys.readouterr().err == ""


# --- Tier 3: plane-layer signal-reservation guardrail (Issue #5014) -----


def test_plane_layer_names_matches_stack_plane_layers():
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    assert plane_layer_names(stack) == ["In1.Cu", "In2.Cu"]


def test_plane_layer_names_empty_for_no_plane_stacks():
    assert plane_layer_names(LayerStack.two_layer()) == []
    assert plane_layer_names(LayerStack.four_layer_all_signal()) == []


def test_non_plane_layer_names_excludes_only_planes():
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    assert non_plane_layer_names(stack) == ["F.Cu", "B.Cu"]


def test_non_plane_layer_names_keeps_mixed_layers():
    # 4-Layer SIG-SIG-GND-PWR: B.Cu is MIXED (plane_net set but still
    # signal-eligible by design) -- it must stay in the non-plane set.
    stack = LayerStack.four_layer_sig_sig_gnd_pwr()
    names = non_plane_layer_names(stack)
    assert "In2.Cu" not in names  # the one PLANE layer
    assert names == ["F.Cu", "In1.Cu", "B.Cu"]


def test_reserve_plane_layers_allowed_layers_for_4layer_plane_stack():
    reserved = reserve_plane_layers_allowed_layers(LayerStack.four_layer_sig_gnd_pwr_sig())
    assert reserved == ["F.Cu", "B.Cu"]


def test_reserve_plane_layers_allowed_layers_for_6layer_plane_stack():
    reserved = reserve_plane_layers_allowed_layers(LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig())
    assert reserved == ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]


def test_reserve_plane_layers_allowed_layers_none_when_no_planes():
    # Drift-prevention no-op: nothing to reserve on a plane-free stack.
    assert reserve_plane_layers_allowed_layers(LayerStack.two_layer()) is None
    assert reserve_plane_layers_allowed_layers(LayerStack.four_layer_all_signal()) is None


def test_plane_layer_reservation_advisory_fires_for_plane_stack():
    msg = plane_layer_reservation_advisory(
        LayerStack.four_layer_sig_gnd_pwr_sig(), reserve_plane_layers=False
    )
    assert msg is not None
    assert "In1.Cu" in msg and "In2.Cu" in msg
    assert "--reserve-plane-layers" in msg
    assert "F.Cu" in msg and "B.Cu" in msg  # names the non-plane alternative


def test_plane_layer_reservation_advisory_silent_when_reserved():
    assert (
        plane_layer_reservation_advisory(
            LayerStack.four_layer_sig_gnd_pwr_sig(), reserve_plane_layers=True
        )
        is None
    )


def test_plane_layer_reservation_advisory_silent_when_no_planes():
    assert (
        plane_layer_reservation_advisory(LayerStack.two_layer(), reserve_plane_layers=False) is None
    )
    assert (
        plane_layer_reservation_advisory(
            LayerStack.four_layer_all_signal(), reserve_plane_layers=False
        )
        is None
    )


def _route_with_segments(net_name: str, layers: list[CopperLayer]) -> Route:
    segments = [
        Segment(x1=0.0, y1=0.0, x2=1.0, y2=0.0, width=0.2, layer=layer, net_name=net_name)
        for layer in layers
    ]
    return Route(net=1, net_name=net_name, segments=segments)


def test_plane_layer_signal_violations_detects_segments_on_plane():
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()  # In1.Cu=GND, In2.Cu=+3.3V planes
    routes = [
        _route_with_segments("CLK", [CopperLayer.F_CU, CopperLayer.IN1_CU, CopperLayer.IN1_CU]),
        _route_with_segments("DATA", [CopperLayer.F_CU, CopperLayer.B_CU]),
    ]
    violations = plane_layer_signal_violations(routes, stack)

    assert len(violations) == 1
    v = violations[0]
    assert isinstance(v, PlaneLayerSignalViolation)
    assert v.net_name == "CLK"
    assert v.layer_name == "In1.Cu"
    assert v.segment_count == 2
    assert "CLK" in v.message and "In1.Cu" in v.message and "--reserve-plane-layers" in v.message


def test_plane_layer_signal_violations_empty_when_stack_has_no_planes():
    stack = LayerStack.four_layer_all_signal()
    routes = [_route_with_segments("SIG", [CopperLayer.IN1_CU, CopperLayer.IN2_CU])]
    assert plane_layer_signal_violations(routes, stack) == []


def test_plane_layer_signal_violations_empty_when_clean():
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    routes = [_route_with_segments("CLK", [CopperLayer.F_CU, CopperLayer.B_CU])]
    assert plane_layer_signal_violations(routes, stack) == []


def test_plane_layer_signal_violations_multiple_nets_sorted():
    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    routes = [
        _route_with_segments("ZNET", [CopperLayer.IN2_CU]),
        _route_with_segments("ANET", [CopperLayer.IN1_CU]),
    ]
    violations = plane_layer_signal_violations(routes, stack)
    assert [v.net_name for v in violations] == ["ANET", "ZNET"]


# --- route_cmd wiring: --reserve-plane-layers (Issue #5014) -------------


def test_route_cmd_warn_plane_layer_reservation_fires(capsys):
    from argparse import Namespace

    from kicad_tools.cli.route_cmd import _warn_plane_layer_reservation

    _warn_plane_layer_reservation(
        Namespace(reserve_plane_layers=False), LayerStack.four_layer_sig_gnd_pwr_sig()
    )
    err = capsys.readouterr().err
    assert "--reserve-plane-layers" in err
    assert "In1.Cu" in err


def test_route_cmd_warn_plane_layer_reservation_silent_when_flag_set(capsys):
    from argparse import Namespace

    from kicad_tools.cli.route_cmd import _warn_plane_layer_reservation

    _warn_plane_layer_reservation(
        Namespace(reserve_plane_layers=True), LayerStack.four_layer_sig_gnd_pwr_sig()
    )
    assert capsys.readouterr().err == ""


def test_route_cmd_warn_plane_layer_reservation_silent_when_no_planes(capsys):
    from argparse import Namespace

    from kicad_tools.cli.route_cmd import _warn_plane_layer_reservation

    _warn_plane_layer_reservation(Namespace(reserve_plane_layers=False), LayerStack.two_layer())
    assert capsys.readouterr().err == ""


def test_route_cmd_warn_plane_layer_reservation_missing_attr_defaults_off(capsys):
    from argparse import Namespace

    from kicad_tools.cli.route_cmd import _warn_plane_layer_reservation

    # No reserve_plane_layers attribute at all -> treated as False (fires).
    _warn_plane_layer_reservation(Namespace(), LayerStack.four_layer_sig_gnd_pwr_sig())
    assert "--reserve-plane-layers" in capsys.readouterr().err


def test_route_cmd_apply_plane_layer_reservation_sets_allowed_layers():
    from argparse import Namespace

    from kicad_tools.cli.route_cmd import _apply_plane_layer_reservation
    from kicad_tools.router.rules import DesignRules

    rules = DesignRules()
    assert rules.allowed_layers is None

    _apply_plane_layer_reservation(
        rules, LayerStack.four_layer_sig_gnd_pwr_sig(), Namespace(reserve_plane_layers=True)
    )
    assert rules.allowed_layers == ["F.Cu", "B.Cu"]


def test_route_cmd_apply_plane_layer_reservation_noop_when_flag_off():
    from argparse import Namespace

    from kicad_tools.cli.route_cmd import _apply_plane_layer_reservation
    from kicad_tools.router.rules import DesignRules

    rules = DesignRules()
    _apply_plane_layer_reservation(
        rules, LayerStack.four_layer_sig_gnd_pwr_sig(), Namespace(reserve_plane_layers=False)
    )
    assert rules.allowed_layers is None


def test_route_cmd_apply_plane_layer_reservation_noop_when_no_planes():
    from argparse import Namespace

    from kicad_tools.cli.route_cmd import _apply_plane_layer_reservation
    from kicad_tools.router.rules import DesignRules

    rules = DesignRules()
    _apply_plane_layer_reservation(
        rules, LayerStack.two_layer(), Namespace(reserve_plane_layers=True)
    )
    assert rules.allowed_layers is None


def test_route_cmd_audit_plane_layer_reservation_reports_violations(capsys):
    from kicad_tools.cli.route_cmd import _audit_plane_layer_reservation

    class _FakeRouter:
        routes = [_route_with_segments("CLK", [CopperLayer.IN1_CU])]

    violations = _audit_plane_layer_reservation(
        _FakeRouter(), LayerStack.four_layer_sig_gnd_pwr_sig()
    )
    assert len(violations) == 1
    err = capsys.readouterr().err
    assert "CLK" in err and "In1.Cu" in err


def test_route_cmd_audit_plane_layer_reservation_silent_when_clean(capsys):
    from kicad_tools.cli.route_cmd import _audit_plane_layer_reservation

    class _FakeRouter:
        routes = [_route_with_segments("CLK", [CopperLayer.F_CU, CopperLayer.B_CU])]

    violations = _audit_plane_layer_reservation(
        _FakeRouter(), LayerStack.four_layer_sig_gnd_pwr_sig()
    )
    assert violations == []
    assert capsys.readouterr().err == ""


# --- End-to-end: allowed_layers derived by reserve_plane_layers_allowed_layers
# actually blocks the pathfinder (Issue #5014) ----------------------------


def test_reserve_plane_layers_allowed_layers_blocks_pathfinder_end_to_end():
    """The derived allowed_layers is not just names -- the router obeys it.

    Mirrors ``tests/test_router_core.py::TestAllowedLayersConstraint``'s
    established technique for proving a hard ``allowed_layers`` restriction
    actually constrains layer choice: through-hole pads are legally
    reachable from ANY copper layer (no SMD-side layer preference biases
    the search), so a segment landing outside the allowed set could only
    happen if the restriction were not enforced.

    Builds a real ``Autorouter`` on a 4-layer SIG-GND-PWR-SIG stack (In1.Cu
    = GND plane, In2.Cu = +3.3V plane) with ``allowed_layers`` set to
    :func:`reserve_plane_layers_allowed_layers`'s output and asserts no
    committed segment lands on either plane layer -- confirming the value
    this module computes is honoured by
    :class:`~kicad_tools.router.rules.DesignRules`'s pre-existing
    ``allowed_layers`` enforcement (Issue #715), not just correctly shaped.
    """
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.rules import DesignRules

    stack = LayerStack.four_layer_sig_gnd_pwr_sig()
    reserved = reserve_plane_layers_allowed_layers(stack)
    assert reserved == ["F.Cu", "B.Cu"]

    rules = DesignRules(allowed_layers=reserved)
    router = Autorouter(width=50.0, height=40.0, rules=rules, layer_stack=stack)
    router.add_component(
        "R1",
        [
            {
                "number": "1",
                "x": 10.0,
                "y": 20.0,
                "net": 1,
                "net_name": "NET1",
                "through_hole": True,
                "drill": 0.8,
            },
            {
                "number": "2",
                "x": 30.0,
                "y": 20.0,
                "net": 1,
                "net_name": "NET1",
                "through_hole": True,
                "drill": 0.8,
            },
        ],
    )

    routes = router.route_net(1)
    assert isinstance(routes, list)
    assert any(route.segments for route in routes), "expected at least one routed segment"

    plane_names = set(plane_layer_names(stack))
    for route in routes:
        for segment in route.segments:
            assert segment.layer.kicad_name not in plane_names, (
                f"segment on {segment.layer.kicad_name} violates --reserve-plane-layers restriction"
            )
