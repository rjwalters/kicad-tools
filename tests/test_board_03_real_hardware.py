"""Revision B must implement the manufacturer's fixed ATmega32U4 pinout."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.lvs import compare_netlists
from kicad_tools.sexp import parse_file, serialize_sexp

BOARD = Path(__file__).resolve().parents[1] / "boards/03-usb-joystick"


@pytest.fixture(scope="module")
def hardware():
    spec = importlib.util.spec_from_file_location(
        "joystick_hardware", BOARD / "joystick_hardware.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manufacturer_fixed_usb_power_adc_and_programming_pins(hardware):
    pins = hardware.MCU_PINS
    assert pins["3"] == "USB_MCU_D-"
    assert pins["4"] == "USB_MCU_D+"
    assert pins["6"] == "UCAP"
    assert pins["7"] == "VBUS"
    assert pins["40"] == "JOY_Y" and pins["41"] == "JOY_X"
    assert {p: pins[p] for p in ("9", "10", "11", "13")} == {
        "9": "ISP_SCK",
        "10": "ISP_MOSI",
        "11": "ISP_MISO",
        "13": "RESET",
    }
    assert all(pins[p] == "VCC" for p in ["2", "14", "24", "34", "44"])
    assert all(pins[p] == "GND" for p in ["5", "15", "23", "35", "43"])
    assert "USB_CC1" not in pins.values() and "USB_CC2" not in pins.values()
    assert "1" not in pins  # Unused PE6 must not be silently grounded.


def test_usb_termination_regulator_and_real_filter_topology(hardware):
    c = {c.ref: c for c in hardware.COMPONENTS}
    assert c["U1"].footprint == "Package_QFP:TQFP-44_10x10mm_P0.8mm"
    for ref, net in [("R1", "USB_CC1"), ("R2", "USB_CC2")]:
        assert c[ref].value == "5.1k"
        assert c[ref].pins == {"1": net, "2": "GND"}
    assert c["R3"].pins == {"1": "USB_D-", "2": "USB_MCU_D-"}
    assert c["R4"].pins == {"1": "USB_D+", "2": "USB_MCU_D+"}
    assert c["C6"].value == "1uF" and c["C6"].pins == {"1": "UCAP", "2": "GND"}
    for ref, axis in [("R10", "X"), ("R11", "Y")]:
        assert c[ref].pins == {"1": f"JOY_{axis}_RAW", "2": f"JOY_{axis}"}
    assert all(c.mpn and c.manufacturer for c in hardware.COMPONENTS)


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    output = tmp_path_factory.mktemp("board03-real")
    for script in ["generate_schematic.py", "generate_pcb.py"]:
        subprocess.run(
            [sys.executable, str(BOARD / script), str(output)],
            check=True,
            capture_output=True,
            text=True,
        )
    return output


def test_real_44_pin_footprint_and_source_schematic_agree(generated):
    pcb = generated / "usb_joystick.kicad_pcb"
    doc = parse_file(pcb)
    mcu = next(
        fp
        for fp in doc.find_children("footprint")
        if any(
            p.get_string(0) == "Reference" and p.get_string(1) == "U1"
            for p in fp.find_children("property")
        )
    )
    assert {p.get_string(0) for p in mcu.find_children("pad")} == {str(i) for i in range(1, 45)}
    # KiCad parses footprint-at in order: placing it after pads applies a
    # second rotation to pads already read, creating false physical shorts.
    assert next(i for i, c in enumerate(mcu.children) if c.name == "at") < next(
        i for i, c in enumerate(mcu.children) if c.name == "pad"
    )
    result = compare_netlists(generated / "usb_joystick.kicad_sch", pcb)
    assert result.clean, result.mismatches


def test_isp_lands_follow_samtec_drawing_and_ship_with_project(generated):
    # Samtec TSM-DV footprint revision F: .145" lands, .050" width
    # and inner-edge gap, .100" longitudinal pitch.
    local = generated / "footprints/Joystick.pretty/Samtec_TSM-103-01-T-DV.kicad_mod"
    pads = parse_file(local).find_children("pad")
    assert len(pads) == 6
    for pad in pads:
        size, at = pad.find_child("size"), pad.find_child("at")
        assert size.get_float(0) == pytest.approx(3.683)
        assert size.get_float(1) == pytest.approx(1.270)
        assert 2 * abs(at.get_float(0)) - size.get_float(0) == pytest.approx(1.270)
    assert "${KIPRJMOD}/footprints/Joystick.pretty" in (generated / "fp-lib-table").read_text()


@pytest.fixture(scope="module")
def routing_plan():
    spec = importlib.util.spec_from_file_location("board03_routing_plan", BOARD / "routing_plan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reviewed_copper_replays_on_fresh_real_footprints(generated, routing_plan, tmp_path):
    routed = tmp_path / "routed.kicad_pcb"
    routing_plan.apply_plan(generated / "usb_joystick.kicad_pcb", routed)
    measurements = routing_plan.usb_geometry(routed)
    assert measurements["common_coupled_mm"] >= 4
    assert measurements["branch_skew_mm"] <= 0.5
    assert max(measurements["lengths_mm"].values()) <= 16
    assert routing_plan.fingerprint(routed) == routing_plan.fingerprint(
        generated / "usb_joystick.kicad_pcb"
    )


@pytest.mark.parametrize(
    "change", ["placement", "pad_size", "pin_net", "via_process", "dielectric"]
)
def test_reviewed_copper_rejects_changed_physical_design(generated, routing_plan, tmp_path, change):
    doc = parse_file(generated / "usb_joystick.kicad_pcb")
    fp = doc.find_children("footprint")[0]
    if change == "placement":
        fp.find_child("at").set_atom(0, fp.find_child("at").get_float(0) + 0.1)
    elif change == "pad_size":
        fp.find_children("pad")[0].find_child("size").set_atom(0, 0.8)
    elif change == "pin_net":
        ground = next(n for n in doc.find_children("net") if n.get_string(1) == "GND")
        net = next(p.find_child("net") for p in fp.find_children("pad") if p.find_child("net"))
        net.set_atom(0, ground.get_int(0))
        net.set_atom(1, "GND")
    elif change == "via_process":
        doc.find_child("setup").find_child("capping").set_atom(0, "no")
    else:
        doc.find_child("setup").find_child("stackup").find_children("layer")[1].find_child(
            "thickness"
        ).set_atom(0, 0.1)
    changed = tmp_path / "changed.kicad_pcb"
    changed.write_text(serialize_sexp(doc))
    output = tmp_path / "reviewed.kicad_pcb"
    output.write_text("prior reviewed output")
    with pytest.raises(ValueError, match="does not match physical circuit"):
        routing_plan.apply_plan(changed, output)
    assert output.read_text() == "prior reviewed output"


def test_usb_guard_rejects_removed_common_run(generated, routing_plan, tmp_path):
    routed = tmp_path / "routed.kicad_pcb"
    routing_plan.apply_plan(generated / "usb_joystick.kicad_pcb", routed)
    doc = parse_file(routed)
    doc.children = [
        n
        for n in doc.children
        if not (n.name == "segment" and n.find_child("layer").get_string(0) == "In2.Cu")
    ]
    routed.write_text(serialize_sexp(doc))
    with pytest.raises(ValueError, match="Missing USB common run"):
        routing_plan.usb_geometry(routed)


@pytest.fixture(scope="module")
def manufacturing_checker(generated):
    # Produce actual replay inputs with native and Python fabrication settings.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from generate_design import route_pcb, create_project; "
            'p=Path(__import__("sys").argv[1]); '
            'route_pcb(p/"usb_joystick.kicad_pcb", p/"usb_joystick_routed.kicad_pcb"); '
            'create_project(p,"usb_joystick_routed")',
            str(generated),
        ],
        cwd=BOARD,
        check=True,
        capture_output=True,
        text=True,
    )
    sys.path.insert(0, str(BOARD))
    try:
        spec = importlib.util.spec_from_file_location(
            "board03_check_manufacturing", BOARD / "check_manufacturing.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(BOARD))


def test_reviewed_hole_floor_does_not_mutate_shared_profile(generated, manufacturing_checker):
    from kicad_tools.manufacturers import get_profile

    baseline = get_profile("jlcpcb-tier1").get_design_rules(4).min_hole_to_hole_mm
    checker = manufacturing_checker.make_checker(generated / "usb_joystick_routed.kicad_pcb")
    assert checker.design_rules.min_hole_to_hole_mm == 0.45
    assert get_profile("jlcpcb-tier1").get_design_rules(4).min_hole_to_hole_mm == baseline
    assert baseline == 0.5


@pytest.mark.parametrize("change", ["native_floor", "stackup"])
def test_manufacturing_check_rejects_mismatched_fabrication(
    generated, manufacturing_checker, tmp_path, change
):
    import json
    import shutil

    work = tmp_path / "board"
    shutil.copytree(generated, work)
    pcb = work / "usb_joystick_routed.kicad_pcb"
    if change == "native_floor":
        project = pcb.with_suffix(".kicad_pro")
        data = json.loads(project.read_text())
        data["board"]["design_settings"]["rules"]["min_hole_to_hole"] = 0.5
        project.write_text(json.dumps(data))
    else:
        doc = parse_file(pcb)
        dielectric = doc.find_child("setup").find_child("stackup").find_children("layer")[1]
        dielectric.find_child("thickness").set_atom(0, 0.3)
        pcb.write_text(serialize_sexp(doc))
    with pytest.raises(ValueError, match="Native pad-hole|Physical circuit"):
        manufacturing_checker.make_checker(pcb)
