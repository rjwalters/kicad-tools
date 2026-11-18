"""Source-qualified mask exposure and public coverage controls."""

import re
from pathlib import Path

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


def toy_geometry(
    conductor_x=1.2,
    *,
    overlap_owner=False,
    merged=False,
    conductor_kind="segment",
    owner_net="N",
    conductor_net="N",
):
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
                "net": owner_net,
                "layers_geometry": {"F.Mask": opening, "F.Cu": pad},
                "margin_mm": {"F.Mask": 0.1},
            },
            other: {
                "kind": conductor_kind,
                "net": conductor_net,
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
        ("mask_to_copper", "error"),
        ("mask_to_copper_openings", "info"),
    ]


@pytest.mark.parametrize(
    "owner_net,conductor_net,expected_nets",
    [
        ("N", "N", ("N",)),
        ("P", "C", ("C", "P")),
        ("N", None, ("N",)),
    ],
)
def test_violation_names_its_nets_for_identity_baselines(owner_net, conductor_net, expected_nets):
    """Issue #8858: each mask_to_copper record names the owner pad's and the
    exposed conductor's nets, so the synthetic-fixture CI gates can baseline
    findings by the regeneration-stable net signature -- their ``items``
    are opening/conductor UUIDs the native pass re-mints every run."""
    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    geometry, _, _ = toy_geometry(
        conductor_x=1.05,
        overlap_owner=False,
        owner_net=owner_net,
        conductor_net=conductor_net,
    )
    result = assess_attributed_geometry(
        geometry,
        MaskCopperPolicy(
            clearance_mm=0.09,
            source="order",
            process="process",
            revision="rev",
            warning_clearance_mm=0.15,
        ),
    )
    violations = [v for v in assessment_results(result).violations if v.rule_id == "mask_to_copper"]
    assert len(violations) == 1
    assert violations[0].severity == "error"
    assert violations[0].nets == expected_nets


@pytest.mark.parametrize(
    "conductor_kind,mode",
    [
        (kind, mode)
        for kind in ("segment", "zone")
        for mode in (
            "valid",
            "stale",
            "wrong_uuid",
            "wrong_layer",
            "disconnected",
            "different_net",
            "unidentified_owner_net_none",
            "unidentified_owner_net_empty",
        )
    ],
)
def test_intent_requires_exact_binding_identity_layer_and_direct_connection(conductor_kind, mode):
    from dataclasses import replace

    from kicad_tools.validate.mask_copper import (
        MaskEscapeIntent,
        assess_attributed_geometry,
        source_binding,
    )

    # Geometry stays directly connected for the net modes so that only the
    # net check can reject the declaration.
    net_kwargs = {
        "different_net": {"conductor_net": "M"},
        "unidentified_owner_net_none": {"owner_net": None},
        "unidentified_owner_net_empty": {"owner_net": ""},
    }.get(mode, {})
    geometry, owner, other = toy_geometry(
        overlap_owner=mode != "disconnected",
        conductor_x=1.05,
        conductor_kind=conductor_kind,
        **net_kwargs,
    )
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
    conductor_measurements = [m for m in result.measurements if m.conductor_uuid == other]
    assert conductor_measurements
    if mode == "valid":
        assert conductor_measurements[0].disposition == "intentional"
    else:
        # A rejected declaration never suppresses the measurement it names.
        assert conductor_measurements[0].disposition == "violation"
    if net_kwargs:
        assert result.intent_audit[0]["reason"] == "connection must share an identified net"
        # A rejected declaration must not suppress the exposure it names.
        assert [m.disposition for m in result.measurements] == ["violation"]


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


