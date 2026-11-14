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
    # Issue #8914: a check that could not run no longer serializes as zero
    # findings -- that silence was read as "kct sees no exposure here" in a
    # rule-by-rule comparison against JLCDFM.  Info severity, so the verdict
    # above is unchanged.
    assert [(v["rule_id"], v["severity"]) for v in data["violations"]] == [
        ("mask_to_copper_incomplete", "info")
    ]
    assert "policy" in data["violations"][0]["message"]
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


@pytest.mark.parametrize(
    ("coverage", "reasons"),
    [
        ("incomplete", ["native unavailable"]),
        ("incomplete", []),
        ("not_run", ["An explicit process-specific mask-to-copper policy is required"]),
    ],
)
def test_incomplete_coverage_is_reported_as_a_finding(coverage, reasons):
    """Issue #8914: "never ran" must not be indistinguishable from "found nothing"."""
    from kicad_tools.validate.mask_copper import assessment_results

    found = assessment_results(
        MaskCopperAssessment(coverage=coverage, reasons=list(reasons))
    ).violations
    assert [(v.rule_id, v.severity) for v in found] == [("mask_to_copper_incomplete", "info")]
    assert coverage in found[0].message
    for reason in reasons:
        assert reason in found[0].message
    if not reasons:
        assert "no reason recorded" in found[0].message


def test_complete_assessment_with_no_findings_reports_nothing():
    """The other half of #8914: a run that did happen and found nothing stays silent."""
    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    geometry, _, _ = toy_geometry(conductor_x=1.1 + 0.2)
    policy = MaskCopperPolicy(0.09, "order", "process", "rev", warning_clearance_mm=0.15)
    assessment = assess_attributed_geometry(geometry, policy)
    assert assessment.coverage == "complete"
    assert not assessment.measurements
    assert not assessment_results(assessment).violations


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


# ---------------------------------------------------------------------------
# Issue #8924: coverage.mask_to_copper must reflect the assessment's own
# ``coverage``, never report ``checked`` for an assessment that could not
# evaluate the board.
# ---------------------------------------------------------------------------


def _patch_assessment(monkeypatch, **kwargs):
    """Make the checker's native call return a controlled assessment.

    The row under test must not depend on whether *this host* has a qualified
    KiCad plot profile: CI's pinned image reports ``10.0.6`` (qualified) while
    a distro build reports ``10.0.6-10.0.6~ubuntu24.04.1`` (not qualified), so
    a test that drove the real native path would assert opposite states on the
    two. ``check_mask_to_copper`` is imported inside
    :meth:`DRCChecker.check_mask_to_copper`, so patching the module attribute
    takes effect at call time.
    """

    def fake(path, policy=None, intents=(), **native_options):
        return MaskCopperAssessment(policy=policy, **kwargs)

    monkeypatch.setattr("kicad_tools.validate.mask_copper.check_mask_to_copper", fake)


def _run_mfr_check(tmp_path, capsys, *extra):
    import json

    from kicad_tools.cli.check_cmd import main
    from kicad_tools.schema.pcb import PCB

    board = tmp_path / "b.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(board)
    main([str(board), "--drc-only", "--format", "json", "--mfr", "jlcpcb", *extra])
    out = capsys.readouterr().out
    return json.loads(out[out.index("{") :])


@pytest.mark.parametrize(
    ("coverage", "state"),
    [("complete", "checked"), ("incomplete", "unknown"), ("not_run", "unknown")],
)
def test_coverage_row_follows_assessment_coverage(tmp_path, capsys, monkeypatch, coverage, state):
    """AC1/AC2 + the ``not_run`` mapping: the row tracks ``assessment.coverage``."""
    _patch_assessment(monkeypatch, coverage=coverage, reasons=["native unavailable"])
    data = _run_mfr_check(tmp_path, capsys)

    assert data["mask_copper_assessments"][0]["coverage"] == coverage
    row = data["coverage"]["mask_to_copper"]
    assert row["state"] == state
    assert row["blocking"] is True
    # Blocking propagation: an unevaluated blocking category must reach the
    # summary gates read by ``kct readiness`` / ``/kct:tapeout``.
    blocking_unknown = data["summary"]["coverage_blocking_unknown"]
    assert ("mask_to_copper" in blocking_unknown) == (state == "unknown")


def test_incomplete_row_carries_the_assessment_reasons(tmp_path, capsys, monkeypatch):
    """Reason plumbing: the row explains itself from the assessment's reasons."""
    _patch_assessment(
        monkeypatch,
        coverage="incomplete",
        reasons=["F.Cu: Invalid region contour", "B.Cu: Invalid region contour"],
    )
    row = _run_mfr_check(tmp_path, capsys)["coverage"]["mask_to_copper"]

    assert row["reason"] and "Invalid region contour" in row["reason"]
    assert row["status"].startswith("unknown:")
    assert "incomplete" in row["reason"]


