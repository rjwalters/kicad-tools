"""Measured object/layer controls for the complete generated factory rules."""

import json
import subprocess
from dataclasses import replace

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.manufacturers import get_profile, write_drc_constraints
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.clearance import ClearanceRule
from kicad_tools.validate.rules.factory_clearance import check_silk_pad_clearance


def board_fixture(path, kind, gap, *, layer="F.Cu", same_net=False, pad_type="smd", masked=True):
    net = 1 if same_net else 2
    if kind in ("pth", "via"):
        pad = (
            '(pad "1" thru_hole circle (at 0 0) (size .7 .7) (drill .4) '
            '(layers "*.Cu" "*.Mask") (net 1 "A"))'
        )
        first = f'(footprint "T" (layer "F.Cu") (at 10 10) {pad})'
        if kind == "via":
            first = '(via (at 10 10) (size .7) (drill .4) (layers "F.Cu" "B.Cu") (net 1))'
        x = 10 + 0.2 + gap + 0.06
        other = f'(segment (start {x} 9) (end {x} 11) (width .12) (layer "{layer}") (net {net}))'
    else:
        mask = ' "F.Mask"' if masked else ""
        drill = "(drill .4)" if pad_type == "thru_hole" else ""
        first = f'(footprint "T" (layer "F.Cu") (at 10 10) (pad "1" {pad_type} rect (at 0 0) (size 1 1) {drill} (layers "F.Cu"{mask}) (net 1 "A")))'
        if kind == "silk":
            x = 10.5 + gap + 0.075
            silk_layer = "F.SilkS" if layer == "F.Cu" else "B.SilkS"
            other = f'(gr_line (start {x} 9) (end {x} 11) (stroke (width .15) (type default)) (layer "{silk_layer}"))'
        else:
            other = f'(footprint "T" (layer "{layer}") (at {11 + gap} 10) (pad "2" smd rect (at 0 0) (size 1 1) (layers "{layer}") (net {net} "{"A" if same_net else "B"}")))'
    path.write_text(f"""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)
       (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
       (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "A") (net 2 "B")
      (gr_rect (start 0 0) (end 20 20) (stroke (width .1) (type default))
       (fill none) (layer "Edge.Cuts")) {first} {other})""")
    return path


CASES = [
    ("silk", 0.085, {}, True),
    ("silk", 0.16, {}, False),
    ("silk", 0.085, {"layer": "B.Cu"}, False),
    ("silk", 0.085, {"masked": False}, True),
    ("smd", 0.12, {}, True),
    ("smd", 0.16, {}, False),
    ("smd", 0.12, {"same_net": True}, False),
    ("smd", 0.12, {"layer": "B.Cu"}, False),
    ("smd", 0.12, {"pad_type": "thru_hole"}, False),
    ("pth", 0.27, {}, True),
    ("pth", 0.29, {}, False),
    ("pth", 0.29, {"layer": "In1.Cu"}, True),
    ("pth", 0.31, {"layer": "In1.Cu"}, False),
    ("pth", 0.27, {"same_net": True}, False),
    ("via", 0.27, {}, False),
    ("via", 0.29, {"layer": "In1.Cu"}, False),
]


@pytest.mark.parametrize("kind,gap,options,expected", CASES)
def test_python_object_specific_clearance(tmp_path, kind, gap, options, expected):
    path = board_fixture(tmp_path / "probe.kicad_pcb", kind, gap, **options)
    pcb = PCB.load(path)
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
    results = (
        check_silk_pad_clearance(pcb, rules)
        if kind == "silk"
        else ClearanceRule().check(pcb, rules)
    )
    relevant = (
        [v for v in results.violations if v.rule_id == "pth_hole_clearance"]
        if kind in ("pth", "via")
        else results.violations
    )
    assert bool(relevant) == expected, relevant
    if expected:
        assert relevant[0].actual_value == pytest.approx(gap, abs=0.0001)
        assert relevant[0].severity == "error"


