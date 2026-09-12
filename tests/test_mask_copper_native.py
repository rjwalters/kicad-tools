"""Independent native material witnesses for the complete exposure checker.

Set KCT_MASK_NATIVE_PYTHON to a JSON argv for KiCad's Python interpreter.
The selected native CLI and Python must both see the scratch paths unchanged.
"""

import json
import os
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point

from kicad_tools.validate.mask_copper import (
    MaskCopperPolicy,
    MaskEscapeIntent,
    check_mask_to_copper,
)
from tests._mask_gerbonara_oracle import read_native_gerber
from tests.test_mask_export_geometry import NATIVE_CASES, _board

POLICY = MaskCopperPolicy(0.1, "fixture process specification", "test process", "rev1")


@pytest.fixture
def native_options(tmp_path):
    from kicad_tools.cli.runner import find_kicad_cli

    if not os.environ.get("KCT_MASK_NATIVE_PYTHON") or find_kicad_cli() is None:
        pytest.skip("KiCad CLI and explicit native Python required")
    pytest.importorskip("gerbonara", minversion="1.6.3")
    return {
        "native_python_command": json.loads(os.environ["KCT_MASK_NATIVE_PYTHON"]),
        "artifact_dir": tmp_path / "native",
        "scratch_dir": tmp_path,
    }


@pytest.mark.parametrize("case", ["custom_rotated", "chamfer", "padstack", "graphics_text", "zone"])
def test_advanced_native_shapes_complete_and_match_independent_material(
    tmp_path, native_options, case
):
    path = _board(
        tmp_path / f"{case}.kicad_pcb", NATIVE_CASES[case], setup="(pad_to_mask_clearance .05)"
    )
    before = path.read_bytes()
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert result.passed, result.to_dict()
    assert path.read_bytes() == before
    for layer in ("F.Mask", "B.Mask", "F.Cu", "B.Cu"):
        oracle = read_native_gerber(
            native_options["artifact_dir"] / f"{case}-{layer.replace('.', '_')}.gbr"
        )
        from shapely import wkt

        actual = wkt.loads(result.geometry_provenance["export"]["layers_wkt"][layer])
        assert actual.difference(oracle.buffer(0.000005)).is_empty
        assert oracle.difference(actual.buffer(0.000005)).is_empty


@pytest.mark.parametrize("case", ["passing", "unintended", "connected", "overlapped"])
def test_final_native_intent_and_exposure_outcomes(tmp_path, native_options, case):
    if case == "connected":
        start, end = (10.4, 10), (12, 10)
    elif case == "overlapped":
        start, end = (9.8, 10), (10.2, 10)
    else:
        x = 10.9 if case == "passing" else 10.58
        start, end = (x, 9), (x, 11)
    trace_uuid = "00000000-0000-0000-0000-000000000099"
    body = f'''(net 1 "N")
      (footprint "P" (layer "F.Cu") (at 10 10)
       (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") (net 1 "N")))
      (segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) (width .1) (layer "F.Cu") (net 1) (uuid "{trace_uuid}"))'''
    path = _board(tmp_path / f"{case}.kicad_pcb", body, setup="(pad_to_mask_clearance .1)")
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert result.passed is (case == "passing"), result.to_dict()
    # Independent Gerbonara interpretation of actual native mask material,
    # intersected with this fixture's exact authored straight conductor capsule.
    mask = read_native_gerber(native_options["artifact_dir"] / f"{case}-F_Mask.gbr")
    conductor = LineString([start, end]).buffer(0.05, quad_segs=4096)
    exposed = mask.intersection(conductor).area
    if case == "passing":
        assert exposed == 0
    else:
        measurement = next(m for m in result.measurements if m.conductor_uuid == trace_uuid)
        assert measurement.exposed_area_mm2 == pytest.approx(exposed, abs=0.000002)
        assert measurement.disposition == "violation"
    if case == "connected":
        owner = "00000000-0000-0000-0000-000000000002"
        declaration = MaskEscapeIntent(
            result.binding, owner, trace_uuid, "F.Mask", "explicit reviewed escape"
        )
        intended = check_mask_to_copper(path, POLICY, [declaration], **native_options)
        assert intended.passed, intended.to_dict()
        assert intended.measurements[0].exposed_area_mm2 == pytest.approx(exposed, abs=0.000002)
        assert intended.measurements[0].disposition == "intentional"


def test_native_merged_bridge_exposes_third_copper_with_multiple_contributors(
    tmp_path, native_options
):
    path = _board(
        tmp_path / "merged.kicad_pcb",
        NATIVE_CASES["merged"],
        setup="(pad_to_mask_clearance 0) (solder_mask_min_width .3)",
    )
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert not result.passed
    bridge = [
        m
        for m in result.measurements
        if m.opening_kind == "merged_region" and m.exposed_area_mm2 > 0.01
    ]
    assert bridge
    assert all(len(m.opening_uuids) >= 2 and m.disposition == "violation" for m in bridge)
    oracle = read_native_gerber(native_options["artifact_dir"] / "merged-F_Mask.gbr")
    assert oracle.contains(Point(10, 10))