def _managed_constraint_names() -> set[str]:
    """Constraint names kct's writers emit into managed ``.kicad_dru`` text.

    Sources are the four writers that build a board's managed ``.kicad_dru``:
    ``dru_generator.py`` and the shipped tier files under
    ``manufacturers/rules/`` (#8948), the router diff-pair block (#6443) and
    the keepout block (#6039) -- the latter two derived by rendering the
    same functions the writers call, in both pair shapes kct emits
    (KiCad-recognised and router-recorded, #6429).
    """
    manufacturers = Path(__file__).resolve().parent.parent / "src" / "kicad_tools" / "manufacturers"
    text = (manufacturers / "dru_generator.py").read_text(encoding="utf-8")
    names = set(re.findall(r"\(constraint\s+([a-z_]+)", text))
    for tier in sorted((manufacturers / "rules").glob("*.kicad_dru")):
        names |= set(re.findall(r"\(constraint\s+([a-z_]+)", tier.read_text(encoding="utf-8")))
    from kicad_tools.manufacturers.keepout_dru import KeepoutDruRule
    from kicad_tools.manufacturers.netclass_dru import (
        PairLimits,
        render_pair_gap_rule,
        render_pair_limit_rules,
    )

    rendered = [render_pair_gap_rule("A_P", "A_N", 0.2)]
    rendered += render_pair_limit_rules(
        PairLimits(p="A_P", n="A_N", uncoupled_mm=5.0, skew_mm=0.5, max_vias=4)
    )
    rendered += render_pair_limit_rules(PairLimits(p="X_POS", n="X_NEG", skew_mm=1.0, max_vias=6))
    rendered.append(
        KeepoutDruRule(
            name="kct keepout all",
            reference="u",
            disallow=("track", "via", "pad", "zone", "footprint"),
        ).render()
    )
    for chunk in rendered:
        names |= set(re.findall(r"\(constraint\s+([a-z_]+)", chunk))
    return names


def test_rule_context_allowlist_admits_managed_vocabulary_and_rejects_plot_shaping():
    """Pin the hoisted native rule-context allowlist without a native interpreter.

    The whole constraint vocabulary kct writes into a managed ``.kicad_dru``
    -- fab floors and tier files (#8948), diff-pair gap and pair limits
    (#6443) and keepout ``disallow`` rules (#6039) -- must be admitted, so a
    board carrying only kct's own output never reports ``Native object rule
    context unsupported``. The expectation side is derived at test time from
    the same writers, so a name newly emitted by any of them turns the first
    assertion red until its geometry-neutrality is argued and parity-tested,
    which is the friction the allowlist exists to enforce. The plot-shaping
    zone-fill constraints and the mask/paste-shaping names must stay outside
    either way.
    """
    from kicad_tools.validate.mask_copper_geometry import SAFE_RULE_CONSTRAINTS

    emitted_by_kct = _managed_constraint_names()
    assert emitted_by_kct, "managed derivation found no constraints; did sources move?"
    assert emitted_by_kct <= SAFE_RULE_CONSTRAINTS
    assert not SAFE_RULE_CONSTRAINTS & {
        "zone_connection",
        "thermal_relief_gap",
        "thermal_spoke_width",
        "min_resolved_spokes",
        "bridged_mask",
        "solder_mask_expansion",
        "solder_mask_sliver",
        "solder_paste_abs_margin",
        "solder_paste_rel_margin",
    }


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
        {"warning", "info"} if expected == "warning" else {"error", "info"} if expected else set()
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


# ---------------------------------------------------------------------------
# Issue #8949: the CLI's human-readable ``Mask-to-copper:`` stdout stanza
# must collapse repeated reasons (one shared collapse rule with the
# coverage row) and print one actionable remediation line.
# ---------------------------------------------------------------------------


def test_collapse_reasons_is_the_shared_dedup_truncate_rule():
    """One collapse rule, two consumers: coverage row and stdout stanza."""
    from kicad_tools.validate.coverage import collapse_reasons

    repeated = ["F.Mask: unattributed native mask region"] * 13
    collapsed = collapse_reasons([*repeated, "a", "b", "c", "d"], limit=3)
    assert collapsed == ("F.Mask: unattributed native mask region; a; b (+2 more)")
    # First-occurrence order is preserved.
    assert collapsed.index("F.Mask") < collapsed.index("a")
    # Exactly one reason: no spurious suffix.
    assert collapse_reasons(["only"], limit=3) == "only"
    # No reasons at all: a placeholder, never an empty string or "(+0 more)".
    assert collapse_reasons([], limit=3) == "no reason reported"
    assert "(+0 more)" not in collapse_reasons(["x", "x"], limit=5)


