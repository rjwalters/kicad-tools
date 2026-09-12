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


def test_report_filter_does_not_erase_incomplete_coverage():
    from kicad_tools.drc.report import DRCReport

    report = DRCReport(
        "", None, "board", mask_copper_assessments=[MaskCopperAssessment(reasons=["policy absent"])]
    )
    filtered = report.apply_filters([])
    assert not filtered.passed
    assert filtered.mask_copper_assessments == report.mask_copper_assessments


def test_checker_deleted_saved_source_is_typed_incomplete(tmp_path):
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    path = tmp_path / "board.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(path)
    checker = DRCChecker(PCB.load(path))
    path.unlink()
    result = checker.check_mask_to_copper()
    assert not result.passed
    assert result.mask_copper_assessments[0].coverage == "incomplete"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {
            "schema": "kct.mask-copper-request.v1",
            "intents": [
                {
                    "binding": {
                        "source_sha256": "a" * 64,
                        "project_sha256": None,
                        "rules_sha256": None,
                        "export_profile_sha256": "b" * 64,
                    },
                    "owner_uuid": "owner",
                    "conductor_uuid": "conductor",
                    "mask_side": "F.Mask",
                    "rationale": None,
                }
            ],
        },
    ],
)
def test_malformed_request_rejected_cleanly(tmp_path, payload):
    import json

    from kicad_tools.validate.mask_copper import MaskCopperRequest

    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        MaskCopperRequest.from_file(path)


@pytest.mark.parametrize("flags", [["--only", "dimensions"], ["--skip", "mask_to_copper"]])
def test_explicit_request_cannot_be_silently_excluded(tmp_path, capsys, flags):
    import json

    from kicad_tools.cli.check_cmd import main
    from kicad_tools.schema.pcb import PCB

    path = tmp_path / "board.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(path)
    config = tmp_path / "request.json"
    config.write_text(json.dumps({"schema": "kct.mask-copper-request.v1"}))
    assert main([str(path), "--drc-only", "--mask-copper-config", str(config), *flags]) == 1
    assert "conflicts" in capsys.readouterr().err


def test_info_finding_cannot_hide_incomplete_human_headline(tmp_path, capsys):
    from kicad_tools.cli.check_cmd import output_table
    from kicad_tools.validate.violations import DRCViolation

    finding = DRCViolation("example", "info", "advisory")
    results = DRCResults(
        [finding], mask_copper_assessments=[MaskCopperAssessment(reasons=["native absent"])]
    )
    output_table([finding], results, tmp_path / "board.kicad_pcb", "jlcpcb", 2, False)
    text = capsys.readouterr().out
    assert "DRC PASSED" not in text
    assert "NOT QUALIFIED" in text


@pytest.mark.parametrize("field", ["project_sha256", "rules_sha256", "export_profile"])
def test_sidecar_and_plot_bindings_invalidate_escape_intent(field):
    from kicad_tools.validate.mask_copper import (
        MaskEscapeIntent,
        assess_attributed_geometry,
        source_binding,
    )

    geometry, owner, other = toy_geometry(overlap_owner=True)
    intent = MaskEscapeIntent(source_binding(geometry), owner, other, "F.Mask", "reviewed escape")
    if field == "export_profile":
        geometry.exported.export_identity["variables"] = [("CONTEXT", "changed")]
    else:
        setattr(geometry.exported, field, "f" * 64)
    result = assess_attributed_geometry(
        geometry, MaskCopperPolicy(0.1, "order", "process", "rev"), [intent]
    )
    assert not result.passed
    assert not result.intent_audit[0]["accepted"]
    assert result.measurements[0].disposition == "violation"


@pytest.mark.parametrize("change", ["missing", "coordinate", "layer", "duplicate"])
def test_native_worker_data_validation_is_fail_visible(change):
    import json

    from kicad_tools.validate.mask_copper_geometry import _read_worker_output

    item = {
        "kind": "PAD",
        "net": "N",
        "margin_mm": {},
        "layers": {"F.Cu": [{"shell": [[0, 0], [1, 0], [0, 1]], "holes": []}]},
    }
    data = {
        "schema": "kct.native-mask-objects.v1",
        "max_error_mm": 0.005,
        "errors": [],
        "objects": {"one": item},
    }
    if change == "missing":
        data["objects"] = {}
        assert _read_worker_output(json.dumps(data), {"one"})["errors"]
        return
    if change == "coordinate":
        item["layers"]["F.Cu"][0]["shell"][0][0] = float("nan")
    elif change == "layer":
        item["layers"]["unknown"] = []
    else:
        with pytest.raises(ValueError, match="Duplicate"):
            _read_worker_output('{"objects":{},"objects":{}}', {"one"})
        return
    with pytest.raises(ValueError):
        _read_worker_output(json.dumps(data), {"one"})


@pytest.mark.parametrize(
    "gap,expected",
    [(0.089, "violation"), (0.095, "uncertain"), (0.107, "uncertain"), (0.112, None)],
)
def test_two_independent_native_construction_errors_bound_clearance(gap, expected):
    from kicad_tools.validate.mask_copper import assess_attributed_geometry

    geometry, _, _ = toy_geometry(conductor_x=1.1 + gap)
    geometry.provenance["max_error_mm_per_construction"] = 0.005
    result = assess_attributed_geometry(
        geometry, MaskCopperPolicy(0.1, "specification", "process", "revision")
    )
    assert ([m.disposition for m in result.measurements] or [None]) == [expected]
    assert result.passed is (expected is None)


def test_checker_rejects_source_replaced_between_preflight_and_native_capture(
    tmp_path, monkeypatch
):
    import hashlib

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker
    from kicad_tools.validate import mask_copper

    path = tmp_path / "board.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(path)
    checker = DRCChecker(PCB.load(path))

    def replace_source_during_check(source, *args, **kwargs):
        source.write_bytes(source.read_bytes() + b"\n")
        return MaskCopperAssessment(
            coverage="complete",
            policy=MaskCopperPolicy(0.1, "spec", "process", "revision"),
            binding=mask_copper.MaskSourceBinding(
                hashlib.sha256(source.read_bytes()).hexdigest(), None, None, "a" * 64
            ),
        )

    monkeypatch.setattr(mask_copper, "check_mask_to_copper", replace_source_during_check)
    result = checker.check_mask_to_copper()
    assert not result.passed
    assessment = result.mask_copper_assessments[0]
    assert assessment.coverage == "incomplete"
    assert "Source changed after checker object validation" in assessment.reasons
