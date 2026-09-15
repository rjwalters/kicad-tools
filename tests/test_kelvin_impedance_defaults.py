"""Current-sense polarity does not imply a transmission-line impedance target."""

import pytest

from kicad_tools.physics import Stackup
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.validate.rules.impedance import ImpedanceRule, NetImpedanceSpec


@pytest.mark.parametrize(
    "name", ["ISENSE_B-", "ISENSE_B+", "/power/ISENSE_A-", "KELVIN_P", "VSNS_N"]
)
def test_current_sense_has_no_automatic_impedance_spec(name):
    assert ImpedanceRule()._find_matching_spec(name) is None
    explicit = NetImpedanceSpec(r".*", target_zdiff=120.0)
    assert ImpedanceRule(specs=[explicit])._find_matching_spec(name) is explicit


@pytest.mark.parametrize(
    "name,target", [("USB_D+", 90), ("LVDS_P", 100), ("DATA_N", 100), ("MIPI_CLK+", 100)]
)
def test_high_speed_defaults_remain(name, target):
    assert ImpedanceRule()._find_matching_spec(name).target_zdiff == target


@pytest.mark.parametrize("explicit", [False, True])
def test_router_preserves_current_sense_width_unless_impedance_is_explicit(explicit):
    nc = NetClassRouting(
        name="Audio",
        trace_width=0.2,
        clearance=0.15,
        target_diff_impedance=100.0 if explicit else None,
    )
    router = Autorouter(
        20,
        20,
        force_python=True,
        stackup=Stackup.jlcpcb_4layer(),
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
        net_class_map={"ISENSE_B-": nc, "ISENSE_B+": nc},
    )
    router.net_names.update({1: "ISENSE_B-", 2: "ISENSE_B+"})
    assert not router._has_synthesis_candidates()
    router._prepare_routing()
    for name in router.net_names.values():
        resolved = router.net_class_map[name]
        assert resolved.target_diff_impedance == (100.0 if explicit else None)
        if not explicit:
            assert resolved.trace_width == 0.2
            assert resolved.effective_intra_pair_clearance() == 0.15
