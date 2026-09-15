"""Current-sense polarity does not imply a transmission-line impedance target."""

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.physics import Stackup
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair import DifferentialPair, DifferentialSignal
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.impedance import ImpedanceRule, NetImpedanceSpec


@pytest.mark.parametrize(
    "name", ["ISENSE_B-", "ISENSE_B+", "/power/ISENSE_A-", "KELVIN_P", "VSNS_N"]
)
def test_current_sense_has_no_automatic_impedance_spec(name, tmp_path):
    pcb = _board(tmp_path, (name, "OTHER"), reference="R1")
    confirmed = ImpedanceRule._collect_current_sense_nets(pcb)
    assert name in confirmed
    assert ImpedanceRule()._find_matching_spec(name, has_kelvin_root=name in confirmed) is None
    assert ImpedanceRule()._find_matching_spec(name).target_zdiff == 100.0
    explicit = NetImpedanceSpec(r".*", target_zdiff=120.0)
    assert ImpedanceRule(specs=[explicit])._find_matching_spec(name) is explicit


@pytest.mark.parametrize(
    "name,target", [("USB_D+", 90), ("LVDS_P", 100), ("DATA_N", 100), ("MIPI_CLK+", 100)]
)
def test_high_speed_defaults_remain(name, target):
    assert ImpedanceRule()._find_matching_spec(name).target_zdiff == target


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("shunt", [False, True])
def test_router_preserves_current_sense_width_unless_impedance_is_explicit(explicit, shunt):
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
    if shunt:
        router.add_component(
            "R1",
            [
                {"number": 1, "x": 5, "y": 5, "net": 1, "net_name": "ISENSE_B-"},
                {"number": 2, "x": 5, "y": 7, "net": 2, "net_name": "ISENSE_B+"},
            ],
        )
    assert router._has_synthesis_candidates() == (not shunt and not explicit)
    router._prepare_routing()
    for name in router.net_names.values():
        resolved = router.net_class_map[name]
        assert resolved.target_diff_impedance == (100.0 if explicit or not shunt else None)
        if not explicit and shunt:
            assert resolved.trace_width == 0.2
            assert resolved.effective_intra_pair_clearance() == 0.15


def _board(tmp_path, names, reference=None, footprint_name="Package_SO:SOIC-8"):
    """Real schema/physics fixture: long, intentionally over-wide paired traces."""
    footprint = ""
    if reference is not None:
        footprint = f'''(footprint "{footprint_name}" (layer "F.Cu") (at 5 5)
          (property "Reference" "{reference}")
          (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "{names[0]}"))
          (pad "2" smd rect (at 0 2) (size 1 1) (layers "F.Cu") (net 2 "{names[1]}")))'''
    path = tmp_path / "sense.kicad_pcb"
    path.write_text(f'''(kicad_pcb (version 20240108) (generator "test")
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (setup (stackup
        (layer "F.Cu" (type "copper") (thickness 0.035))
        (layer "dielectric 1" (type "core") (thickness 0.2) (epsilon_r 4.5))
        (layer "B.Cu" (type "copper") (thickness 0.035))))
      (net 0 "") (net 1 "{names[0]}") (net 2 "{names[1]}")
      {footprint}
      (segment (start 5 5) (end 15 5) (width 1.0) (layer "F.Cu") (net 1))
      (segment (start 5 7) (end 15 7) (width 1.0) (layer "F.Cu") (net 2)))''')
    return PCB.load(path)


def _rule(names, specs=None):
    pair = DifferentialPair(
        name="test",
        positive=DifferentialSignal(names[0], 1, "test", "P", "pn_suffix"),
        negative=DifferentialSignal(names[1], 2, "test", "N", "pn_suffix"),
    )
    return ImpedanceRule(specs=specs, detected_pairs=[pair])


@pytest.mark.parametrize(
    "stem", ["LIGHT_SENSE", "TEMP_SENSE_DIFF", "PROXIMITY_SENSE", "IMON", "VMON", "SHUNT", "KELVIN"]
)
@pytest.mark.parametrize("reference", [None, "U1"])
def test_sense_substring_without_shunt_retains_real_drc(tmp_path, stem, reference):
    names = (f"{stem}_P", f"{stem}_N")
    pcb = _board(tmp_path, names, reference=reference)
    rule = _rule(names)
    for name in names:
        assert rule._find_matching_spec(name).target_zdiff == 100
    results = rule.check(pcb, get_profile("jlcpcb").get_design_rules(2))
    assert rule._stackup.has_explicit_data
    assert {v.items[0] for v in results.errors} == set(names)
    assert all(v.required_value == 100 for v in results.errors)


@pytest.mark.parametrize(
    "reference,footprint_name", [("R1", "custom"), ("", "Resistor_SMD:R_0603")]
)
def test_shunt_exclusion_and_explicit_override_in_real_drc(tmp_path, reference, footprint_name):
    names = ("ISENSE_B+", "ISENSE_B-")
    pcb = _board(tmp_path, names, reference=reference, footprint_name=footprint_name)
    rules = get_profile("jlcpcb").get_design_rules(2)
    rule = _rule(names)
    assert rule._collect_current_sense_nets(pcb) == set(names)
    assert not rule.check(pcb, rules).violations
    explicit = _rule(names, [NetImpedanceSpec(r".*", target_zdiff=100)])
    assert {v.items[0] for v in explicit.check(pcb, rules).errors} == set(names)
    # Same rule, same names, different board: never retain a prior exemption.
    pcb_without_shunt = _board(tmp_path, names)
    assert {v.items[0] for v in rule.check(pcb_without_shunt, rules).errors} == set(names)


def test_resistor_on_unrelated_net_does_not_exempt_pair(tmp_path):
    names = ("LIGHT_SENSE_P", "LIGHT_SENSE_N")
    pcb = _board(tmp_path, names, reference="R1")
    for pad in pcb.footprints[0].pads:
        pad.net_name = "ISENSE_OTHER"
    results = _rule(names).check(pcb, get_profile("jlcpcb").get_design_rules(2))
    assert {v.items[0] for v in results.errors} == set(names)
