"""Source-qualified mask exposure and public coverage controls."""

import pytest

from kicad_tools.validate.mask_copper import MaskCopperAssessment, MaskCopperPolicy
from kicad_tools.validate.violations import DRCResults


def test_policy_requires_explicit_process_provenance():
    with pytest.raises(ValueError):
        MaskCopperPolicy(0.1, "", "process", "revision")
    with pytest.raises(ValueError):
        MaskCopperPolicy(float("nan"), "source", "process", "revision")


def test_requested_incomplete_assessment_survives_public_aggregation():
    assessment = MaskCopperAssessment(coverage="incomplete", reasons=["native unavailable"])
    result = DRCResults()
    result.mask_copper_assessments.append(assessment)
    merged = DRCResults()
    merged.merge(result)
    assert not merged.passed
    assert merged.to_dict()["mask_copper_assessments"][0]["coverage"] == "incomplete"


def test_complete_without_policy_or_binding_is_not_pass():
    assert not MaskCopperAssessment(coverage="complete").passed
    with pytest.raises(ValueError):
        MaskCopperAssessment(coverage="invented")


@pytest.mark.parametrize("flags", [[], ["--strict"], ["--allow-incomplete"]])
def test_actual_cli_missing_policy_never_prints_or_serializes_pass(tmp_path, capsys, flags):
    import json

    from kicad_tools.cli.check_cmd import main
    from kicad_tools.schema.pcb import PCB

    path = tmp_path / "board.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(path)
    report = tmp_path / "result.json"
    assert (
        main(
            [
                str(path),
                "--only",
                "mask_to_copper",
                "--format",
                "json",
                "--drc-only",
                "--output",
                str(report),
                *flags,
            ]
        )
        == 2
    )
    data = json.loads(capsys.readouterr().out)
    assert not data["summary"]["passed"]
    assert data["violations"] == []
    assert data["mask_copper_assessments"][0]["coverage"] == "not_run"
    assert (
        json.loads(report.read_text())["mask_copper_assessments"] == data["mask_copper_assessments"]
    )


def toy_geometry(conductor_x=1.2, *, overlap_owner=False, merged=False):
    from shapely.geometry import box

    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    owner, other = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    pad = box(0, 0, 1, 1)
    trace = box(0.5 if overlap_owner else conductor_x, 0.4, 2, 0.6)
    opening = box(-0.1, -0.1, 1.1, 1.1)
    exported = ExportedMaskGeometry(
        "a" * 64, None, None, "10.0.5", {}, layers={"F.Mask": opening, "F.Cu": pad.union(trace)}
    )
    geometry = AttributedMaskGeometry(
        exported,
        {
            owner: {
                "kind": "pad:rect",
                "net": "N",
                "layers_geometry": {"F.Mask": opening, "F.Cu": pad},
                "margin_mm": {"F.Mask": 0.1},
            },
            other: {
                "kind": "segment",
                "net": "N",
                "layers_geometry": {"F.Cu": trace},
                "margin_mm": {},
            },
        },
    )
    return geometry, owner, other


def test_same_net_and_overlapped_neighbor_remain_independent():
    from kicad_tools.validate.mask_copper import assess_attributed_geometry

    geometry, _, other = toy_geometry(overlap_owner=True)
    result = assess_attributed_geometry(geometry, MaskCopperPolicy(0.1, "order", "process", "rev"))
    assert not result.passed
    assert result.measurements[0].conductor_uuid == other
    assert result.measurements[0].exposed_area_mm2 == pytest.approx(0.12, abs=0.000002)


@pytest.mark.parametrize("mode", ["valid", "stale", "wrong_uuid", "wrong_layer", "disconnected"])
def test_intent_requires_exact_binding_identity_layer_and_direct_connection(mode):
    from dataclasses import replace

    from kicad_tools.validate.mask_copper import (
        MaskEscapeIntent,
        assess_attributed_geometry,
        source_binding,
    )

    geometry, owner, other = toy_geometry(overlap_owner=mode != "disconnected", conductor_x=1.05)
    binding = source_binding(geometry)
    if mode == "stale":
        binding = replace(binding, source_sha256="b" * 64)
    intent = MaskEscapeIntent(
        binding,
        owner,
        "unknown" if mode == "wrong_uuid" else other,
        "B.Mask" if mode == "wrong_layer" else "F.Mask",
        "reviewed connected escape",
    )
    result = assess_attributed_geometry(
        geometry, MaskCopperPolicy(0.1, "order", "process", "rev"), [intent]
    )
    assert result.passed is (mode == "valid")
    assert result.intent_audit[0]["accepted"] is (mode == "valid")
    assert result.measurements


@pytest.mark.parametrize(
    "gap,expected",
    [
        (0.1, "uncertain"),
        (0.100002, "uncertain"),
        (0.099998, "uncertain"),
        (0.099, "violation"),
        (0.101, None),
    ],
)
def test_exact_limit_and_construction_uncertainty_are_not_waivers(gap, expected):
    from kicad_tools.validate.mask_copper import assess_attributed_geometry

    geometry, _, _ = toy_geometry(conductor_x=1.1 + gap)
    result = assess_attributed_geometry(geometry, MaskCopperPolicy(0.1, "order", "process", "rev"))
    assert ([m.disposition for m in result.measurements] or [None]) == [expected]
    assert result.passed is (expected is None)


def test_assessment_roundtrip_in_real_drc_report(tmp_path):
    import json

    from kicad_tools.drc.report import DRCReport

    assessment = MaskCopperAssessment(
        coverage="incomplete", reasons=["unsupported native material"]
    )
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "file": "board.kicad_pcb",
                "summary": {"passed": False},
                "violations": [],
                "mask_copper_assessments": [assessment.to_dict()],
            }
        )
    )
    report = DRCReport.load(path)
    assert not report.passed
    assert report.to_dict()["mask_copper_assessments"] == [assessment.to_dict()]
