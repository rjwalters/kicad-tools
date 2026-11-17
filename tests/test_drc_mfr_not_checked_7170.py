"""Issue #7170 Part A: ``kct drc --mfr`` never drops a kicad-cli violation.

Every violation gets exactly one manufacturer verdict -- compatible,
incompatible, or *not checked* (kicad-cli's severity stands) -- and ``--mfr``
shares plain ``kct drc``'s exit gate (``_drc_exit_code``).

Fixtures are real kicad-cli 10.0.6 output (run with the pinned
``src/kicad_tools/data/kicad-config/10.0`` tables):

* ``kicad10_via_in_pad_7170.{json,rpt}`` -- board 00 ``simple_led_routed``
  plus one GND via (size 0.4, drill 0.3) inside D1 pad 1.
* ``kicad10_drill_out_of_range_7170.json`` -- board 00 plus a 0.15 mm-drill
  via and a 7 mm-drill via, with a ``hole_size (max 6.3mm)`` DRU rule added.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import drc_cmd
from kicad_tools.core.types import Severity
from kicad_tools.drc import DRCReport, DRCViolation, check_manufacturer_rules
from kicad_tools.drc.checker import (
    MANUFACTURER_CHECKED_TYPES,
    NOT_CHECKED_RULE,
    CheckResult,
    summarize_checks,
)
from kicad_tools.drc.parity import (
    PARITY_FAILED,
    PARITY_NOT_RUN,
    PARITY_PASSED,
    SchematicParityStatus,
)
from kicad_tools.drc.violation import ViolationType as V

FIXTURES = Path(__file__).parent / "fixtures" / "drc"
VIA_IN_PAD_JSON = FIXTURES / "kicad10_via_in_pad_7170.json"
VIA_IN_PAD_RPT = FIXTURES / "kicad10_via_in_pad_7170.rpt"
DRILL_JSON = FIXTURES / "kicad10_drill_out_of_range_7170.json"


# ---------------------------------------------------------------------------
# Pinned enumeration: every kicad-cli 10.0.6 DRC settings key (67, from the
# DRC_ITEM table in KiCad 10.0.6's _pcbnew.kiface) and what from_string makes
# of it.  A change here is deliberate; update the table and say why.
# ---------------------------------------------------------------------------

KICAD_CLI_10_DRC_TYPES: dict[str, V] = {
    "unconnected_items": V.UNCONNECTED_ITEMS,
    "shorting_items": V.SHORTING_ITEMS,
    "items_not_allowed": V.UNKNOWN,
    "text_on_edge_cuts": V.UNKNOWN,
    "clearance": V.CLEARANCE,
    "creepage": V.UNKNOWN,
    "tracks_crossing": V.UNKNOWN,
    "copper_edge_clearance": V.COPPER_EDGE_CLEARANCE,
    "zones_intersect": V.UNKNOWN,
    "isolated_copper": V.UNKNOWN,
    "starved_thermal": V.UNKNOWN,
    "via_dangling": V.UNKNOWN,
    "track_dangling": V.UNKNOWN,
    "hole_clearance": V.DRILL_CLEARANCE,
    "hole_to_hole": V.HOLE_TO_HOLE_CLEARANCE,
    "holes_co_located": V.UNKNOWN,
    "connection_width": V.UNKNOWN,
    "track_width": V.TRACK_WIDTH,
    "track_angle": V.TRACK_ANGLE,
    "track_segment_length": V.UNKNOWN,
    "annular_width": V.VIA_ANNULAR_WIDTH,
    "drill_out_of_range": V.DRILL_HOLE_TOO_SMALL,
    "via_diameter": V.DIMENSION_VIA_DIAMETER,
    "padstack": V.UNKNOWN,
    "padstack_invalid": V.UNKNOWN,
    "microvia_drill_out_of_range": V.MICRO_VIA_HOLE_TOO_SMALL,
    "courtyards_overlap": V.COURTYARD_OVERLAP,
    # The fuzzy "courtyard" / "footprint" matches below mislabel several keys
    # (out of scope for #7170: harmless for --mfr, which has no branch for
    # them, but they affect --category and fix-drc).
    "missing_courtyard": V.COURTYARD_OVERLAP,
    "malformed_courtyard": V.COURTYARD_OVERLAP,
    "pth_inside_courtyard": V.COURTYARD_OVERLAP,
    "npth_inside_courtyard": V.COURTYARD_OVERLAP,
    "item_on_disabled_layer": V.UNKNOWN,
    "invalid_outline": V.MALFORMED_OUTLINE,
    "duplicate_footprints": V.DUPLICATE_FOOTPRINT,
    "missing_footprint": V.MISSING_FOOTPRINT,
    "extra_footprint": V.EXTRA_FOOTPRINT,
    "net_conflict": V.UNKNOWN,
    "footprint_symbol_mismatch": V.FOOTPRINT,
    "footprint_filters_mismatch": V.FOOTPRINT,
    "footprint_symbol_field_mismatch": V.FOOTPRINT,
    "lib_footprint_issues": V.FOOTPRINT,
    "lib_footprint_mismatch": V.FOOTPRINT,
    "unresolved_variable": V.UNKNOWN,
    "assertion_failure": V.UNKNOWN,
    "generic_warning": V.UNKNOWN,
    "generic_error": V.UNKNOWN,
    "copper_sliver": V.COPPER_SLIVER,
    "solder_mask_bridge": V.SOLDER_MASK_BRIDGE,
    "silk_over_copper": V.SILK_OVER_COPPER,
    "silk_edge_clearance": V.SILK_EDGE_CLEARANCE,
    "silk_overlap": V.SILK_OVERLAP,
    "text_height": V.UNKNOWN,
    "text_thickness": V.UNKNOWN,
    "length_out_of_range": V.UNKNOWN,
    "skew_out_of_range": V.UNKNOWN,
    "too_many_vias": V.UNKNOWN,
    "diff_pair_gap_out_of_range": V.UNKNOWN,
    "diff_pair_uncoupled_length_too_long": V.UNKNOWN,
    "footprint": V.FOOTPRINT,
    "footprint_type_mismatch": V.FOOTPRINT,
    "through_hole_pad_without_hole": V.UNKNOWN,
    "mirrored_text_on_front_layer": V.UNKNOWN,
    "nonmirrored_text_on_back_layer": V.UNKNOWN,
    "missing_tuning_profile": V.UNKNOWN,
    "tuning_profile_track_geometries": V.UNKNOWN,
    "track_on_post_machined_layer": V.UNKNOWN,
    "track_not_centered_on_via": V.UNKNOWN,
}

#: The kicad-cli keys ``--mfr`` judges against a fab limit -- and only these.
JUDGED_KICAD_CLI_TYPES = {
    "unconnected_items",
    "shorting_items",
    "clearance",
    "copper_edge_clearance",
    "track_width",
    "annular_width",
    "drill_out_of_range",
    "via_diameter",
    "hole_to_hole",
    "solder_mask_bridge",
}


def test_enumeration_is_complete():
    assert len(KICAD_CLI_10_DRC_TYPES) == 67


@pytest.mark.parametrize("type_str", sorted(KICAD_CLI_10_DRC_TYPES))
def test_from_string_pinned(type_str):
    assert V.from_string(type_str) is KICAD_CLI_10_DRC_TYPES[type_str]


def test_judged_kicad_cli_types_are_exactly_the_mfr_branches():
    """Alias drift guard: a judged key that parses UNKNOWN (how ``annular_width``
    went dead) or an unjudged key that lands on a judged type (how
    ``hole_clearance`` was read against the copper floor) both fail here."""
    judged = {k for k, t in KICAD_CLI_10_DRC_TYPES.items() if t in MANUFACTURER_CHECKED_TYPES}
    assert judged == JUDGED_KICAD_CLI_TYPES
    for key in JUDGED_KICAD_CLI_TYPES:
        assert V.from_string(key) is not V.UNKNOWN, key


def _violation(type_str: str, severity: Severity = Severity.ERROR, **kw) -> DRCViolation:
    return DRCViolation(
        type=V.from_string(type_str),
        type_str=type_str,
        severity=severity,
        message=kw.pop("message", f"{type_str} finding"),
        **kw,
    )


def test_every_kicad_cli_type_gets_exactly_one_result():
    violations = [_violation(k) for k in KICAD_CLI_10_DRC_TYPES]
    report = DRCReport(source_file="", created_at=None, pcb_name="", violations=violations)
    checks = check_manufacturer_rules(report, "jlcpcb", layers=2)

    assert len(checks) == len(violations) == 67
    for check, violation in zip(checks, violations, strict=True):
        assert check.violation is violation
        if violation.type_str in JUDGED_KICAD_CLI_TYPES:
            assert not check.is_not_checked, violation.type_str
        else:
            assert check.result is CheckResult.UNKNOWN, violation.type_str
            assert check.rule_name == NOT_CHECKED_RULE
            assert violation.type_str in check.message


# ---------------------------------------------------------------------------
# The Hermit reproducer
# ---------------------------------------------------------------------------


def _by_type(checks):
    out: dict[str, list] = {}
    for c in checks:
        out.setdefault(c.violation.type_str, []).append(c)
    return out


@pytest.mark.parametrize("fixture", [VIA_IN_PAD_JSON, VIA_IN_PAD_RPT], ids=["json", "rpt"])
def test_reproducer_verdicts(fixture):
    report = DRCReport.load(fixture)
    assert len(report.violations) == 5
    checks = check_manufacturer_rules(report, "jlcpcb", layers=2)
    assert len(checks) == 5
    by_type = _by_type(checks)

    (annular,) = by_type["annular_width"]
    assert annular.result is CheckResult.FAIL
    assert annular.manufacturer_limit == pytest.approx(0.15)
    assert annular.actual_value == pytest.approx(0.05)

    hole_to_hole = by_type["hole_to_hole"]
    assert len(hole_to_hole) == 2
    for c in hole_to_hole:
        assert c.result is CheckResult.FAIL
        assert c.manufacturer_limit == pytest.approx(0.5)
        assert c.actual_value == pytest.approx(0.27)

    (via_dia,) = by_type["via_diameter"]
    assert via_dia.result is CheckResult.FAIL
    assert via_dia.manufacturer_limit == pytest.approx(0.6)
    assert via_dia.actual_value == pytest.approx(0.4)

    (dangling,) = by_type["via_dangling"]
    assert dangling.is_not_checked
    assert dangling.violation.severity is Severity.WARNING

    summary = summarize_checks(checks)
    assert summary["fail"] == 4
    assert summary["not_checked"] == summary["unknown"] == 1


def test_via_diameter_value_is_parsed():
    report = DRCReport.load(VIA_IN_PAD_JSON)
    (v,) = [v for v in report.violations if v.type_str == "via_diameter"]
    assert v.required_value_mm == pytest.approx(0.6)
    assert v.actual_value_mm == pytest.approx(0.4)


def test_reproducer_cli_table_exits_1(capsys):
    rc = drc_cmd.main([str(VIA_IN_PAD_JSON), "--mfr", "jlcpcb"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "All 0 checked violations are compatible" not in out
    assert "INCOMPATIBLE VIOLATIONS (4)" in out
    assert "NOT CHECKED against JLCPCB limits: 1 (errors 0, warnings 1)" in out
    assert "by type: via_dangling 1" in out
    assert "Compatible: 0, Incompatible: 4, Not checked: 1" in out


def test_reproducer_cli_json_counts_add_up(capsys):
    rc = drc_cmd.main([str(VIA_IN_PAD_JSON), "--mfr", "jlcpcb", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    summary = data["summary"]
    total = len(DRCReport.load(VIA_IN_PAD_JSON).violations)

    assert rc == 1
    assert data["exit_code"] == rc
    assert data["manufacturer"] == "jlcpcb"
    assert data["layers"] == 2
    assert summary["total"] == total == len(data["checks"]) == 5
    assert summary["checked"] + summary["not_checked"] == total
    assert summary["compatible"] + summary["incompatible"] == summary["checked"]
    assert (summary["compatible"], summary["incompatible"], summary["not_checked"]) == (0, 4, 1)
    assert summary["not_checked_errors"] == 0
    assert summary["not_checked_warnings"] == 1
    assert summary["not_checked_by_type"] == {"via_dangling": 1}
    assert data["schematic_parity"]["checked"] is False
    for entry in data["checks"]:
        assert set(entry) >= {
            "type_str",
            "severity",
            "result",
            "rule_name",
            "message",
            "manufacturer_limit",
            "actual_value",
        }


def test_json_exit_code_matches_table(capsys):
    for extra in ([], ["--allow-incomplete"], ["--strict", "--allow-incomplete"]):
        rc_table = drc_cmd.main([str(VIA_IN_PAD_JSON), "--mfr", "jlcpcb", *extra])
        rc_json = drc_cmd.main(
            [str(VIA_IN_PAD_JSON), "--mfr", "jlcpcb", "--format", "json", *extra]
        )
        capsys.readouterr()
        assert rc_table == rc_json == 1


# ---------------------------------------------------------------------------
# drill_out_of_range: both bounds; hole_clearance: not checked
# ---------------------------------------------------------------------------


def test_drill_out_of_range_checks_min_and_max():
    report = DRCReport.load(DRILL_JSON)
    checks = check_manufacturer_rules(report, "jlcpcb", layers=2)
    assert len(checks) == len(report.violations)
    drills = _by_type(checks)["drill_out_of_range"]
    by_rule = {c.rule_name: c for c in drills}
    assert set(by_rule) == {"min_via_drill", "max_hole_diameter"}

    small = by_rule["min_via_drill"]
    assert small.result is CheckResult.FAIL
    assert small.actual_value == pytest.approx(0.15)
    assert small.manufacturer_limit == pytest.approx(0.3)

    large = by_rule["max_hole_diameter"]
    assert large.result is CheckResult.FAIL
    assert large.actual_value == pytest.approx(7.0)
    assert large.manufacturer_limit == pytest.approx(6.3)


def test_drill_within_both_bounds_passes():
    v = _violation(
        "drill_out_of_range", message="Hole size out of range (min hole 0.2 mm; actual 1.0000 mm)"
    )
    v.actual_value_mm = 1.0
    report = DRCReport(source_file="", created_at=None, pcb_name="", violations=[v])
    (check,) = check_manufacturer_rules(report, "jlcpcb")
    assert check.result is CheckResult.PASS


def test_hole_clearance_is_not_judged_against_copper_floor():
    report = DRCReport.load(DRILL_JSON)
    checks = check_manufacturer_rules(report, "jlcpcb", layers=2)
    hole_clearance = _by_type(checks)["hole_clearance"]
    assert hole_clearance
    assert all(c.is_not_checked for c in hole_clearance)
    assert all(c.violation.type is V.DRILL_CLEARANCE for c in hole_clearance)


def test_kicad_cli_track_via_clearance_is_judged():
    """kicad-cli ``clearance`` between a track and a via is refined to
    CLEARANCE_SEGMENT_VIA by the parser; --mfr must still judge it."""
    v = _violation(
        "clearance",
        message="Clearance violation (clearance 0.2000 mm; actual 0.1000 mm)",
        items=["Track [GND] on F.Cu", "Via [VCC] on F.Cu - B.Cu"],
    )
    v.type = V.CLEARANCE_SEGMENT_VIA
    v.actual_value_mm = 0.1
    report = DRCReport(source_file="", created_at=None, pcb_name="", violations=[v])
    (check,) = check_manufacturer_rules(report, "jlcpcb")
    assert check.rule_name == "min_clearance"
    assert check.result is CheckResult.FAIL


# ---------------------------------------------------------------------------
# Shared exit gate
# ---------------------------------------------------------------------------


def _write_report(tmp_path: Path, violations: list[dict], name: str = "r.json") -> Path:
    data = json.loads(VIA_IN_PAD_JSON.read_text())
    data["violations"] = violations
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def _raw(type_str: str, severity: str) -> dict:
    return {
        "type": type_str,
        "severity": severity,
        "description": f"{type_str} finding",
        "items": [{"description": "Via [GND] on F.Cu - B.Cu", "pos": {"x": 1.0, "y": 1.0}}],
    }


@pytest.mark.parametrize(
    ("violations", "extra"),
    [
        ([_raw("via_dangling", "error")], []),
        ([_raw("via_dangling", "error")], ["--allow-incomplete"]),
        ([_raw("via_dangling", "warning")], []),
        ([_raw("via_dangling", "warning")], ["--allow-incomplete"]),
        ([_raw("via_dangling", "warning")], ["--allow-incomplete", "--strict"]),
        ([], []),
        ([], ["--allow-incomplete"]),
        ([], ["--allow-incomplete", "--strict"]),
    ],
    ids=[
        "nc-error",
        "nc-error-allow",
        "nc-warning",
        "nc-warning-allow",
        "nc-warning-strict",
        "empty",
        "empty-allow",
        "empty-strict",
    ],
)
def test_plain_and_mfr_share_exit_code(tmp_path, capsys, violations, extra):
    """For findings --mfr cannot judge, both modes see the same counts and
    must return the same code."""
    report = _write_report(tmp_path, violations)
    rc_plain = drc_cmd.main([str(report), *extra])
    rc_mfr = drc_cmd.main([str(report), "--mfr", "jlcpcb", *extra])
    capsys.readouterr()
    assert rc_plain == rc_mfr


@pytest.mark.parametrize(
    ("violations", "extra", "expected"),
    [
        ([_raw("via_dangling", "error")], ["--allow-incomplete"], 1),
        ([_raw("via_dangling", "warning")], ["--allow-incomplete"], 0),
        ([_raw("via_dangling", "warning")], ["--allow-incomplete", "--strict"], 2),
        ([_raw("via_dangling", "warning")], [], 2),
        ([], [], 2),
        ([], ["--allow-incomplete"], 0),
    ],
)
def test_mfr_exit_codes(tmp_path, capsys, violations, extra, expected):
    report = _write_report(tmp_path, violations)
    assert drc_cmd.main([str(report), "--mfr", "jlcpcb", *extra]) == expected
    capsys.readouterr()


@pytest.mark.parametrize(
    ("errors", "warnings", "status", "strict", "allow", "expected"),
    [
        (1, 0, PARITY_PASSED, False, False, 1),
        (0, 1, PARITY_PASSED, True, False, 2),
        (0, 1, PARITY_PASSED, False, False, 0),
        (0, 0, PARITY_PASSED, False, False, 0),
        (0, 0, PARITY_NOT_RUN, False, False, 2),
        (0, 0, PARITY_NOT_RUN, False, True, 0),
    ],
)
def test_drc_exit_code_helper(errors, warnings, status, strict, allow, expected):
    parity = SchematicParityStatus(status=status)
    assert (
        drc_cmd._drc_exit_code(errors, warnings, parity, strict=strict, allow_incomplete=allow)
        == expected
    )


def test_drc_exit_code_folds_in_parity_findings():
    issue = _violation("net_conflict")
    parity = SchematicParityStatus(status=PARITY_FAILED, issues=(issue,))
    assert drc_cmd._drc_exit_code(0, 0, parity, strict=False, allow_incomplete=False) == 1


def test_filter_config_applies_before_mfr(tmp_path, capsys):
    cfg = tmp_path / "filters.toml"
    cfg.write_text('[[drc.filters]]\ntype_pattern = "via_dangling"\naction = "ignore"\n')
    rc = drc_cmd.main(
        [str(VIA_IN_PAD_JSON), "--mfr", "jlcpcb", "--format", "json", "--filter-config", str(cfg)]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["summary"]["total"] == 4
    assert data["summary"]["not_checked"] == 0


# ---------------------------------------------------------------------------
# Edge cases and API
# ---------------------------------------------------------------------------


def test_unknown_manufacturer_does_not_crash_summarize():
    report = DRCReport.load(VIA_IN_PAD_JSON)
    checks = check_manufacturer_rules(report, "nosuchfab")
    assert len(checks) == 5
    assert all(c.result is CheckResult.UNKNOWN for c in checks)
    summary = summarize_checks(checks)
    assert summary["unknown"] == summary["not_checked"] == 5
    assert summary["by_rule"]["unknown"]["unknown"] == 5


def test_compatibility_hint_quiet_when_an_error_is_not_checked():
    passing_clearance = _violation(
        "clearance", message="Clearance violation (clearance 0.2000 mm; actual 0.1500 mm)"
    )
    passing_clearance.actual_value_mm = 0.15
    unjudged = _violation("starved_thermal")

    assert drc_cmd._get_manufacturer_compatibility_hint([passing_clearance]) is not None
    assert drc_cmd._get_manufacturer_compatibility_hint([passing_clearance, unjudged]) is None


def test_project_check_drc_returns_one_entry_per_violation():
    from kicad_tools.project import Project

    checks = Project().check_drc(manufacturer="jlcpcb", report_path=VIA_IN_PAD_RPT)
    assert len(checks) == 5
    assert sum(1 for c in checks if c.is_not_checked) == 1
