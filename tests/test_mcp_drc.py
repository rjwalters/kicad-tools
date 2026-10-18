"""Tests for the MCP DRC analysis tool.

Tests the get_drc_violations MCP tool for running Design Rule Checks
on PCB files and returning violations with locations and fix suggestions.
"""

import json
import shutil
from pathlib import Path

import pytest


class TestDRCTypes:
    """Tests for DRC type models."""

    def test_drc_result_passed(self):
        """Test DRCResult with passing status."""
        from kicad_tools.mcp.types import DRCResult

        result = DRCResult(
            passed=True,
            violation_count=0,
            error_count=0,
            warning_count=0,
            violations=[],
            summary_by_type={},
            manufacturer="jlcpcb",
            layers=4,
            verdict="pass",
            exit_code=0,
        )

        assert result.passed is True
        assert result.violation_count == 0
        assert result.manufacturer == "jlcpcb"

    def test_drc_result_failed(self):
        """Test DRCResult with failing status."""
        from kicad_tools.mcp.types import DRCResult, DRCViolation, ViolationLocation

        violation = DRCViolation(
            id="drc-0001-abc123",
            type="clearance_trace_pad",
            severity="error",
            message="Clearance violation",
            location=ViolationLocation(x_mm=10.0, y_mm=20.0, layer="F.Cu"),
            affected_items=[],
            fix_suggestion="Increase spacing by 0.1mm",
        )

        result = DRCResult(
            passed=False,
            violation_count=1,
            error_count=1,
            warning_count=0,
            violations=[violation],
            summary_by_type={"clearance_trace_pad": 1},
            manufacturer="jlcpcb",
            layers=4,
            verdict="fail",
            exit_code=2,
        )

        assert result.passed is False
        assert result.violation_count == 1
        assert len(result.violations) == 1
        assert result.violations[0].type == "clearance_trace_pad"

    def test_violation_location(self):
        """Test ViolationLocation model."""
        from kicad_tools.mcp.types import ViolationLocation

        loc = ViolationLocation(x_mm=45.2, y_mm=32.1, layer="F.Cu")

        assert loc.x_mm == 45.2
        assert loc.y_mm == 32.1
        assert loc.layer == "F.Cu"

    def test_affected_item(self):
        """Test AffectedItem model."""
        from kicad_tools.mcp.types import AffectedItem

        item = AffectedItem(
            item_type="pad",
            reference="U1",
            net="GND",
        )

        assert item.item_type == "pad"
        assert item.reference == "U1"
        assert item.net == "GND"

    def test_drc_result_to_dict(self):
        """Test DRCResult serialization."""
        from kicad_tools.mcp.types import DRCResult

        result = DRCResult(
            passed=True,
            violation_count=0,
            error_count=0,
            warning_count=0,
            manufacturer="jlcpcb",
            layers=4,
            verdict="pass",
            exit_code=0,
        )

        d = result.to_dict()
        assert d["passed"] is True
        assert d["manufacturer"] == "jlcpcb"
        assert d["layers"] == 4
        assert d["verdict"] == "pass"
        assert d["exit_code"] == 0
        assert d["notes"] == []