@pytest.mark.parametrize("margin,web", [(0.05, 0), (-0.05, 0), (0.05, 0.2), (-0.05, 0.2)])
def test_native_padstack_plot_branch_context(tmp_path, native_options, margin, web):
    path = _board(
        tmp_path / "stack-context.kicad_pcb",
        NATIVE_CASES["padstack"],
        setup=f"(pad_to_mask_clearance {margin}) (solder_mask_min_width {web})",
    )
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert result.passed, result.to_dict()
    # Native direct-mode setter/read asymmetry leaves the padstack mask at
    # copper size; web-merging uses a distinct polygon expansion path.
    oracle = read_native_gerber(native_options["artifact_dir"] / "stack-context-B_Mask.gbr")
    if web == 0 or margin < 0:
        assert oracle.area == pytest.approx(6, abs=0.00001)
    elif margin > 0:
        assert oracle.area > 6


@pytest.mark.parametrize("case", ["custom", "back_roundrect", "negative"])
def test_native_advanced_and_mask_defined_owner_never_hide_neighbor(tmp_path, native_options, case):
    layer = "B.Cu" if case == "back_roundrect" else "F.Cu"
    if case == "custom":
        body = NATIVE_CASES["custom_rotated"]
        start, end = (11.55, 8.79), (11.65, 8.79)
        margin = 0.05
    else:
        side = layer.replace(".Cu", ".Mask")
        body = f'''(footprint "P" (layer "{layer}") (at 10 10 37)
          (pad "1" smd roundrect (at 0 0 37) (size 2 1) (roundrect_rratio .25)
           (layers "{layer}" "{side}")))'''
        start, end = (9.95, 10), (10.05, 10)
        margin = -0.1 if case == "negative" else 0.05
    identity = "00000000-0000-0000-0000-000000000099"
    body += f'(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) (width .05) (layer "{layer}") (net 0) (uuid "{identity}"))'
    path = _board(tmp_path / f"{case}.kicad_pcb", body, setup=f"(pad_to_mask_clearance {margin})")
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert not result.passed
    finding = next(m for m in result.measurements if m.conductor_uuid == identity)
    oracle = read_native_gerber(
        native_options["artifact_dir"] / f"{case}-{layer.replace('.Cu', '_Mask')}.gbr"
    )
    expected = oracle.intersection(LineString([start, end]).buffer(0.025, quad_segs=4096)).area
    assert finding.exposed_area_mm2 == pytest.approx(expected, abs=0.000002)
    assert finding.exposed_area_mm2 > 0.001
    assert finding.mask_defined is (case == "negative")
    if case == "negative":
        assert finding.negative_expansion
        assert finding.margin_provenance == "board:pad_to_mask_clearance=-0.1"


@pytest.mark.parametrize("front,back", [("yes", "no"), ("no", "yes"), ("yes", "yes"), ("no", "no")])
def test_native_via_tenting_controls_each_actual_mask_side(tmp_path, native_options, front, back):
    via = "00000000-0000-0000-0000-000000000001"
    body = f'(via (at 10 10) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (tenting (front {front}) (back {back})) (uuid "{via}"))'
    path = _board(tmp_path / "via.kicad_pcb", body)
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert result.passed, result.to_dict()
    for side, tented in [("F", front), ("B", back)]:
        oracle = read_native_gerber(native_options["artifact_dir"] / f"via-{side}_Mask.gbr")
        assert oracle.is_empty is (tented == "yes")


def test_native_stored_plot_policy_default_frame(tmp_path, native_options):
    from kicad_tools.validate.mask_export_geometry import MaskExportOptions

    path = _board(
        tmp_path / "stored.kicad_pcb",
        NATIVE_CASES["padstack"],
        setup="(pcbplotparams (disableapertmacros false) (usegerberextensions true))",
    )
    result = check_mask_to_copper(
        path, POLICY, options=MaskExportOptions(board_plot_params=True), **native_options
    )
    assert result.coverage == "complete", result.reasons
    assert result.passed


@pytest.mark.parametrize(
    "footprint_margin,pad_margin,expected,source",
    [
        (None, None, 0.1, "board:"),
        (0.2, None, 0.2, "footprint:"),
        (0.2, 0, 0, "object:"),
        (0.2, -0.1, -0.1, "object:"),
    ],
)
def test_final_native_expansion_precedence(
    tmp_path, native_options, footprint_margin, pad_margin, expected, source
):
    fp_override = "" if footprint_margin is None else f"(solder_mask_margin {footprint_margin})"
    pad_override = "" if pad_margin is None else f"(solder_mask_margin {pad_margin})"
    body = f"""(footprint "P" (layer "F.Cu") (at 10 10) {fp_override}
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask") {pad_override}))"""
    path = _board(tmp_path / "precedence.kicad_pcb", body, setup="(pad_to_mask_clearance .1)")
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert result.passed
    pad = result.geometry_provenance["source_objects"]["00000000-0000-0000-0000-000000000002"]
    assert pad["margin_mm"]["F.Mask"] == expected
    assert pad["margin_source"].startswith(source)
    oracle = read_native_gerber(native_options["artifact_dir"] / "precedence-F_Mask.gbr")
    assert oracle.bounds == pytest.approx(
        (9.5 - expected, 9.5 - expected, 10.5 + expected, 10.5 + expected), abs=0.000005
    )


