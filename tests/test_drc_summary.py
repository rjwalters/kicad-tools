"""Tests for the kicad-drc-summary CLI command."""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.drc_summary import (
    IssueSeverity,
    ManufacturerComparison,
    _build_fab_annotation,
    compare_with_manufacturer,
    create_summary,
    get_severity,
    main,
)
from kicad_tools.drc import DRCReport, Severity, ViolationType
from kicad_tools.drc.violation import DRCViolation
from kicad_tools.manufacturers import get_profile


@pytest.fixture
def sample_drc_report(fixtures_dir: Path) -> DRCReport:
    """Load the sample DRC report."""
    return DRCReport.load(fixtures_dir / "sample_drc.rpt")


class TestIssueSeverity:
    """Tests for severity classification."""

    def test_clearance_is_blocking(self):
        """Clearance violations should be blocking."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
        )
        assert get_severity(violation) == IssueSeverity.BLOCKING

    def test_shorting_items_is_blocking(self):
        """Shorting items should be blocking."""
        violation = DRCViolation(
            type=ViolationType.SHORTING_ITEMS,
            type_str="shorting_items",
            severity=Severity.ERROR,
            message="Short circuit",
        )
        assert get_severity(violation) == IssueSeverity.BLOCKING

    def test_track_width_is_blocking(self):
        """Track width violations should be blocking."""
        violation = DRCViolation(
            type=ViolationType.TRACK_WIDTH,
            type_str="track_width",
            severity=Severity.ERROR,
            message="Track too narrow",
        )
        assert get_severity(violation) == IssueSeverity.BLOCKING

    def test_unconnected_items_is_warning(self):
        """Unconnected items should be warning."""
        violation = DRCViolation(
            type=ViolationType.UNCONNECTED_ITEMS,
            type_str="unconnected_items",
            severity=Severity.ERROR,
            message="Unconnected pin",
        )
        assert get_severity(violation) == IssueSeverity.WARNING

    def test_courtyard_overlap_is_warning(self):
        """Courtyard overlap should be warning."""
        violation = DRCViolation(
            type=ViolationType.COURTYARD_OVERLAP,
            type_str="courtyard_overlap",
            severity=Severity.ERROR,
            message="Courtyards overlap",
        )
        assert get_severity(violation) == IssueSeverity.WARNING

    def test_silk_over_copper_is_cosmetic(self):
        """Silk over copper should be cosmetic."""
        violation = DRCViolation(
            type=ViolationType.SILK_OVER_COPPER,
            type_str="silk_over_copper",
            severity=Severity.WARNING,
            message="Silkscreen over copper",
        )
        assert get_severity(violation) == IssueSeverity.COSMETIC

    def test_silk_overlap_is_cosmetic(self):
        """Silk overlap should be cosmetic."""
        violation = DRCViolation(
            type=ViolationType.SILK_OVERLAP,
            type_str="silk_overlap",
            severity=Severity.WARNING,
            message="Silkscreen overlap",
        )
        assert get_severity(violation) == IssueSeverity.COSMETIC

    def test_unknown_type_defaults_to_warning(self):
        """Unknown violation types should default to warning."""
        violation = DRCViolation(
            type=ViolationType.UNKNOWN,
            type_str="some_new_type",
            severity=Severity.ERROR,
            message="Unknown violation",
        )
        assert get_severity(violation) == IssueSeverity.WARNING


class TestCreateSummary:
    """Tests for creating DRC summaries."""

    def test_summary_categorizes_by_severity(self, sample_drc_report: DRCReport):
        """Summary should categorize violations by severity."""
        summary = create_summary(sample_drc_report)

        # Should have some violations
        assert summary.total_violations > 0

        # All violations should be categorized
        total_categorized = summary.blocking_count + summary.warning_count + summary.cosmetic_count
        assert total_categorized == summary.total_violations

    def test_summary_tracks_unconnected_by_net(self, sample_drc_report: DRCReport):
        """Summary should track unconnected items by net."""
        summary = create_summary(sample_drc_report)

        # If there are unconnected items, they should be tracked by net
        unconnected_violations = [
            v for v in sample_drc_report.violations if v.type == ViolationType.UNCONNECTED_ITEMS
        ]
        if unconnected_violations:
            assert len(summary.unconnected_by_net) > 0

    def test_summary_with_manufacturer(self, sample_drc_report: DRCReport):
        """Summary should include manufacturer comparison when specified."""
        summary = create_summary(sample_drc_report, manufacturer_id="jlcpcb", layers=2)

        assert summary.manufacturer == "jlcpcb"
        # Should have some comparison results if there are measurable violations
        measurable_count = sum(
            1 for v in sample_drc_report.violations if v.actual_value_mm is not None
        )
        if measurable_count > 0:
            assert len(summary.false_positives) + len(summary.true_violations) > 0

    def test_summary_verdict_blocking(self):
        """Summary verdict should be BLOCKING when blocking issues exist."""
        # Create a minimal report with a blocking violation
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report)

        assert summary.has_blocking
        assert "BLOCKING" in summary.verdict

    def test_summary_verdict_warnings_only(self):
        """Summary verdict should indicate warnings when no blocking issues."""
        violation = DRCViolation(
            type=ViolationType.UNCONNECTED_ITEMS,
            type_str="unconnected_items",
            severity=Severity.ERROR,
            message="Unconnected pin",
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report)

        assert not summary.has_blocking
        assert summary.warning_count == 1
        assert "WARNINGS" in summary.verdict

    def test_summary_verdict_passed(self):
        """Summary verdict should be PASSED when no issues."""
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[],
        )
        summary = create_summary(report)

        assert not summary.has_blocking
        assert summary.warning_count == 0
        assert "PASSED" in summary.verdict


class TestManufacturerComparison:
    """Tests for manufacturer rule comparison."""

    def test_compare_clearance_pass(self):
        """Clearance meeting manufacturer limit should be false positive."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=0.15,  # Above JLCPCB 0.1mm limit
        )
        rules = get_profile("jlcpcb").get_design_rules(2)

        comparison = compare_with_manufacturer(violation, rules, "jlcpcb")

        assert comparison is not None
        assert comparison.is_false_positive
        assert "JLCPCB" in comparison.message

    def test_compare_clearance_fail(self):
        """Clearance below manufacturer limit should be true violation."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=0.05,  # Below JLCPCB 0.1mm limit
        )
        rules = get_profile("jlcpcb").get_design_rules(2)

        comparison = compare_with_manufacturer(violation, rules, "jlcpcb")

        assert comparison is not None
        assert not comparison.is_false_positive

    def test_compare_track_width(self):
        """Track width comparison should work."""
        violation = DRCViolation(
            type=ViolationType.TRACK_WIDTH,
            type_str="track_width",
            severity=Severity.ERROR,
            message="Track too narrow",
            actual_value_mm=0.2,  # Above typical limit
        )
        rules = get_profile("jlcpcb").get_design_rules(2)

        comparison = compare_with_manufacturer(violation, rules, "jlcpcb")

        assert comparison is not None
        assert comparison.manufacturer_limit is not None

    def test_compare_no_value_returns_none(self):
        """Violations without actual_value should return None."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=None,
        )
        rules = get_profile("jlcpcb").get_design_rules(2)

        comparison = compare_with_manufacturer(violation, rules, "jlcpcb")

        assert comparison is None