def test_incomplete_assessment_carries_a_remediation_hint():
    """AC: an incomplete assessment names what to configure, not just what failed."""
    incomplete = MaskCopperAssessment(
        coverage="incomplete", reasons=["F.Mask: unattributed native mask region"]
    )
    assert incomplete.remediation is not None
    assert "KCT_KICAD_DOCKER" in incomplete.remediation
    # A complete assessment needs no remediation; not_run and incomplete do
    # (the dataclass default is ``not_run``, which is exactly "never ran").
    assert MaskCopperAssessment(coverage="complete").remediation is None
    assert MaskCopperAssessment().remediation is not None
    # And the hint must never enter the serialized JSON payload: that dict
    # is round-trip validated against kct.mask-copper-assessment.v1.
    assert "remediation" not in incomplete.to_dict()


def _run_mask_stdout(tmp_path, capsys, monkeypatch, reasons, *extra):
    """Run a --drc-only human-format check with a controlled incomplete assessment."""
    from kicad_tools.cli.check_cmd import main
    from kicad_tools.schema.pcb import PCB

    _patch_assessment(monkeypatch, coverage="incomplete", reasons=list(reasons))
    board = tmp_path / "b.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(board)
    main([str(board), "--drc-only", "--mfr", "jlcpcb", *extra])
    return capsys.readouterr().out


def _mask_stdout_stanza(out: str) -> str:
    """The ``Mask-to-copper:`` stdout stanza, through its Remediation line.

    Scoped because the ``mask_to_copper_incomplete`` info *finding* (which
    ships the full un-collapsed reasons list inside its message, in the
    violations table and the JSON report) repeats the same strings.
    """
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Mask-to-copper:"))
    stanza = []
    for line in lines[start:]:
        stanza.append(line)
        if line.strip().startswith("Remediation:"):
            break
    else:
        raise AssertionError("no Remediation line after the Mask-to-copper header")
    return "\n".join(stanza)


def test_stdout_collapses_repeated_reasons_and_shows_distinct_causes(tmp_path, capsys, monkeypatch):
    """Hundreds of repeats collapse to one bounded line; distinct causes survive.

    Mirrors the reporter's run: 13 repeats of one line plus enough distinct
    causes to exceed the stdout bound, so both the collapse and the
    ``(+N more)`` remainder are exercised.
    """
    repeated = ["F.Mask: unattributed native mask region"] * 13
    distinct = [f"cause {i}" for i in range(9)]
    out = _run_mask_stdout(tmp_path, capsys, monkeypatch, [*repeated, *distinct])
    stanza = _mask_stdout_stanza(out)

    headers = [line for line in stanza.splitlines() if line.startswith("Mask-to-copper:")]
    assert len(headers) == 1
    assert "incomplete" in headers[0]
    # The repeated reason appears exactly once inside the stanza.
    assert stanza.count("F.Mask: unattributed native mask region") == 1
    # Distinct causes up to the bound are visible, the remainder counted.
    assert "cause 0" in stanza and "cause 7" not in stanza
    assert "(+2 more)" in stanza


def test_stdout_complete_assessment_has_no_placeholder_line(tmp_path, capsys, monkeypatch):
    """Issue #8991: ``complete`` + no reasons prints only the header line."""
    from kicad_tools.cli.check_cmd import main
    from kicad_tools.schema.pcb import PCB

    _patch_assessment(monkeypatch, coverage="complete", reasons=[])
    board = tmp_path / "b.kicad_pcb"
    PCB.create(width=10, height=10, layers=2).save(board)
    main([str(board), "--drc-only", "--mfr", "jlcpcb"])
    lines = capsys.readouterr().out.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("Mask-to-copper: complete"))
    following = lines[idx + 1] if idx + 1 < len(lines) else ""
    assert "no reason reported" not in following
    assert "Remediation:" not in following
    assert not any("no reason reported" in ln for ln in lines)


