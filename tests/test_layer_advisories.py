"""Unit tests for route-time layer-selection advisories (Issue #4314).

Exercises :mod:`kicad_tools.router.layer_advisories`, the warn-floor guards
that surface two silent ``kct route`` footguns:

* **Tier 1** -- ``--layers auto`` picks a signal-on-inner stack while the
  net-class-map declares ``is_pour_net`` classes (``detect_layer_stack``
  never sees that intent).
* **Tier 2** -- a ``target_ampacity`` net whose required internal-copper
  width is unroutable on an inner layer, predicting the post-route ampacity
  DRC failure at route time.

The Tier-2 required-width number is asserted to equal the post-route
ampacity DRC's number (both call ``width_for_current`` with the identical
``inner_copper_oz`` / ``layer="internal"`` shape), and the drift-prevention
contract (no warnings when the map declares neither pour nets nor
``target_ampacity``) is pinned so the guards stay silent on ordinary boards.
"""

from __future__ import annotations

from kicad_tools.manufacturers import get_profile
from kicad_tools.router.layer_advisories import (
    AmpacityLayerConflict,
    ampacity_inner_layer_conflicts,
    declared_pour_net_names,
    is_external_layer,
    pour_net_blind_auto_warning,
    stack_routes_signal_on_inner,
)
from kicad_tools.router.layers import LayerStack
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