@pytest.mark.parametrize("kind,gap,options,expected", CASES)
def test_native_object_specific_clearance(tmp_path, kind, gap, options, expected):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    path = board_fixture(tmp_path / "probe.kicad_pcb", kind, gap, **options)
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    report = tmp_path / "native.json"
    proc = subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    violations = json.loads(report.read_text())["violations"]
    assert not [v for v in violations if v["type"] == "drc_rule_error"], violations
    names = {
        "silk": ("Silk to Pad",),
        "smd": ("SMD Pad Clearance",),
        "pth": ("PTH Hole to Track", "Inner PTH Hole to Copper"),
        "via": ("PTH Hole to Track", "Inner PTH Hole to Copper"),
    }[kind]
    relevant = [v for v in violations if any(f"rule '{name}" in v["description"] for name in names)]
    assert bool(relevant) == expected, violations
    if expected:
        assert relevant[0]["severity"] == "error"


def test_optional_constraints_preserve_other_profiles_and_stricter_general_clearance(tmp_path):
    from kicad_tools.manufacturers.dru_generator import generate_dru

    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1)
    assert "SMD Pad Clearance" in generate_dru(rules)
    legacy = replace(
        rules,
        min_silk_to_pad_clearance_mm=None,
        min_smd_pad_clearance_mm=None,
        min_pth_hole_to_track_mm=None,
        min_inner_pth_hole_to_copper_mm=None,
    )
    assert "Silk to Pad" not in generate_dru(legacy)
    strict = replace(rules, min_clearance_mm=0.2)
    path = board_fixture(tmp_path / "probe.kicad_pcb", "smd", 0.18)
    violations = ClearanceRule().check(PCB.load(path), strict).violations
    assert violations and violations[0].required_value == 0.2
    assert "(constraint clearance (min 0.2mm))" in generate_dru(strict)


@pytest.mark.parametrize("kind,gap", [("silk", 0.085), ("smd", 0.12), ("pth", 0.27)])
def test_native_old_profile_misses_new_constraint(tmp_path, kind, gap):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    path = board_fixture(tmp_path / "probe.kicad_pcb", kind, gap)
    rules = replace(
        get_profile("jlcpcb").get_design_rules(layers=4),
        min_silk_to_pad_clearance_mm=None,
        min_smd_pad_clearance_mm=None,
        min_pth_hole_to_track_mm=None,
        min_inner_pth_hole_to_copper_mm=None,
    )
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    report = tmp_path / "native.json"
    subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    violations = json.loads(report.read_text())["violations"]
    assert not [
        v
        for v in violations
        if v["type"] in ("silk_over_copper", "silk_overlap", "clearance", "hole_clearance")
    ], violations


def test_pth_slot_rotation_offset_and_zone_fill(tmp_path):
    from kicad_tools.validate.rules.factory_clearance import _hole_geometry

    path = board_fixture(tmp_path / "probe.kicad_pcb", "pth", 0.27, layer="In1.Cu")
    path.write_text(
        path.read_text().replace(
            "(at 0 0) (size .7 .7) (drill .4)",
            "(at 0 0 90) (size 1 1) (drill oval .4 .8 (offset .1 0))",
        )
    )
    pcb = PCB.load(path)
    fp = pcb.footprints[0]
    geom = _hole_geometry(fp.pads[0], fp)
    assert geom.bounds == pytest.approx((9.6, 9.7, 10.4, 10.1), abs=0.0001)
    # A slot's long dimension reaches the nearby inner track: drill=0 in
    # the legacy schema must not cause this hole to disappear from checking.
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    assert [
        v for v in ClearanceRule().check(pcb, rules).violations if v.rule_id == "pth_hole_clearance"
    ]
    text = board_fixture(path, "pth", 0.4, layer="In1.Cu").read_text()
    zone = """(zone (net 2) (net_name "B") (layer "In1.Cu") (hatch edge .5)
      (connect_pads (clearance .1)) (min_thickness .1) (fill yes)
      (polygon (pts (xy 10.49 9) (xy 11 9) (xy 11 11) (xy 10.49 11)))
      (filled_polygon (layer "In1.Cu") (pts (xy 10.49 9) (xy 11 9) (xy 11 11) (xy 10.49 11))))"""
    path.write_text(text[:-1] + zone + ")")
    violations = [
        v
        for v in ClearanceRule().check(PCB.load(path), rules).violations
        if v.rule_id == "pth_hole_clearance"
    ]
    assert len(violations) == 1
    assert violations[0].actual_value == pytest.approx(0.29, abs=0.0001)
    assert "Zone" in violations[0].items[1]