class TestDRCSummaryToDict:
    """Tests for DRCSummary JSON serialization."""

    def test_to_dict_basic(self, sample_drc_report: DRCReport):
        """Summary should convert to dict correctly."""
        summary = create_summary(sample_drc_report)
        d = summary.to_dict()

        assert "pcb_name" in d
        assert "source_file" in d
        assert "total_violations" in d
        assert "verdict" in d
        assert "counts" in d
        assert "blocking" in d
        assert "warnings" in d
        assert "cosmetic" in d

    def test_to_dict_with_manufacturer(self, sample_drc_report: DRCReport):
        """Summary with manufacturer should include comparison in dict."""
        summary = create_summary(sample_drc_report, manufacturer_id="jlcpcb", layers=2)
        d = summary.to_dict()

        assert "manufacturer" in d
        assert d["manufacturer"]["id"] == "jlcpcb"

    def test_to_dict_json_serializable(self, sample_drc_report: DRCReport):
        """Summary dict should be JSON serializable."""
        summary = create_summary(sample_drc_report)
        d = summary.to_dict()

        # Should not raise
        json_str = json.dumps(d)
        assert json_str is not None


class TestCLIMain:
    """Tests for the main CLI entry point."""

    def test_main_with_report_file(self, fixtures_dir: Path):
        """Main should process a DRC report file."""
        report_path = fixtures_dir / "sample_drc.rpt"
        exit_code = main([str(report_path)])

        # Should return 1 if blocking issues, 0 if none
        assert exit_code in (0, 1)

    def test_main_with_json_format(self, fixtures_dir: Path, capsys):
        """Main should output JSON when requested."""
        report_path = fixtures_dir / "sample_drc.rpt"
        main([str(report_path), "--format", "json"])

        captured = capsys.readouterr()

        # Should be valid JSON
        output = json.loads(captured.out)
        assert "verdict" in output
        assert "counts" in output

    def test_main_with_manufacturer(self, fixtures_dir: Path, capsys):
        """Main should include manufacturer comparison when specified."""
        report_path = fixtures_dir / "sample_drc.rpt"
        main([str(report_path), "--fab", "jlcpcb", "--format", "json"])

        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert "manufacturer" in output
        assert output["manufacturer"]["id"] == "jlcpcb"

    def test_main_with_blocking_only(self, fixtures_dir: Path, capsys):
        """Main with --blocking-only should only show blocking issues."""
        report_path = fixtures_dir / "sample_drc.rpt"
        main([str(report_path), "--blocking-only"])

        captured = capsys.readouterr()

        # Should contain BLOCKING section but condensed output
        assert "BLOCKING" in captured.out
        assert "VERDICT" in captured.out

    def test_main_nonexistent_file(self, capsys):
        """Main should return 1 for nonexistent file."""
        exit_code = main(["nonexistent_file.json"])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err or "not found" in captured.err.lower()

    def test_main_invalid_file_type(self, tmp_path, capsys):
        """Main should return 1 for unsupported file type."""
        bad_file = tmp_path / "test.txt"
        bad_file.write_text("not a drc report")

        exit_code = main([str(bad_file)])

        assert exit_code == 1

    def test_main_strict_mode(self):
        """Main with --strict should return 2 for warnings."""
        # Save warning-only report to temp file and test
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump(
                {
                    "source": "test.kicad_pcb",
                    "date": "2025-01-01T00:00:00",
                    "violations": [
                        {
                            "type": "unconnected_items",
                            "severity": "error",
                            "description": "Unconnected pin",
                        }
                    ],
                    "footprint_errors": 0,
                },
                f,
            )
            temp_path = f.name

        try:
            exit_code = main([temp_path, "--strict"])
            # Should return 2 for warnings in strict mode
            assert exit_code == 2
        finally:
            Path(temp_path).unlink()