class TestGetDRCViolations:
    """Tests for the get_drc_violations MCP tool."""

    def test_file_not_found(self, tmp_path: Path):
        """Test error when PCB file doesn't exist."""
        from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        with pytest.raises(KiCadFileNotFoundError):
            get_drc_violations(str(tmp_path / "nonexistent.kicad_pcb"))

    def test_invalid_file_extension(self, tmp_path: Path):
        """Test error for non-.kicad_pcb file."""
        from kicad_tools.exceptions import ParseError
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        txt_file = tmp_path / "test.txt"
        txt_file.write_text("not a pcb")

        with pytest.raises(ParseError, match="Invalid file extension"):
            get_drc_violations(str(txt_file))

    def test_unknown_manufacturer(self, drc_clean_pcb: Path):
        """Test error for unknown manufacturer preset."""
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        with pytest.raises(ValueError, match="Unknown manufacturer preset"):
            get_drc_violations(str(drc_clean_pcb), rules="unknown_fab")

    def test_clean_pcb_has_no_drc_errors(self, drc_clean_pcb: Path):
        """A DRC-clean PCB with no schematic is ``incomplete``, like ``kct check``.

        Issue #7260: the verdict includes the ERC + LVS meta rollup, which
        cannot run without a schematic -- so the board is not ``pass``.
        """
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        result = get_drc_violations(str(drc_clean_pcb), rules="jlcpcb")

        assert result.error_count == 0
        assert result.manufacturer == "jlcpcb"
        assert result.layers == 2  # resolved from the board, not a default
        assert result.verdict == "incomplete"
        assert result.exit_code == 2
        assert result.passed is False

        allowed = get_drc_violations(str(drc_clean_pcb), rules="jlcpcb", allow_incomplete=True)
        assert allowed.verdict == "incomplete"
        assert allowed.exit_code == 0
        assert allowed.passed is False

    def test_manufacturer_presets(self, drc_clean_pcb: Path):
        """Test that all manufacturer presets work."""
        from kicad_tools.mcp.tools.analysis import (
            MANUFACTURER_PRESETS,
            get_drc_violations,
        )

        for mfr in MANUFACTURER_PRESETS:
            result = get_drc_violations(str(drc_clean_pcb), rules=mfr)
            assert result.manufacturer == mfr

    def test_default_manufacturer(self, drc_clean_pcb: Path):
        """Test that default manufacturer is jlcpcb."""
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        result = get_drc_violations(str(drc_clean_pcb))
        assert result.manufacturer == "jlcpcb"

    def test_severity_filter_errors_only(self, minimal_pcb: Path):
        """Test filtering to errors only."""
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        result = get_drc_violations(str(minimal_pcb), severity_filter="error")

        # All returned violations should be errors
        for v in result.violations:
            assert v.severity == "error"

    def test_severity_filter_warnings_only(self, minimal_pcb: Path):
        """Test filtering to warnings only."""
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        result = get_drc_violations(str(minimal_pcb), severity_filter="warning")

        # All returned violations should be warnings
        for v in result.violations:
            assert v.severity == "warning"

    def test_violation_has_location(self, minimal_pcb: Path):
        """Test that violations include location information."""
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        result = get_drc_violations(str(minimal_pcb))

        # If there are violations, they should have locations
        for v in result.violations:
            assert hasattr(v.location, "x_mm")
            assert hasattr(v.location, "y_mm")

    def test_violation_has_unique_id(self, minimal_pcb: Path):
        """Test that each violation has a unique ID."""
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        result = get_drc_violations(str(minimal_pcb))

        if len(result.violations) > 1:
            ids = [v.id for v in result.violations]
            assert len(ids) == len(set(ids)), "Violation IDs should be unique"

    def test_summary_by_type(self, minimal_pcb: Path):
        """Test that summary_by_type counts violations correctly."""
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        result = get_drc_violations(str(minimal_pcb))

        # Sum of summary should equal violation count (may not match if filtered)
        total_in_summary = sum(result.summary_by_type.values())
        assert total_in_summary == result.violation_count

    def test_layers_parameter_removed(self, drc_clean_pcb: Path):
        """Issue #7260: the layer count comes from the board, never a param."""
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        with pytest.raises(TypeError):
            get_drc_violations(str(drc_clean_pcb), layers=4)  # type: ignore[call-arg]


BOARD_00 = Path(__file__).resolve().parent.parent / "boards" / "00-simple-led" / "output"


@pytest.fixture
def narrow_track_board(tmp_path: Path) -> Path:
    """Scratch copy of board 00 (2-layer) with both tracks narrowed to 0.11 mm.

    0.11 mm sits between JLCPCB's 4-layer (0.1016 mm) and 2-layer (0.127 mm)
    minimum trace widths, so it fails only when the board's real layer count
    is used (Issue #7260 repro 2).
    """
    if not BOARD_00.is_dir():
        pytest.skip("boards/00-simple-led/output not present")
    for name in (
        "simple_led_routed.kicad_pcb",
        "simple_led_routed.kicad_pro",
        "simple_led_routed.kicad_dru",
        "simple_led.kicad_sch",
        "simple_led.kicad_pro",
    ):
        shutil.copy2(BOARD_00 / name, tmp_path / name)
    pcb = tmp_path / "simple_led_routed.kicad_pcb"
    text = pcb.read_text()
    assert text.count("(width 0.2)") == 2
    pcb.write_text(text.replace("(width 0.2)", "(width 0.11)"))
    return pcb


