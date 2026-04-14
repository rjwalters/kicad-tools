"""Tests for the fix-erc command."""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.fix_erc_cmd import FixERCResult, _apply_fixes, main
from kicad_tools.erc.report import ERCReport
from kicad_tools.erc.violation import ERCViolation, ERCViolationType, Severity

# ── Fixture paths ─────────────────────────────────────────────────────

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SAMPLE_ERC_FIXABLE = FIXTURE_DIR / "sample_erc_fixable.json"

# Minimal schematic for round-trip testing
MINIMAL_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "kicadtools_test")
\t(uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "power:PWR_FLAG"
\t\t\t(power)
\t\t\t(symbol "PWR_FLAG_0_0"
\t\t\t\t(pin power_out line
\t\t\t\t\t(at 0 0 270)
\t\t\t\t\t(length 0)
\t\t\t\t\t(name "pwr")
\t\t\t\t\t(number "1")
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""


# ── Helper to build violation objects ─────────────────────────────────


def _make_violation(
    vtype: ERCViolationType,
    type_str: str,
    description: str,
    pos_x: float,
    pos_y: float,
    sheet: str = "/",
) -> ERCViolation:
    return ERCViolation(
        type=vtype,
        type_str=type_str,
        severity=Severity.ERROR,
        description=description,
        sheet=sheet,
        pos_x=pos_x,
        pos_y=pos_y,
        items=[],
    )


def _make_report(violations: list[ERCViolation]) -> ERCReport:
    return ERCReport(
        source_file="test.kicad_sch",
        kicad_version="8.0.0",
        coordinate_units="mm",
        violations=violations,
    )


# ── Tests: dry-run prints plan without modifying files ────────────────


class TestDryRun:
    """Dry-run should preview fixes without writing any files."""

    def test_dry_run_no_file_modification(self, tmp_path):
        """--dry-run should not modify the schematic file."""
        sch_file = tmp_path / "board.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC)
        original_content = sch_file.read_text()

        report_file = tmp_path / "erc.json"
        report_file.write_text(SAMPLE_ERC_FIXABLE.read_text())

        main([str(sch_file), "--erc-report", str(report_file), "--dry-run", "--format", "json"])

        # File should be unchanged
        assert sch_file.read_text() == original_content

    def test_dry_run_reports_fix_count(self, tmp_path, capsys):
        """--dry-run should report the number of fixes it would apply."""
        sch_file = tmp_path / "board.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC)

        report_file = tmp_path / "erc.json"
        report_file.write_text(SAMPLE_ERC_FIXABLE.read_text())

        main([str(sch_file), "--erc-report", str(report_file), "--dry-run", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["dry_run"] is True
        assert data["total_fixed"] == 5  # 2 pwr_flag + 3 no_connect
        assert data["pwr_flag_inserted"] == 2
        assert data["no_connect_inserted"] == 3


# ── Tests: power_pin_not_driven fix ──────────────────────────────────


class TestPowerPinNotDriven:
    """power_pin_not_driven violations should produce PWR_FLAG insertions."""

    def test_pwr_flag_coordinates(self):
        """PWR_FLAG should be placed at pos_y + 2.54mm offset."""
        violations = [
            _make_violation(
                ERCViolationType.POWER_PIN_NOT_DRIVEN,
                "power_pin_not_driven",
                "Input Power pin not driven",
                pos_x=100.0,
                pos_y=50.0,
            ),
        ]
        report = _make_report(violations)

        # Use dry_run to skip schematic load
        result = _apply_fixes(Path("dummy.kicad_sch"), report, dry_run=True, quiet=True)

        assert result.pwr_flag_inserted == 1
        assert len(result.actions) == 1
        action = result.actions[0]
        assert action.action == "insert_pwr_flag"
        assert action.x == 100.0
        assert action.y == pytest.approx(52.54, abs=0.01)

    def test_multiple_pwr_flags(self):
        """Multiple power_pin_not_driven violations each get a PWR_FLAG."""
        violations = [
            _make_violation(
                ERCViolationType.POWER_PIN_NOT_DRIVEN,
                "power_pin_not_driven",
                "VCC not driven",
                pos_x=100.0,
                pos_y=50.0,
            ),
            _make_violation(
                ERCViolationType.POWER_PIN_NOT_DRIVEN,
                "power_pin_not_driven",
                "GND not driven",
                pos_x=150.0,
                pos_y=75.0,
            ),
        ]
        report = _make_report(violations)

        result = _apply_fixes(Path("dummy.kicad_sch"), report, dry_run=True, quiet=True)

        assert result.pwr_flag_inserted == 2


# ── Tests: pin_not_connected fix ─────────────────────────────────────


class TestPinNotConnected:
    """pin_not_connected violations should produce no-connect markers."""

    def test_no_connect_at_exact_position(self):
        """No-connect marker should be placed at the exact pin position."""
        violations = [
            _make_violation(
                ERCViolationType.PIN_NOT_CONNECTED,
                "pin_not_connected",
                "Pin not connected",
                pos_x=120.0,
                pos_y=60.0,
            ),
        ]
        report = _make_report(violations)

        result = _apply_fixes(Path("dummy.kicad_sch"), report, dry_run=True, quiet=True)

        assert result.no_connect_inserted == 1
        assert len(result.actions) == 1
        action = result.actions[0]
        assert action.action == "insert_no_connect"
        assert action.x == 120.0
        assert action.y == 60.0

    def test_multiple_no_connects(self):
        """Multiple pin_not_connected violations each get a no-connect marker."""
        violations = [
            _make_violation(
                ERCViolationType.PIN_NOT_CONNECTED,
                "pin_not_connected",
                "Pin 4 not connected",
                pos_x=120.0,
                pos_y=60.0,
            ),
            _make_violation(
                ERCViolationType.PIN_NOT_CONNECTED,
                "pin_not_connected",
                "Pin 5 not connected",
                pos_x=130.0,
                pos_y=70.0,
            ),
        ]
        report = _make_report(violations)

        result = _apply_fixes(Path("dummy.kicad_sch"), report, dry_run=True, quiet=True)

        assert result.no_connect_inserted == 2


# ── Tests: unknown violations are skipped ────────────────────────────


class TestUnknownViolations:
    """Unknown/unhandled violation types should be skipped, not cause errors."""

    def test_unknown_type_skipped(self):
        """UNKNOWN violations should be counted as skipped."""
        violations = [
            _make_violation(
                ERCViolationType.UNKNOWN,
                "some_exotic_rule",
                "Exotic check failed",
                pos_x=50.0,
                pos_y=50.0,
            ),
        ]
        report = _make_report(violations)

        result = _apply_fixes(Path("dummy.kicad_sch"), report, dry_run=True, quiet=True)

        assert result.skipped_unknown == 1
        assert result.total_fixed == 0
        assert result.total_violations == 0  # unknown not counted in targeted

    def test_unknown_with_fixable_mixed(self):
        """Unknown violations mixed with fixable ones; fixable are processed normally."""
        violations = [
            _make_violation(
                ERCViolationType.UNKNOWN,
                "unknown_rule",
                "Some unknown check",
                pos_x=10.0,
                pos_y=10.0,
            ),
            _make_violation(
                ERCViolationType.PIN_NOT_CONNECTED,
                "pin_not_connected",
                "Pin 1 unconnected",
                pos_x=100.0,
                pos_y=50.0,
            ),
        ]
        report = _make_report(violations)

        result = _apply_fixes(Path("dummy.kicad_sch"), report, dry_run=True, quiet=True)

        assert result.skipped_unknown == 1
        assert result.no_connect_inserted == 1
        assert result.total_fixed == 1

    def test_unknown_generates_warning(self, capsys):
        """Unknown violations should produce a warning message."""
        violations = [
            _make_violation(
                ERCViolationType.UNKNOWN,
                "exotic_check",
                "Exotic check failed",
                pos_x=50.0,
                pos_y=50.0,
            ),
        ]
        report = _make_report(violations)

        _apply_fixes(Path("dummy.kicad_sch"), report, dry_run=True, quiet=False)

        captured = capsys.readouterr()
        assert "Warning: Skipping UNKNOWN violation type 'exotic_check'" in captured.err


# ── Tests: exit code behavior ────────────────────────────────────────


class TestExitCode:
    """Exit code should reflect fix completeness."""

    def test_exit_code_zero_when_all_fixed(self, tmp_path, capsys):
        """Exit code 0 when all targeted violations are fixed."""
        sch_file = tmp_path / "board.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC)

        # Create a report with only fixable violations
        report_data = {
            "source": "board.kicad_sch",
            "kicad_version": "8.0.0",
            "coordinate_units": "mm",
            "sheets": [
                {
                    "path": "/",
                    "violations": [
                        {
                            "type": "pin_not_connected",
                            "severity": "error",
                            "description": "Pin 1 not connected",
                            "pos": {"x": 100, "y": 50},
                            "items": [],
                            "excluded": False,
                        }
                    ],
                }
            ],
        }
        report_file = tmp_path / "erc.json"
        report_file.write_text(json.dumps(report_data))

        # dry-run so we don't need a real schematic
        exit_code = main([str(sch_file), "--erc-report", str(report_file), "--dry-run", "--quiet"])
        assert exit_code == 0

    def test_exit_code_zero_no_violations(self, tmp_path):
        """Exit code 0 when there are zero violations."""
        sch_file = tmp_path / "board.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC)

        report_data = {
            "source": "board.kicad_sch",
            "kicad_version": "8.0.0",
            "coordinate_units": "mm",
            "sheets": [],
        }
        report_file = tmp_path / "erc.json"
        report_file.write_text(json.dumps(report_data))

        exit_code = main([str(sch_file), "--erc-report", str(report_file), "--quiet"])
        assert exit_code == 0

    def test_exit_code_zero_when_duplicates_all_resolved(self, tmp_path):
        """Exit code 0 when all violations share a position and one fix covers them all.

        Regression test for the skipped_duplicate accounting bug: when two violations
        share the same coordinates, total_violations==2, total_fixed==1, and
        skipped_duplicate==1.  The exit-code formula must subtract skipped_duplicate
        so that remaining==0 and the command returns 0.
        """
        sch_file = tmp_path / "board.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC)

        report_data = {
            "source": "board.kicad_sch",
            "kicad_version": "8.0.0",
            "coordinate_units": "mm",
            "sheets": [
                {
                    "path": "/",
                    "violations": [
                        {
                            "type": "pin_not_connected",
                            "severity": "error",
                            "description": "Pin 1 not connected",
                            "pos": {"x": 100, "y": 50},
                            "items": [],
                            "excluded": False,
                        },
                        {
                            "type": "pin_not_connected",
                            "severity": "error",
                            "description": "Pin 2 not connected",
                            "pos": {"x": 100, "y": 50},
                            "items": [],
                            "excluded": False,
                        },
                    ],
                }
            ],
        }
        report_file = tmp_path / "erc.json"
        report_file.write_text(json.dumps(report_data))

        exit_code = main([str(sch_file), "--erc-report", str(report_file), "--dry-run", "--quiet"])
        assert exit_code == 0


# ── Tests: duplicate position handling ───────────────────────────────


class TestDuplicatePositions:
    """Multiple violations at the same position should not insert duplicate markers."""

    def test_duplicate_no_connect_position(self):
        """Two pin_not_connected at same position should insert only one marker."""
        violations = [
            _make_violation(
                ERCViolationType.PIN_NOT_CONNECTED,
                "pin_not_connected",
                "Pin 1 not connected",
                pos_x=120.0,
                pos_y=60.0,
            ),
            _make_violation(
                ERCViolationType.PIN_NOT_CONNECTED,
                "pin_not_connected",
                "Pin 2 not connected",
                pos_x=120.0,
                pos_y=60.0,
            ),
        ]
        report = _make_report(violations)

        result = _apply_fixes(Path("dummy.kicad_sch"), report, dry_run=True, quiet=True)

        assert result.no_connect_inserted == 1
        assert result.skipped_duplicate == 1

    def test_duplicate_pwr_flag_position(self):
        """Two power_pin_not_driven at same position should insert only one PWR_FLAG."""
        violations = [
            _make_violation(
                ERCViolationType.POWER_PIN_NOT_DRIVEN,
                "power_pin_not_driven",
                "VCC not driven (1)",
                pos_x=100.0,
                pos_y=50.0,
            ),
            _make_violation(
                ERCViolationType.POWER_PIN_NOT_DRIVEN,
                "power_pin_not_driven",
                "VCC not driven (2)",
                pos_x=100.0,
                pos_y=50.0,
            ),
        ]
        report = _make_report(violations)

        result = _apply_fixes(Path("dummy.kicad_sch"), report, dry_run=True, quiet=True)

        assert result.pwr_flag_inserted == 1
        assert result.skipped_duplicate == 1


# ── Tests: JSON fixture loading ──────────────────────────────────────


class TestFixtureLoading:
    """Test loading the sample_erc_fixable.json fixture."""

    def test_fixture_loads(self):
        """The fixture should parse correctly as an ERC report."""
        report = ERCReport.load(SAMPLE_ERC_FIXABLE)
        assert report.violation_count == 5
        assert len(report.by_type(ERCViolationType.POWER_PIN_NOT_DRIVEN)) == 2
        assert len(report.by_type(ERCViolationType.PIN_NOT_CONNECTED)) == 3

    def test_fixture_has_subsheet_violations(self):
        """Fixture should include violations from subsheet paths."""
        report = ERCReport.load(SAMPLE_ERC_FIXABLE)
        subsheet_violations = report.by_sheet("/subsheet")
        assert len(subsheet_violations) == 1


# ── Tests: output format ─────────────────────────────────────────────


class TestOutputFormat:
    """Test the different output formats."""

    def test_json_output(self, tmp_path, capsys):
        """JSON output should be valid JSON with expected fields."""
        sch_file = tmp_path / "board.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC)

        report_file = tmp_path / "erc.json"
        report_file.write_text(SAMPLE_ERC_FIXABLE.read_text())

        main([str(sch_file), "--erc-report", str(report_file), "--dry-run", "--format", "json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "total_violations" in data
        assert "total_fixed" in data
        assert "actions" in data
        assert isinstance(data["actions"], list)

    def test_summary_output(self, tmp_path, capsys):
        """Summary output should include fix counts."""
        sch_file = tmp_path / "board.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC)

        report_file = tmp_path / "erc.json"
        report_file.write_text(SAMPLE_ERC_FIXABLE.read_text())

        main([str(sch_file), "--erc-report", str(report_file), "--dry-run", "--format", "summary"])

        captured = capsys.readouterr()
        assert "Would fix" in captured.out
        assert "PWR_FLAG" in captured.out


# ── Tests: error handling ────────────────────────────────────────────


class TestErrorHandling:
    """Test error cases."""

    def test_missing_schematic_file(self, capsys):
        """Should return error when schematic file doesn't exist."""
        exit_code = main(["nonexistent.kicad_sch", "--erc-report", "dummy.json"])
        assert exit_code == 1

    def test_wrong_file_extension(self, tmp_path, capsys):
        """Should return error for non-.kicad_sch files."""
        wrong_file = tmp_path / "board.kicad_pcb"
        wrong_file.write_text("")
        exit_code = main([str(wrong_file)])
        assert exit_code == 1

    def test_missing_erc_report(self, tmp_path, capsys):
        """Should return error when ERC report path doesn't exist."""
        sch_file = tmp_path / "board.kicad_sch"
        sch_file.write_text(MINIMAL_SCHEMATIC)
        exit_code = main([str(sch_file), "--erc-report", str(tmp_path / "missing.json")])
        assert exit_code == 1


# ── Tests: FixERCResult dataclass ────────────────────────────────────


class TestFixERCResult:
    """Test FixERCResult properties."""

    def test_total_fixed(self):
        result = FixERCResult(pwr_flag_inserted=2, no_connect_inserted=3)
        assert result.total_fixed == 5

    def test_total_skipped(self):
        result = FixERCResult(skipped_unknown=1, skipped_duplicate=2)
        assert result.total_skipped == 3


# ── Tests: unhandled but non-UNKNOWN types ───────────────────────────


class TestUnhandledTypes:
    """Non-fixable, non-UNKNOWN types should not be in the fix count."""

    def test_label_dangling_not_fixed(self):
        """LABEL_DANGLING is not auto-fixable; should not count as targeted."""
        violations = [
            _make_violation(
                ERCViolationType.LABEL_DANGLING,
                "label_dangling",
                "Label not connected",
                pos_x=50.0,
                pos_y=50.0,
            ),
        ]
        report = _make_report(violations)

        result = _apply_fixes(Path("dummy.kicad_sch"), report, dry_run=True, quiet=True)

        assert result.total_violations == 0
        assert result.total_fixed == 0
        # Not counted as unknown since it has a known type
        assert result.skipped_unknown == 0
