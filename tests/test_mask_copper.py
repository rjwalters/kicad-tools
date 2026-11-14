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


@pytest.mark.parametrize(
    "conductor_x,exposed_area_mm2",
    [(1.05, 0.01), (1.08, 0.004)],
)
def test_same_net_conductor_inside_the_opening_is_flagged_without_overlapping(
    conductor_x, exposed_area_mm2
):
    """A same-net trace inside a pad's mask opening is an error (issues #8914, #8932).

    This is the shape JLCDFM reports as danger-level "solder mask opening
    exposing trace": a track passing an SMD pad of *its own net* within the
    pad's mask margin, without overlapping the pad. It is measured exactly like
    cross-net exposure -- the measurement loop never reads ``net``, whose only
    gate is the ``MaskEscapeIntent`` path. Guards against a same-net exclusion
    being reintroduced into that loop, which would silently drop the
    measurement and the finding while still reporting complete coverage.
    """
    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    geometry, _, other = toy_geometry(conductor_x=conductor_x, overlap_owner=False)
    result = assess_attributed_geometry(
        geometry,
        # Interim JLCPCB values written literally: a future threshold table
        # must not silently move what this test asserts.
        MaskCopperPolicy(
            clearance_mm=0.09,
            source="order",
            process="process",
            revision="rev",
            warning_clearance_mm=0.15,
        ),
    )
    assert result.coverage == "complete"
    assert len(result.measurements) == 1
    measurement = result.measurements[0]
    assert measurement.conductor_uuid == other
    assert measurement.disposition == "violation"
    assert measurement.exposed_area_mm2 == pytest.approx(exposed_area_mm2, abs=0.000002)
    assert [(v.rule_id, v.severity) for v in assessment_results(result).violations] == [
        ("mask_to_copper", "error")
    ]


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
    from kicad_tools.validate import DRCChecker, mask_copper

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


def test_public_cli_forwards_mask_config_and_preserves_incomplete(tmp_path, capsys):
    import json

    from kicad_tools.cli import main
    from kicad_tools.schema.pcb import PCB

    board = tmp_path / "source board.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(board)
    before = board.read_bytes()
    config = tmp_path / "mask request.json"
    config.write_text('{"schema": "kct.mask-copper-request.v1"}')
    result = main(
        [
            "check",
            str(board),
            "--mask-copper-config",
            str(config),
            "--drc-only",
            "--format",
            "json",
        ]
    )
    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert output["summary"]["passed"] is False
    assessment = output["mask_copper_assessments"][0]
    assert assessment["coverage"] == "not_run"
    assert "explicit process-specific" in assessment["reasons"][0]
    assert board.read_bytes() == before


@pytest.mark.parametrize(
    "gap,expected,passed",
    [
        (0.05, "violation", False),
        (0.12, "warning", True),
        (0.2, None, True),
    ],
)
def test_warning_tier_does_not_fail_verdict(gap, expected, passed):
    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    geometry, _, _ = toy_geometry(conductor_x=1.1 + gap)
    policy = MaskCopperPolicy(0.09, "order", "process", "rev", warning_clearance_mm=0.15)
    result = assess_attributed_geometry(geometry, policy)
    assert ([m.disposition for m in result.measurements] or [None]) == [expected]
    assert result.passed is passed
    severities = {v.severity for v in assessment_results(result).violations}
    assert severities <= (
        {"warning"} if expected == "warning" else {"error"} if expected else set()
    )


def test_warning_clearance_must_not_be_below_clearance():
    with pytest.raises(ValueError):
        MaskCopperPolicy(0.1, "s", "p", "r", warning_clearance_mm=0.05)


def test_default_request_is_jlcpcb_only_and_cited():
    from kicad_tools.validate.mask_copper import default_request_for_manufacturer

    request = default_request_for_manufacturer("jlcpcb")
    assert request.policy.clearance_mm == 0.09
    assert request.policy.warning_clearance_mm == 0.15
    assert default_request_for_manufacturer("jlcpcb-tier1") is not None
    assert default_request_for_manufacturer("pcbway") is None
    assert default_request_for_manufacturer(None) is None