class TestCheckParity7260:
    """``get_drc_violations`` reports ``kct check``'s verdict (Issue #7260)."""

    def test_narrow_tracks_fail_on_two_layer_rules(self, narrow_track_board: Path):
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        result = get_drc_violations(str(narrow_track_board), rules="jlcpcb")

        assert result.passed is False
        assert result.verdict == "fail"
        assert result.exit_code == 2
        assert result.layers == 2
        assert result.summary_by_type.get("dimension_trace_width") == 2

    def test_verdict_matches_kct_check_json(self, narrow_track_board: Path, capsys):
        from kicad_tools.cli import check_cmd
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        rc = check_cmd.main([str(narrow_track_board), "--mfr", "jlcpcb", "--format", "json"])
        cli = json.loads(capsys.readouterr().out)

        result = get_drc_violations(str(narrow_track_board), rules="jlcpcb")

        assert (result.verdict, result.exit_code) == (cli["verdict"], cli["exit_code"])
        assert result.exit_code == rc
        assert result.error_count == cli["summary"]["errors"]

    def test_severity_filter_does_not_change_verdict(self, narrow_track_board: Path):
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        result = get_drc_violations(
            str(narrow_track_board), rules="jlcpcb", severity_filter="warning"
        )

        assert all(v.severity == "warning" for v in result.violations)
        assert result.passed is False
        assert result.verdict == "fail"

    def test_mcp_server_call(self, narrow_track_board: Path):
        from kicad_tools.mcp.server import MCPServer

        out = MCPServer().call_tool(
            "get_drc_violations",
            {"pcb_path": str(narrow_track_board), "manufacturer": "jlcpcb"},
        )
        assert out["passed"] is False
        assert out["verdict"] == "fail"
        assert out["exit_code"] == 2
        assert out["layers"] == 2

    def test_manufacturer_resolved_like_kct_check(self, drc_clean_pcb: Path):
        """No ``manufacturer`` -> ``fab_profile.json`` wins, as for ``kct check``."""
        from kicad_tools.mcp.tools.analysis import get_drc_violations

        (drc_clean_pcb.parent / "fab_profile.json").write_text(json.dumps({"mfr": "pcbway"}))

        assert get_drc_violations(str(drc_clean_pcb)).manufacturer == "pcbway"
        # An explicit manufacturer still wins.
        assert get_drc_violations(str(drc_clean_pcb), rules="jlcpcb").manufacturer == "jlcpcb"


class TestFixSuggestionGeneration:
    """Tests for fix suggestion generation."""

    def test_clearance_fix_suggestion(self):
        """Test fix suggestion for clearance violations."""
        from kicad_tools.mcp.tools.analysis import _generate_fix_suggestion

        suggestion = _generate_fix_suggestion(
            rule_id="clearance_trace_pad",
            required_value=0.15,
            actual_value=0.10,
        )

        assert suggestion is not None
        assert "clearance" in suggestion.lower()

    def test_track_width_fix_suggestion(self):
        """Test fix suggestion for track width violations."""
        from kicad_tools.mcp.tools.analysis import _generate_fix_suggestion

        suggestion = _generate_fix_suggestion(
            rule_id="track_width_min",
            required_value=0.20,
            actual_value=0.15,
        )

        assert suggestion is not None
        assert "track" in suggestion.lower()

    def test_via_fix_suggestion(self):
        """Test fix suggestion for via violations."""
        from kicad_tools.mcp.tools.analysis import _generate_fix_suggestion

        suggestion = _generate_fix_suggestion(
            rule_id="via_drill_min",
            required_value=0.30,
            actual_value=0.25,
        )

        assert suggestion is not None
        assert "via" in suggestion.lower()

    def test_no_suggestion_without_values(self):
        """Test that no suggestion is generated without values."""
        from kicad_tools.mcp.tools.analysis import _generate_fix_suggestion

        suggestion = _generate_fix_suggestion(
            rule_id="unknown_rule",
            required_value=None,
            actual_value=None,
        )

        assert suggestion is None