def test_stdout_single_and_empty_reason_lists_stay_clean(tmp_path, capsys, monkeypatch):
    """Edge cases: exactly one reason (no spurious count), no reasons at all."""
    stanza = _mask_stdout_stanza(
        _run_mask_stdout(tmp_path, capsys, monkeypatch, ["F.Cu: Invalid region contour"])
    )
    assert stanza.count("F.Cu: Invalid region contour") == 1
    assert "(+" not in stanza
    assert "Remediation:" in stanza

    stanza = _mask_stdout_stanza(_run_mask_stdout(tmp_path, capsys, monkeypatch, []))
    assert "no reason reported" in stanza
    assert "Remediation:" in stanza


# ---------------------------------------------------------------------------
# Issue #9038: findings must be triageable (owner, copper kind, net relation)
# and must not read as board defects while the assessment is incomplete.
# ---------------------------------------------------------------------------


_POLICY = MaskCopperPolicy(0.09, "order", "process", "rev")


def _violation(**extra):
    from kicad_tools.validate.mask_copper import MaskCopperMeasurement

    fields = {
        "opening_uuids": ("00000000-0000-0000-0000-000000000001",),
        "conductor_uuid": "00000000-0000-0000-0000-000000000002",
        "mask_side": "F.Mask",
        "copper_layer": "F.Cu",
        "location_mm": (1.0, 2.0),
        "clearance_mm": 0.01,
        "exposed_area_mm2": 0.5,
        "relation": "overlap",
        "disposition": "violation",
        "uncertainty_mm": 0.0,
        "opening_kind": "pad:rect",
        "mask_defined": False,
        "margin_provenance": "native",
    }
    fields.update(extra)
    return MaskCopperMeasurement(**fields)


def _opening_geometry(pad, trace, *, pad_net="N", trace_net="N", trace_kind="segment"):
    """A pad with a mask opening and one conductor, both shapes given directly."""
    from shapely.geometry import box
    from shapely.ops import unary_union

    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    owner, other = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    opening = box(-0.1, -0.1, 1.1, 1.1)
    exported = ExportedMaskGeometry(
        "a" * 64,
        None,
        None,
        "10.0.5",
        {},
        layers={"F.Mask": opening, "F.Cu": unary_union([pad, trace])},
    )
    geometry = AttributedMaskGeometry(
        exported,
        {
            owner: {
                "kind": "pad:rect",
                "net": pad_net,
                "pad_ref": "R1/1",
                "layers_geometry": {"F.Mask": opening, "F.Cu": pad},
                "margin_mm": {"F.Mask": 0.1},
            },
            other: {
                "kind": trace_kind,
                "net": trace_net,
                "layers_geometry": {"F.Cu": trace},
                "margin_mm": {},
            },
        },
    )
    return geometry, owner, other


def _mask_findings(results):
    return [v for v in results.violations if v.rule_id == "mask_to_copper"]


@pytest.mark.parametrize("coverage", ["incomplete", "not_run"])
def test_incomplete_coverage_never_emits_error_severity_findings(coverage):
    """Issue #9038 AC: until coverage is complete, findings stay warnings."""
    from kicad_tools.validate.mask_copper import assessment_results

    assessment = MaskCopperAssessment(
        coverage=coverage,
        reasons=["native unavailable"],
        policy=_POLICY,
        measurements=[_violation()],
    )
    found = _mask_findings(assessment_results(assessment))
    assert [(v.rule_id, v.severity) for v in found] == [("mask_to_copper", "warning")]
    assert all("provisional: coverage incomplete" in v.message for v in found)