def test_custom_rule_survives_repeated_emission(tmp_path):
    path = board_fixture(tmp_path / "probe.kicad_pcb", "smd", 0.16)
    dru = path.with_suffix(".kicad_dru")
    custom = (
        '(version 1)\n# Reviewed creepage\n(rule "Reviewed HV" (constraint clearance (min 2mm)))\n'
    )
    dru.write_text(custom)
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    first = dru.read_bytes()
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    assert dru.read_bytes() == first
    assert custom.strip() in first.decode()
    assert first.count(b'(rule "Silk to Pad') == 1


@pytest.mark.parametrize("layer", ["F.Cu", "In1.Cu"])
@pytest.mark.parametrize(
    "hole_net,track_net,gap,expected",
    [
        (0, 2, 0.27, True),
        (1, 0, 0.27, True),
        (0, 0, 0.27, True),
        (1, 1, 0.27, False),
        (0, 2, 0.31, False),
    ],
)
def test_unassigned_pth_native_python_parity(tmp_path, layer, hole_net, track_net, gap, expected):
    from kicad_tools.validate.rules.factory_clearance import check_pth_hole_clearance

    path = board_fixture(tmp_path / "probe.kicad_pcb", "pth", gap, layer=layer)
    text = path.read_text().replace(
        '(net 1 "A")))', f'(net {hole_net} "{"A" if hole_net else ""}")))'
    )
    text = text.replace("(net 2))", f"(net {track_net}))")
    path.write_text(text)
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    violations = check_pth_hole_clearance(PCB.load(path), rules).violations
    assert bool(violations) == expected, violations
    assert all(v.actual_value == pytest.approx(gap, abs=0.0001) for v in violations)
    # Native hole_clearance skips equal net codes, including two net-0
    # objects. Python deliberately does not infer a connection from that.
    _assert_native_pth_scope(path, rules, expected and (hole_net != 0 or track_net != 0))


def _assert_native_pth_scope(path, rules, expected):
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=4)
    report = path.with_suffix(".json")
    subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "-o", str(report), str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    violations = json.loads(report.read_text())["violations"]
    relevant = [
        v
        for v in violations
        if any(
            f"rule '{name}" in v["description"]
            for name in ("PTH Hole to Track", "Inner PTH Hole to Copper")
        )
    ]
    assert bool(relevant) == expected, violations


@pytest.mark.parametrize("layer,expected", [("F.Cu", False), ("In1.Cu", True)])
@pytest.mark.parametrize("zone_net", [0, 2])
def test_pth_zone_layer_native_python_parity(tmp_path, layer, expected, zone_net):
    from kicad_tools.validate.rules.factory_clearance import check_pth_hole_clearance

    path = board_fixture(tmp_path / "probe.kicad_pcb", "pth", 0.4, layer=layer)
    zone = f'''(zone (net {zone_net}) (net_name "{"B" if zone_net else ""}") (layer "{layer}") (hatch edge .5)
      (connect_pads (clearance .1)) (min_thickness .1) (fill yes)
      (polygon (pts (xy 10.47 9) (xy 11 9) (xy 11 11) (xy 10.47 11)))
      (filled_polygon (layer "{layer}") (pts (xy 10.47 9) (xy 11 9) (xy 11 11) (xy 10.47 11))))'''
    path.write_text(path.read_text()[:-1] + zone + ")")
    rules = get_profile("jlcpcb").get_design_rules(layers=4)
    violations = check_pth_hole_clearance(PCB.load(path), rules).violations
    assert bool(violations) == expected, violations
    _assert_native_pth_scope(path, rules, expected)