def test_checked_row_has_no_reason(tmp_path, capsys, monkeypatch):
    _patch_assessment(monkeypatch, coverage="complete")
    row = _run_mfr_check(tmp_path, capsys)["coverage"]["mask_to_copper"]

    assert row == {"status": "checked", "state": "checked", "reason": None, "blocking": True}


def test_coverage_row_keys_on_coverage_never_on_passed(tmp_path, capsys, monkeypatch):
    """The regression test that separates a correct fix from one keyed on ``passed``.

    ``assessment.passed`` is strictly stronger than ``coverage == "complete"``:
    it also requires empty reasons and no violation/uncertain measurement.  A
    board whose mask check ran fine and *found real violations* is a
    ``checked`` row carrying error findings -- reporting ``unknown`` there
    would invert the row's meaning.
    """
    from kicad_tools.validate.mask_copper import MaskCopperMeasurement

    violation = MaskCopperMeasurement(
        opening_uuids=("00000000-0000-0000-0000-000000000001",),
        conductor_uuid="00000000-0000-0000-0000-000000000002",
        mask_side="F.Mask",
        copper_layer="F.Cu",
        location_mm=(1.0, 2.0),
        clearance_mm=0.01,
        exposed_area_mm2=0.5,
        relation="overlap",
        disposition="violation",
        uncertainty_mm=0.0,
        opening_kind="pad:rect",
        mask_defined=False,
        margin_provenance="native",
    )
    _patch_assessment(monkeypatch, coverage="complete", measurements=[violation])
    data = _run_mfr_check(tmp_path, capsys, "--allow-incomplete")

    assert data["mask_copper_assessments"][0]["passed"] is False
    assert data["coverage"]["mask_to_copper"]["state"] == "checked"
    assert "mask_to_copper" not in data["summary"]["coverage_blocking_unknown"]
    errors = [
        v
        for v in data["violations"]
        if v["rule_id"] == "mask_to_copper" and v["severity"] == "error"
    ]
    assert errors, "the violation must still surface as an error finding"


def test_only_mask_to_copper_without_policy_is_unknown_not_checked(tmp_path, capsys):
    """End-to-end ``not_run``: requested with no policy, so it never ran.

    No monkeypatching -- ``--only mask_to_copper`` with no ``--mfr`` leaves the
    request without a policy, and ``check_mask_to_copper`` refuses rather than
    evaluating.  A *requested* check that never ran is not a pass.
    """
    import json

    from kicad_tools.cli.check_cmd import main
    from kicad_tools.schema.pcb import PCB

    board = tmp_path / "b.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(board)
    main([str(board), "--drc-only", "--format", "json", "--only", "mask_to_copper"])
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])

    assert data["mask_copper_assessments"][0]["coverage"] == "not_run"
    assert data["coverage"]["mask_to_copper"]["state"] == "unknown"
    assert "policy" in data["coverage"]["mask_to_copper"]["reason"]


def test_opt_in_skip_rows_are_unchanged(tmp_path, capsys):
    """The pre-run ``skipped`` branches must not be touched by this change."""
    import json

    from kicad_tools.cli.check_cmd import main
    from kicad_tools.schema.pcb import PCB

    board = tmp_path / "b.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(board)

    def run(*extra):
        main([str(board), "--drc-only", "--format", "json", *extra])
        out = capsys.readouterr().out
        return json.loads(out[out.index("{") :])["coverage"]["mask_to_copper"]

    # Both land on the pre-run opt-in branch: ``--skip`` suppresses #8842's
    # default request, so the category is never requested in the first place
    # and the loop's opt-in ``continue`` fires before the ``--skip`` branch.
    assert run()["status"] == "skipped:opt_in:--mask-copper-config"
    assert (
        run("--mfr", "jlcpcb", "--skip", "mask_to_copper")["status"]
        == "skipped:opt_in:--mask-copper-config"
    )


def test_mask_copper_unknown_deduplicates_and_truncates_reasons():
    """The live incomplete case has 19 reasons, 13 identical; reports land in files."""
    from kicad_tools.validate import coverage as cov

    repeated = ["F.Mask: unattributed native mask region"] * 13
    result = cov.mask_copper_unknown("incomplete", [*repeated, "a", "b", "c", "d"])

    assert result.state == cov.UNKNOWN and result.blocking
    assert result.reason.count("unattributed native mask region") == 1
    assert "(+2 more)" in result.reason
    assert len(result.reason) < 200


def test_mask_copper_unknown_tolerates_no_reasons():
    from kicad_tools.validate import coverage as cov

    result = cov.mask_copper_unknown("not_run", [])
    assert result.state == cov.UNKNOWN and "not_run" in result.reason