def test_complete_coverage_keeps_error_severity_without_provisional_mark():
    from kicad_tools.validate.mask_copper import assessment_results

    assessment = MaskCopperAssessment(
        coverage="complete",
        policy=_POLICY,
        measurements=[_violation()],
    )
    found = _mask_findings(assessment_results(assessment))
    assert [(v.rule_id, v.severity) for v in found] == [("mask_to_copper", "error")]
    assert all("provisional" not in v.message for v in found)


def test_incomplete_run_downgrades_geometry_measurements_to_provisional_warnings():
    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    geometry, _, _ = toy_geometry(overlap_owner=True)
    geometry.errors.append("F.Mask: unattributed native mask region")
    assessment = assess_attributed_geometry(geometry, _POLICY)
    assert assessment.coverage == "incomplete"
    found = _mask_findings(assessment_results(assessment))
    assert found
    assert all(v.severity == "warning" for v in found)
    assert all("provisional: coverage incomplete" in v.message for v in found)


def test_finding_message_names_owner_conductor_net_and_relation():
    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    geometry, owner, _ = toy_geometry(overlap_owner=True)
    geometry.objects[owner]["pad_ref"] = "R1/1"
    assessment = assess_attributed_geometry(geometry, _POLICY)
    found = _mask_findings(assessment_results(assessment))
    assert len(found) == 1
    message = found[0].message
    assert "opening pad:R1/1" in message
    assert "exposed copper segment" in message
    assert "net N" in message
    assert "same-net own-pad entry" in message


def test_finding_without_pad_ref_names_the_opening_kind_and_uuid():
    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    geometry, _, _ = toy_geometry(overlap_owner=True)
    assessment = assess_attributed_geometry(geometry, _POLICY)
    found = _mask_findings(assessment_results(assessment))
    assert len(found) == 1
    assert "opening pad:rect[00000000]" in found[0].message


def test_own_pad_entry_and_pass_through_are_distinguished():
    """The curator's rule: endpoint within the opening = own-pad entry."""
    from shapely.geometry import box

    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    # Trace terminates inside the opening (endpoint cap at x=0.5).
    geometry, _, _ = _opening_geometry(box(0, 0, 1, 1), box(0.5, 0.4, 2.0, 0.6))
    own = assess_attributed_geometry(geometry, _POLICY).measurements[0]
    assert own.same_net is True and own.own_pad_entry is True

    # Trace crosses the whole opening; both endpoints lie outside it.
    geometry, _, _ = _opening_geometry(box(0, 0, 1, 1), box(-0.2, 0.4, 1.3, 0.6))
    through = assess_attributed_geometry(geometry, _POLICY).measurements[0]
    assert through.same_net is True and through.own_pad_entry is False

    own_labels = [
        v.message
        for v in assessment_results(
            assess_attributed_geometry(
                _opening_geometry(box(0, 0, 1, 1), box(0.5, 0.4, 2.0, 0.6))[0], _POLICY
            )
        ).violations
        if v.rule_id == "mask_to_copper"
    ]
    through_labels = [
        v.message
        for v in assessment_results(
            assess_attributed_geometry(
                _opening_geometry(box(0, 0, 1, 1), box(-0.2, 0.4, 1.3, 0.6))[0], _POLICY
            )
        ).violations
        if v.rule_id == "mask_to_copper"
    ]
    assert "same-net own-pad entry" in own_labels[0]
    assert "same-net pass-through" in through_labels[0]


def test_via_inside_its_own_pad_opening_is_own_pad_entry():
    """A via has no trace endpoint; its center stands in for one (documented)."""
    from shapely.geometry import Point

    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    geometry, _, _ = _opening_geometry(
        Point(0.5, 0.5).buffer(1.0), Point(0.5, 0.5).buffer(0.15), trace_kind="via"
    )
    assessment = assess_attributed_geometry(geometry, _POLICY)
    assert assessment.measurements[0].conductor_kind == "via"
    assert assessment.measurements[0].own_pad_entry is True
    found = _mask_findings(assessment_results(assessment))
    assert "same-net own-pad entry" in found[0].message


