"""Independent native material witnesses for the complete exposure checker.

Set KCT_MASK_NATIVE_PYTHON to a JSON argv for KiCad's Python interpreter.
The selected native CLI and Python must both see the scratch paths unchanged.
"""

import json
import os

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
