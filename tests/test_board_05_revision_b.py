"""Real revision-B component pinout, procurement and copper provenance guards."""

import importlib.util
import sys
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB

SOURCE = Path(__file__).resolve().parents[1] / "boards/05-bldc-motor-controller/redesign"


def load(name):
    spec = importlib.util.spec_from_file_location(f"board05_revb_{name}", SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hardware():
    return load("hardware")


def test_real_driver_current_comparator_and_power(hardware):
    parts = {part.ref: part for part in hardware.parts()}
    assert parts["U2"].mpn == "DRV8313PWPR"
    assert parts["U2"].nets["12"] == "IREF"  # COMPP
    assert parts["U2"].nets["13"] == "SENSE_IN"  # COMPN: low output on overcurrent
    assert parts["U2"].nets["19"] == parts["U1"].nets["32"] == "nTRIP"
    assert parts["U2"].nets["1"] == "CPL"
    assert parts["U2"].nets["2"] == "CPH"
    assert parts["U2"].nets["3"] == "VCP"
    assert parts["U3"].nets["3"] == parts["U1"].nets["29"] == "RESET"
    assert parts["U2"].nets["29"] == parts["U3"].nets["9"] == "GND"


def test_all_parts_have_exact_procurement_and_physical_pins(hardware):
    parts = hardware.parts()
    assert len(parts) == 42
    assert len({part.ref for part in parts}) == 42
    assert all(part.mpn and part.lcsc[0] == "C" and part.lcsc[1:].isdigit() for part in parts)
    pcb = hardware.build_pcb()
    for part in parts:
        fp = pcb.get_footprint(part.ref)
        actual = {pad.number: pad.net_name for pad in fp.pads if pad.number in part.nets}
        assert actual == part.nets, part.ref
    assert pcb.get_footprint("U1").name.endswith("TQFP-32_7x7mm_P0.8mm")
    assert pcb.get_footprint("C1").name.endswith("CP_Elec_8x10.5_PolarityMark")


def test_reference_copper_rejects_a_changed_real_net(tmp_path, hardware):
    routing = load("routing")
    path = tmp_path / "board.kicad_pcb"
    hardware.build_pcb().save(path)
    original = routing.geometry_fingerprint(path)
    routing.apply_routing(path)
    assert routing.geometry_fingerprint(path) == original
    assert PCB.load(path).segments
    from kicad_tools.router.quantize import segment_angle_census

    assert not segment_angle_census(path)[1], (
        "Reviewed copper must preserve the fleet 45-degree policy"
    )
    hardware.build_pcb().save(path)
    changed = PCB.load(path)
    changed.assign_net_to_footprint_pad("U2", "12", "GND")
    changed.save(path)
    with pytest.raises(ValueError, match="geometry changed"):
        routing.apply_routing(path)


def test_thermal_vias_keep_real_smt_ics_in_cpl(hardware):
    from kicad_tools.export.pnp import is_through_hole_footprint

    pcb = hardware.build_pcb()
    assert all(not is_through_hole_footprint(pcb.get_footprint(ref)) for ref in ["U1", "U2", "U3"])
    assert all(
        is_through_hole_footprint(pcb.get_footprint(ref))
        for ref in ["J1", "J2", "J3", "J4", "J5", "RV1"]
    )