def test_different_net_conductor_is_labelled_and_never_own_pad():
    from shapely.geometry import box

    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    geometry, _, _ = _opening_geometry(box(0, 0, 1, 1), box(0.5, 0.4, 2.0, 0.6), trace_net="M")
    measurement = assess_attributed_geometry(geometry, _POLICY).measurements[0]
    assert measurement.same_net is False and measurement.own_pad_entry is None
    found = _mask_findings(assessment_results(assess_attributed_geometry(geometry, _POLICY)))
    assert "different-net" in found[0].message
    assert "same-net" not in found[0].message


def test_summary_reports_per_opening_counts():
    """One pad with many findings reads as one opening (issue #9038 AC)."""
    from kicad_tools.validate.mask_copper import assessment_results

    assessment = MaskCopperAssessment(
        coverage="complete",
        policy=_POLICY,
        measurements=[
            _violation(opening_uuids=("u1",), owner_ref="R1/1"),
            _violation(
                opening_uuids=("u1",),
                conductor_uuid="00000000-0000-0000-0000-000000000003",
                owner_ref="R1/1",
            ),
            _violation(
                opening_uuids=("u2",),
                conductor_uuid="00000000-0000-0000-0000-000000000004",
                owner_ref="R2/1",
                opening_kind="pad:circle",
            ),
        ],
    )
    summaries = [
        v
        for v in assessment_results(assessment).violations
        if v.rule_id == "mask_to_copper_openings"
    ]
    assert len(summaries) == 1
    assert summaries[0].severity == "info"
    assert "3 findings across 2 mask openings" in summaries[0].message
    assert "pad:R1/1 x2" in summaries[0].message
    assert "pad:R2/1 x1" in summaries[0].message


def test_no_measurements_means_no_summary_finding():
    from kicad_tools.validate.mask_copper import assessment_results

    assessment = MaskCopperAssessment(coverage="complete", policy=_POLICY)
    assert not list(assessment_results(assessment).violations)


def test_v1_payload_without_new_fields_loads_and_round_trips():
    from kicad_tools.validate.mask_copper import MaskCopperAssessment

    payload = {
        "schema": "kct.mask-copper-assessment.v1",
        "coverage": "complete",
        "reasons": [],
        "policy": None,
        "binding": None,
        "measurements": [
            {
                "opening_uuids": ["00000000-0000-0000-0000-000000000001"],
                "conductor_uuid": "00000000-0000-0000-0000-000000000002",
                "mask_side": "F.Mask",
                "copper_layer": "F.Cu",
                "location_mm": [1.0, 2.0],
                "clearance_mm": 0.01,
                "exposed_area_mm2": 0.5,
                "relation": "overlap",
                "disposition": "violation",
                "uncertainty_mm": 0.0,
                "opening_kind": "pad:rect",
                "mask_defined": False,
                "margin_provenance": "native",
            }
        ],
        "intent_audit": [],
        "geometry_provenance": {},
        "evaluated_pairs": 0,
    }
    assessment = MaskCopperAssessment.from_dict(payload)
    measurement = assessment.measurements[0]
    assert measurement.owner_net is None
    assert measurement.conductor_net is None
    assert measurement.conductor_kind is None
    assert measurement.same_net is None
    assert measurement.own_pad_entry is None
    assert measurement.owner_ref is None
    reloaded = MaskCopperAssessment.from_dict(assessment.to_dict())
    assert reloaded.measurements[0].to_dict() == measurement.to_dict()