def test_cli_mfr_runs_mask_to_copper_by_default_but_not_without_mfr(tmp_path, capsys):
    import json

    from kicad_tools.cli import main
    from kicad_tools.schema.pcb import PCB

    board = tmp_path / "b.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(board)

    def run(*extra):
        main(["check", str(board), "--drc-only", "--format", "json", *extra])
        return json.loads(capsys.readouterr().out)

    with_mfr = run("--mfr", "jlcpcb")
    assessments = with_mfr.get("mask_copper_assessments")
    assert assessments, "mask_to_copper must run by default under --mfr"
    assert assessments[0]["policy"]["clearance_mm"] == 0.09
    assert assessments[0]["coverage"] != "not_run" or assessments[0]["reasons"]

    assert not run("--mfr", "jlcpcb", "--skip", "mask_to_copper").get("mask_copper_assessments")
    assert not run().get("mask_copper_assessments")


def spread_geometry(near_gap, *, far_conductor_count=40, far_spacing=5.0):
    """Owner pad with one near conductor and many deliberately distant ones.

    The distant conductors exist so the bounding-box prefilter in
    :func:`assess_attributed_geometry` (issue #8901) has something to prune:
    only the conductor at ``near_gap`` from the mask opening's right edge can
    be inside the policy's reach.
    """
    from shapely.geometry import box
    from shapely.ops import unary_union

    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    owner = "00000000-0000-0000-0000-000000000001"
    near = "00000000-0000-0000-0000-000000000002"
    pad = box(0, 0, 1, 1)
    opening = box(-0.1, -0.1, 1.1, 1.1)
    objects = {
        owner: {
            "kind": "pad:rect",
            "net": "N",
            "layers_geometry": {"F.Mask": opening, "F.Cu": pad},
            "margin_mm": {"F.Mask": 0.1},
        },
        near: {
            "kind": "segment",
            "net": "N",
            "layers_geometry": {"F.Cu": box(1.1 + near_gap, 0.4, 1.6 + near_gap, 0.6)},
            "margin_mm": {},
        },
    }
    copper = [item["layers_geometry"]["F.Cu"] for item in objects.values()]
    for index in range(far_conductor_count):
        start = 10.0 + index * far_spacing
        shape = box(start, 0.4, start + 1.0, 0.6)
        objects[f"00000000-0000-0000-0000-{index + 3:012d}"] = {
            "kind": "segment",
            "net": "F",
            "layers_geometry": {"F.Cu": shape},
            "margin_mm": {},
        }
        copper.append(shape)
    exported = ExportedMaskGeometry(
        "a" * 64,
        None,
        None,
        "10.0.5",
        {},
        layers={"F.Mask": opening, "F.Cu": unary_union(copper)},
    )
    return AttributedMaskGeometry(exported, objects), owner, near


@pytest.mark.parametrize(
    ("near_gap", "measured"),
    [(0.05, True), (0.5, False)],
)
def test_distance_prefilter_keeps_every_in_reach_pair_and_the_pair_count(near_gap, measured):
    """The bbox prefilter may only drop pairs the exact distance would reject.

    Issue #8901: the pairwise scan was an unindexed ``openings x objects``
    sweep, 170 s of one board-03 ``kct check``. Pruning by envelope must not
    change which pairs are measured, and ``evaluated_pairs`` must still report
    the whole comparison set, not just the surviving candidates.
    """
    from kicad_tools.validate.mask_copper import assess_attributed_geometry

    geometry, owner, near = spread_geometry(near_gap)
    result = assess_attributed_geometry(geometry, MaskCopperPolicy(0.1, "order", "process", "rev"))

    # One opening, every conductor on F.Cu but the owner's own contribution.
    assert result.evaluated_pairs == len(geometry.objects) - 1
    assert [m.conductor_uuid for m in result.measurements] == ([near] if measured else [])
    assert all(m.opening_uuids == (owner,) for m in result.measurements)