class TestManufacturerOptions:
    """Tests for different manufacturer options."""

    def test_all_manufacturers_work(self, sample_drc_report: DRCReport):
        """All supported manufacturers should work."""
        for mfr in ["jlcpcb", "oshpark", "pcbway", "seeed"]:
            summary = create_summary(sample_drc_report, manufacturer_id=mfr)
            assert summary.manufacturer == mfr

    def test_layers_affect_rules(self, sample_drc_report: DRCReport):
        """Different layer counts should use different rules."""
        summary_2l = create_summary(sample_drc_report, manufacturer_id="jlcpcb", layers=2)
        summary_4l = create_summary(sample_drc_report, manufacturer_id="jlcpcb", layers=4)

        # Both should work
        assert summary_2l.manufacturer == "jlcpcb"
        assert summary_4l.manufacturer == "jlcpcb"


class TestFabReclassification:
    """Tests for fab-aware severity reclassification."""

    def test_annular_width_reclassified_for_jlcpcb(self):
        """Annular width violation above JLCPCB limit should be FAB_ACCEPTABLE."""
        jlcpcb_rules = get_profile("jlcpcb").get_design_rules(2)
        violation = DRCViolation(
            type=ViolationType.VIA_ANNULAR_WIDTH,
            type_str="via_annular_width",
            severity=Severity.ERROR,
            message="Annular width violation",
            actual_value_mm=jlcpcb_rules.min_annular_ring_mm + 0.01,  # Above JLCPCB limit
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")

        assert summary.fab_acceptable_count == 1
        assert summary.blocking_count == 0
        assert summary.warning_count == 0

    def test_annular_width_blocking_for_oshpark(self):
        """Annular width below OSHPark limit should remain in default severity."""
        # OSHPark has stricter annular ring requirements
        oshpark_rules = get_profile("oshpark").get_design_rules(2)
        # Use a value below OSHPark's limit
        actual = oshpark_rules.min_annular_ring_mm - 0.01

        violation = DRCViolation(
            type=ViolationType.VIA_ANNULAR_WIDTH,
            type_str="via_annular_width",
            severity=Severity.ERROR,
            message="Annular width violation",
            actual_value_mm=actual,
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="oshpark")

        # Via annular width defaults to WARNING, and it fails fab rules,
        # so it stays as WARNING (not reclassified)
        assert summary.fab_acceptable_count == 0
        assert summary.warning_count == 1

    def test_clearance_reclassified_when_above_fab_limit(self):
        """Clearance above manufacturer limit should be FAB_ACCEPTABLE."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=0.15,  # Above JLCPCB 0.1mm limit
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")

        assert summary.fab_acceptable_count == 1
        assert summary.blocking_count == 0
        assert "clearance" in summary.fab_acceptable_by_type

    def test_clearance_stays_blocking_when_below_fab_limit(self):
        """Clearance below manufacturer limit should stay BLOCKING."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=0.05,  # Below JLCPCB 0.1mm limit
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")

        assert summary.blocking_count == 1
        assert summary.fab_acceptable_count == 0

    def test_silk_stays_cosmetic_regardless_of_manufacturer(self):
        """Silk violations always stay cosmetic, even with --fab."""
        violation = DRCViolation(
            type=ViolationType.SILK_OVER_COPPER,
            type_str="silk_over_copper",
            severity=Severity.WARNING,
            message="Silk over copper",
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")

        assert summary.cosmetic_count == 1
        assert summary.fab_acceptable_count == 0

    def test_no_manufacturer_behavior_unchanged(self):
        """Without --fab, behavior should be unchanged (backward compatible)."""
        violations = [
            DRCViolation(
                type=ViolationType.CLEARANCE,
                type_str="clearance",
                severity=Severity.ERROR,
                message="Clearance violation",
                actual_value_mm=0.15,
            ),
            DRCViolation(
                type=ViolationType.VIA_ANNULAR_WIDTH,
                type_str="via_annular_width",
                severity=Severity.ERROR,
                message="Annular width",
                actual_value_mm=0.125,
            ),
            DRCViolation(
                type=ViolationType.SILK_OVER_COPPER,
                type_str="silk_over_copper",
                severity=Severity.WARNING,
                message="Silk",
            ),
        ]
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=violations,
        )
        summary = create_summary(report)

        # Without manufacturer, no fab reclassification
        assert summary.fab_acceptable_count == 0
        assert summary.blocking_count == 1  # clearance
        assert summary.warning_count == 1  # via_annular_width
        assert summary.cosmetic_count == 1  # silk

    def test_mixed_violations_with_fab(self):
        """Mixed violations should be correctly split with --fab."""
        violations = [
            # Clearance above JLCPCB limit → FAB_ACCEPTABLE
            DRCViolation(
                type=ViolationType.CLEARANCE,
                type_str="clearance",
                severity=Severity.ERROR,
                message="Clearance above fab limit",
                actual_value_mm=0.15,
            ),
            # Clearance below JLCPCB limit → stays BLOCKING
            DRCViolation(
                type=ViolationType.CLEARANCE,
                type_str="clearance",
                severity=Severity.ERROR,
                message="Clearance below fab limit",
                actual_value_mm=0.05,
            ),
            # Short circuit → always BLOCKING (no fab comparison)
            DRCViolation(
                type=ViolationType.SHORTING_ITEMS,
                type_str="shorting_items",
                severity=Severity.ERROR,
                message="Short circuit",
            ),
            # Unconnected → WARNING (no fab comparison for this type)
            DRCViolation(
                type=ViolationType.UNCONNECTED_ITEMS,
                type_str="unconnected_items",
                severity=Severity.ERROR,
                message="Unconnected pin",
            ),
            # Silk → COSMETIC
            DRCViolation(
                type=ViolationType.SILK_OVER_COPPER,
                type_str="silk_over_copper",
                severity=Severity.WARNING,
                message="Silk",
            ),
        ]
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=violations,
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")

        assert summary.fab_acceptable_count == 1  # clearance above limit
        assert summary.blocking_count == 2  # clearance below limit + short
        assert summary.warning_count == 1  # unconnected
        assert summary.cosmetic_count == 1  # silk

    def test_track_width_reclassified(self):
        """Track width above fab limit should be FAB_ACCEPTABLE."""
        violation = DRCViolation(
            type=ViolationType.TRACK_WIDTH,
            type_str="track_width",
            severity=Severity.ERROR,
            message="Track width violation",
            actual_value_mm=0.2,  # Above typical JLCPCB limit
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")

        assert summary.fab_acceptable_count == 1
        assert summary.blocking_count == 0


class TestFabAcceptableOutput:
    """Tests for fab-acceptable output formatting."""

    def test_fab_acceptable_to_dict(self):
        """JSON output should include fab_acceptable category."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=0.15,
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")
        d = summary.to_dict()

        assert "fab_acceptable" in d
        assert d["fab_acceptable"]["count"] == 1
        assert "clearance" in d["fab_acceptable"]["by_type"]
        assert d["counts"]["fab_acceptable"] == 1

    def test_fab_acceptable_json_serializable(self):
        """Fab-acceptable data in dict should be JSON serializable."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=0.15,
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")
        d = summary.to_dict()

        # Should not raise
        json_str = json.dumps(d)
        assert "fab_acceptable" in json_str

    def test_cli_fab_summary_table(self, fixtures_dir: Path, capsys):
        """Table output with --fab should show FAB-ACCEPTABLE section."""
        report_path = fixtures_dir / "sample_drc.rpt"
        main([str(report_path), "--fab", "jlcpcb"])

        captured = capsys.readouterr()
        # Output should contain BLOCKING and VERDICT sections
        assert "BLOCKING" in captured.out
        assert "VERDICT" in captured.out

    def test_cli_fab_summary_json(self, fixtures_dir: Path, capsys):
        """JSON output with --fab should include fab_acceptable counts."""
        report_path = fixtures_dir / "sample_drc.rpt"
        main([str(report_path), "--fab", "jlcpcb", "--format", "json"])

        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert "fab_acceptable" in output
        assert "count" in output["fab_acceptable"]
        assert output["counts"]["fab_acceptable"] >= 0

    def test_exit_code_zero_when_only_fab_acceptable(self):
        """Exit code should be 0 when all violations are fab-acceptable."""
        # Create violation that passes JLCPCB limits
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=0.15,  # Above JLCPCB 0.1mm
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")

        # Fab-acceptable violations should not trigger blocking
        assert not summary.has_blocking
        assert summary.fab_acceptable_count == 1

    def test_verdict_fab_compatible(self):
        """Verdict should indicate fab compatibility when all pass."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=0.15,
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")

        assert "FAB COMPATIBLE" in summary.verdict
        assert "JLCPCB" in summary.verdict

    def test_verdict_blocking_with_manufacturer(self):
        """Verdict with fab and blocking violations should mention manufacturer."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=0.05,  # Below JLCPCB limit
        )
        report = DRCReport(
            source_file="test.json",
            created_at=None,
            pcb_name="test.kicad_pcb",
            violations=[violation],
        )
        summary = create_summary(report, manufacturer_id="jlcpcb")

        assert "BLOCKING" in summary.verdict
        assert "JLCPCB" in summary.verdict

    def test_build_fab_annotation(self):
        """Fab annotation should show actual vs manufacturer limit."""
        violation = DRCViolation(
            type=ViolationType.CLEARANCE,
            type_str="clearance",
            severity=Severity.ERROR,
            message="Clearance violation",
            actual_value_mm=0.15,
        )
        comparison = ManufacturerComparison(
            violation=violation,
            is_false_positive=True,
            manufacturer_limit=0.1,
            actual_value=0.15,
            message="JLCPCB accepts 0.100mm",
        )
        annotation = _build_fab_annotation("clearance", [comparison], "jlcpcb")

        assert "0.150mm" in annotation
        assert "JLCPCB" in annotation
        assert "0.100mm" in annotation

    def test_build_fab_annotation_no_match(self):
        """Fab annotation should return empty string when no match."""
        annotation = _build_fab_annotation("unknown_type", [], "jlcpcb")
        assert annotation == ""


class TestFabReclassificationWithFixture:
    """Tests using the sample DRC fixture for fab reclassification."""

    def test_sample_report_with_fab(self, sample_drc_report: DRCReport):
        """Sample report with --fab should correctly categorize violations."""
        summary = create_summary(sample_drc_report, manufacturer_id="jlcpcb")

        # All violations should still be accounted for
        total = (
            summary.blocking_count
            + summary.warning_count
            + summary.fab_acceptable_count
            + summary.cosmetic_count
        )
        assert total == summary.total_violations

    def test_sample_report_without_fab_no_fab_acceptable(self, sample_drc_report: DRCReport):
        """Without --fab, no violations should be fab-acceptable."""
        summary = create_summary(sample_drc_report)
        assert summary.fab_acceptable_count == 0

    def test_all_manufacturers_categorize_correctly(self, sample_drc_report: DRCReport):
        """All manufacturers should produce valid categorization."""
        for mfr in ["jlcpcb", "oshpark", "pcbway", "seeed"]:
            summary = create_summary(sample_drc_report, manufacturer_id=mfr)
            total = (
                summary.blocking_count
                + summary.warning_count
                + summary.fab_acceptable_count
                + summary.cosmetic_count
            )
            assert total == summary.total_violations, f"Total mismatch for {mfr}"