def _merged_region_geometry(trace_kind):
    """Two pads on nets N/M whose mask openings are bridged by one native region.

    The native F.Mask is wider than the two per-pad openings, so the residual
    bridge strip (x 1.1..1.3) becomes a ``merged_region`` opening with both pads
    as contributors. A conductor on net N sits wholly inside that strip.
    """
    from shapely.geometry import Point, box
    from shapely.ops import unary_union

    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    pad_a, pad_b, trace = (f"00000000-0000-0000-0000-00000000000{i}" for i in (1, 2, 3))
    open_a, open_b = box(-0.1, -0.1, 1.1, 1.1), box(1.3, -0.1, 2.3, 1.1)
    native = unary_union([open_a, open_b, box(1.0, -0.1, 1.3, 1.1)])
    copper_a, copper_b = box(0, 0, 1, 1), box(1.4, 0, 2.2, 1)
    conductor = Point(1.2, 0.5).buffer(0.04) if trace_kind == "via" else box(1.15, 0.4, 1.25, 0.6)
    exported = ExportedMaskGeometry(
        "a" * 64,
        None,
        None,
        "10.0.5",
        {},
        layers={"F.Mask": native, "F.Cu": unary_union([copper_a, copper_b, conductor])},
    )
    return AttributedMaskGeometry(
        exported,
        {
            pad_a: {
                "kind": "pad:rect",
                "net": "N",
                "pad_ref": "R1/1",
                "layers_geometry": {"F.Mask": open_a, "F.Cu": copper_a},
                "margin_mm": {"F.Mask": 0.1},
            },
            pad_b: {
                "kind": "pad:rect",
                "net": "M",
                "pad_ref": "R2/1",
                "layers_geometry": {"F.Mask": open_b, "F.Cu": copper_b},
                "margin_mm": {"F.Mask": 0.1},
            },
            trace: {
                "kind": trace_kind,
                "net": "N",
                "layers_geometry": {"F.Cu": conductor},
                "margin_mm": {},
            },
        },
    )


@pytest.mark.parametrize("trace_kind", ["segment", "via"])
def test_same_net_conductor_over_merged_region_prints_bare_same_net(trace_kind):
    """Issue #9149: bare ``same-net`` is also the no-single-owner-pad path.

    A segment/arc/via has a terminating endpoint, yet over a merged-region
    opening there is no single owner pad to test it against, so the relation
    is the bare ``same-net`` string -- not own-pad entry / pass-through.
    """
    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    assessment = assess_attributed_geometry(_merged_region_geometry(trace_kind), _POLICY)
    merged = [m for m in assessment.measurements if m.opening_kind == "merged_region"]
    assert len(merged) == 1
    assert merged[0].conductor_kind == trace_kind
    assert merged[0].same_net is True and merged[0].own_pad_entry is None
    labels = [
        v.message
        for v in _mask_findings(assessment_results(assessment))
        if "opening merged_region" in v.message
    ]
    assert len(labels) == 1
    assert labels[0].endswith("; same-net")


@pytest.mark.parametrize(
    "pad_b_net,expected_nets",
    [
        ("M", ("M", "N")),  # owners on N and M, conductor on N: M must survive
        ("", ("N",)),  # unnamed owner pad mixed with a named one
        ("N", ("N",)),  # same net on every owner stays single-net
    ],
)
def test_merged_region_nets_carry_every_owner_net(pad_b_net, expected_nets):
    """Issue #9374: a merged region owned by pads on 2+ nets has no scalar
    ``owner_net``; its finding's ``nets`` must still name every owner net, so
    a ``("mask_to_copper", "N")`` allowance entry cannot also cover it."""
    from kicad_tools.validate.mask_copper import assess_attributed_geometry, assessment_results

    geometry = _merged_region_geometry("segment")
    geometry.objects["00000000-0000-0000-0000-000000000002"]["net"] = pad_b_net
    assessment = assess_attributed_geometry(geometry, _POLICY)
    merged = [m for m in assessment.measurements if m.opening_kind == "merged_region"]
    assert len(merged) == 1
    assert merged[0].owner_nets == tuple(sorted({n for n in ("N", pad_b_net) if n}))
    findings = [
        v
        for v in assessment_results(assessment).violations
        if v.rule_id == "mask_to_copper" and "opening merged_region" in v.message
    ]
    assert len(findings) == 1
    assert findings[0].nets == expected_nets
    # Round trip keeps the field.
    reloaded = MaskCopperAssessment.from_dict(assessment.to_dict())
    assert [m.owner_nets for m in reloaded.measurements] == [
        m.owner_nets for m in assessment.measurements
    ]