def test_native_repeated_pad_numbers_do_not_share_owner_exclusion(tmp_path, native_options):
    body = """(footprint "P" (layer "F.Cu") (at 10 10)
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask"))
      (pad "1" smd rect (at .4 0) (size 1 1) (layers "F.Cu" "F.Mask")))"""
    path = _board(tmp_path / "repeated.kicad_pcb", body)
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "complete", result.reasons
    assert not result.passed
    first = "00000000-0000-0000-0000-000000000002"
    second = "00000000-0000-0000-0000-000000000003"
    pairs = {(m.opening_uuids, m.conductor_uuid) for m in result.measurements}
    assert ((first,), second) in pairs
    assert ((second,), first) in pairs
    assert all(m.exposed_area_mm2 > 0.59 for m in result.measurements)


def test_actual_native_cli_serializes_exposure_and_report_binding(tmp_path, native_options, capsys):
    from kicad_tools.cli.check_cmd import main
    from kicad_tools.drc.report import DRCReport

    body = """(footprint "P" (layer "F.Cu") (at 10 10)
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))
      (segment (start 10.58 9) (end 10.58 11) (width .1) (layer "F.Cu") (net 0)
       (uuid "00000000-0000-0000-0000-000000000099"))"""
    path = _board(tmp_path / "cli.kicad_pcb", body, setup="(pad_to_mask_clearance .1)")
    config = tmp_path / "request.json"
    config.write_text(
        json.dumps(
            {
                "schema": "kct.mask-copper-request.v1",
                "policy": POLICY.to_dict(),
                "native": {
                    key: str(value) if isinstance(value, Path) else value
                    for key, value in native_options.items()
                },
            }
        )
    )
    report_path = tmp_path / "report.json"
    assert (
        main(
            [
                str(path),
                "--drc-only",
                "--only",
                "mask_to_copper",
                "--mask-copper-config",
                str(config),
                "--format",
                "json",
                "--output",
                str(report_path),
            ]
        )
        == 2
    )
    stdout = json.loads(capsys.readouterr().out)
    assessment = stdout["mask_copper_assessments"][0]
    assert assessment["coverage"] == "complete"
    assert not stdout["summary"]["passed"]
    assert assessment["measurements"][0]["exposed_area_mm2"] > 0
    report = DRCReport.load(report_path)
    assert not report.passed
    assert json.loads(json.dumps(report.mask_copper_assessments[0].to_dict())) == assessment


@pytest.mark.parametrize("defect", ["missing", "duplicate"])
def test_final_native_identity_defects_cannot_pass(tmp_path, native_options, defect):
    body = """(footprint "P" (layer "F.Cu") (at 10 10)
      (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))"""
    path = _board(tmp_path / "identity.kicad_pcb", body)
    raw = path.read_text()
    pad_uuid = "00000000-0000-0000-0000-000000000002"
    if defect == "duplicate":
        raw = raw.replace(pad_uuid, "00000000-0000-0000-0000-000000000001")
    else:
        raw = raw.replace(f'(uuid "{pad_uuid}")', "")
    path.write_text(raw)
    result = check_mask_to_copper(path, POLICY, **native_options)
    assert result.coverage == "incomplete"
    assert not result.passed
    assert any(
        "uuid" in reason.lower() or "identity" in reason.lower() for reason in result.reasons
    ), result.reasons


@pytest.mark.parametrize("covered_by_pad", [False, True])
def test_uninventoried_native_target_cannot_hide_in_copper_union(
    tmp_path, native_options, covered_by_pad
):
    identity = "00000000-0000-0000-0000-000000000099"
    pad = """(footprint "P" (layer "F.Cu") (at 10 10)
      (pad "1" smd rect (at 0 0) (size 4 4) (layers "F.Cu" "F.Mask")))"""
    target = f'''(target plus (at 10 10) (size 1) (width .1)
      (layer "F.Cu") (uuid "{identity}"))'''
    path = _board(tmp_path / "target.kicad_pcb", (pad if covered_by_pad else "") + target)
    before = path.read_bytes()
    result = check_mask_to_copper(path, POLICY, **native_options)
    # The target-only parameter proves this native object really plots copper;
    # the covered parameter prevents union parity from hiding its omission.
    copper = read_native_gerber(native_options["artifact_dir"] / "target-F_Cu.gbr")
    assert copper.area > 0
    assert path.read_bytes() == before
    assert result.coverage == "incomplete", result.to_dict()
    assert not result.passed
    assert any(identity in reason and "uninventoried" in reason for reason in result.reasons)
