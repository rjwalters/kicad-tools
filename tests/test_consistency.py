"""Tests for kicad_tools.validate.consistency module."""

from pathlib import Path

import pytest

from kicad_tools.validate.consistency import (
    ConsistencyIssue,
    ConsistencyResult,
    SchematicPCBChecker,
)


class TestConsistencyIssue:
    """Tests for ConsistencyIssue dataclass."""

    def test_valid_issue_creation(self):
        """Test creating a valid consistency issue."""
        issue = ConsistencyIssue(
            issue_type="missing",
            domain="component",
            schematic_value="R1",
            pcb_value=None,
            reference="R1",
            severity="error",
            suggestion="Add footprint for R1 to PCB",
        )
        assert issue.issue_type == "missing"
        assert issue.domain == "component"
        assert issue.is_error
        assert not issue.is_warning

    def test_warning_issue(self):
        """Test creating a warning issue."""
        issue = ConsistencyIssue(
            issue_type="extra",
            domain="component",
            schematic_value=None,
            pcb_value="TP1",
            reference="TP1",
            severity="warning",
            suggestion="Remove TP1 from PCB or add to schematic",
        )
        assert issue.is_warning
        assert not issue.is_error

    def test_invalid_issue_type_raises(self):
        """Test that invalid issue_type raises ValueError."""
        with pytest.raises(ValueError, match="issue_type must be"):
            ConsistencyIssue(
                issue_type="invalid",
                domain="component",
                schematic_value="R1",
                pcb_value=None,
                reference="R1",
                severity="error",
                suggestion="Fix it",
            )

    def test_invalid_domain_raises(self):
        """Test that invalid domain raises ValueError."""
        with pytest.raises(ValueError, match="domain must be"):
            ConsistencyIssue(
                issue_type="missing",
                domain="invalid",
                schematic_value="R1",
                pcb_value=None,
                reference="R1",
                severity="error",
                suggestion="Fix it",
            )

    def test_invalid_severity_raises(self):
        """Test that invalid severity raises ValueError."""
        with pytest.raises(ValueError, match="severity must be"):
            ConsistencyIssue(
                issue_type="missing",
                domain="component",
                schematic_value="R1",
                pcb_value=None,
                reference="R1",
                severity="critical",
                suggestion="Fix it",
            )

    def test_to_dict(self):
        """Test conversion to dictionary."""
        issue = ConsistencyIssue(
            issue_type="mismatch",
            domain="property",
            schematic_value="10k",
            pcb_value="10K",
            reference="R1",
            severity="warning",
            suggestion="Update R1 value",
        )
        d = issue.to_dict()
        assert d["issue_type"] == "mismatch"
        assert d["domain"] == "property"
        assert d["schematic_value"] == "10k"
        assert d["pcb_value"] == "10K"


class TestConsistencyResult:
    """Tests for ConsistencyResult dataclass."""

    def test_empty_result_is_consistent(self):
        """Test that empty result is consistent."""
        result = ConsistencyResult(issues=[])
        assert result.is_consistent
        assert result.error_count == 0
        assert result.warning_count == 0

    def test_result_with_errors_is_inconsistent(self):
        """Test that result with errors is inconsistent."""
        issues = [
            ConsistencyIssue(
                issue_type="missing",
                domain="component",
                schematic_value="R1",
                pcb_value=None,
                reference="R1",
                severity="error",
                suggestion="Add R1",
            ),
        ]
        result = ConsistencyResult(issues=issues)
        assert not result.is_consistent
        assert result.error_count == 1

    def test_result_with_only_warnings_is_consistent(self):
        """Test that result with only warnings is still consistent."""
        issues = [
            ConsistencyIssue(
                issue_type="extra",
                domain="component",
                schematic_value=None,
                pcb_value="TP1",
                reference="TP1",
                severity="warning",
                suggestion="Remove TP1",
            ),
        ]
        result = ConsistencyResult(issues=issues)
        assert result.is_consistent
        assert result.warning_count == 1

    def test_filtering_by_domain(self):
        """Test filtering issues by domain."""
        issues = [
            ConsistencyIssue(
                issue_type="missing",
                domain="component",
                schematic_value="R1",
                pcb_value=None,
                reference="R1",
                severity="error",
                suggestion="Add R1",
            ),
            ConsistencyIssue(
                issue_type="mismatch",
                domain="property",
                schematic_value="10k",
                pcb_value="10K",
                reference="R2",
                severity="warning",
                suggestion="Update value",
            ),
            ConsistencyIssue(
                issue_type="mismatch",
                domain="net",
                schematic_value="VCC",
                pcb_value="VDD",
                reference="U1.1",
                severity="error",
                suggestion="Update net",
            ),
        ]
        result = ConsistencyResult(issues=issues)

        assert len(result.component_issues) == 1
        assert len(result.property_issues) == 1
        assert len(result.net_issues) == 1

    def test_summary(self):
        """Test summary generation."""
        issues = [
            ConsistencyIssue(
                issue_type="missing",
                domain="component",
                schematic_value="R1",
                pcb_value=None,
                reference="R1",
                severity="error",
                suggestion="Add R1",
            ),
        ]
        result = ConsistencyResult(issues=issues)
        summary = result.summary()
        assert "INCONSISTENT" in summary
        assert "1 errors" in summary


# Fixture: Schematic with component C1 that's not in PCB
SCHEMATIC_WITH_EXTRA_COMPONENT = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "Device:C")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000005")
    (property "Reference" "C1" (at 120 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 120 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 120 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 120 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000006"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000007"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C1")
          (unit 1)
        )
      )
    )
  )
)
"""

# Fixture: PCB with component TP1 that's not in schematic
PCB_WITH_EXTRA_COMPONENT = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "00000000-0000-0000-0000-000000000013"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
  (footprint "TestPoint:TestPoint_Pad_1.0x1.0mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 110 100)
    (property "Reference" "TP1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000021"))
    (property "Value" "TestPoint" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000022"))
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))
  )
)
"""

# Fixture: PCB with mismatched value
PCB_WITH_VALUE_MISMATCH = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "4.7k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "00000000-0000-0000-0000-000000000013"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
)
"""


@pytest.fixture
def schematic_with_extra(tmp_path: Path) -> Path:
    """Create schematic with extra component C1."""
    sch_file = tmp_path / "extra.kicad_sch"
    sch_file.write_text(SCHEMATIC_WITH_EXTRA_COMPONENT)
    return sch_file


@pytest.fixture
def pcb_with_extra(tmp_path: Path) -> Path:
    """Create PCB with extra component TP1."""
    pcb_file = tmp_path / "extra.kicad_pcb"
    pcb_file.write_text(PCB_WITH_EXTRA_COMPONENT)
    return pcb_file


@pytest.fixture
def pcb_with_value_mismatch(tmp_path: Path) -> Path:
    """Create PCB with value mismatch (R1: 4.7k instead of 10k)."""
    pcb_file = tmp_path / "mismatch.kicad_pcb"
    pcb_file.write_text(PCB_WITH_VALUE_MISMATCH)
    return pcb_file


class TestSchematicPCBChecker:
    """Tests for SchematicPCBChecker class."""

    def test_consistent_schematic_pcb(self, minimal_schematic: Path, minimal_pcb: Path):
        """Test that consistent schematic and PCB pass checks."""
        checker = SchematicPCBChecker(minimal_schematic, minimal_pcb)
        result = checker.check()

        # Should be consistent (R1 is in both)
        assert result.is_consistent
        assert result.error_count == 0

    def test_detect_missing_on_pcb(self, schematic_with_extra: Path, pcb_with_extra: Path):
        """Test detection of component in schematic but not on PCB."""
        checker = SchematicPCBChecker(schematic_with_extra, pcb_with_extra)
        result = checker.check()

        # C1 is in schematic but not on PCB
        missing = [i for i in result.component_issues if i.issue_type == "missing"]
        assert len(missing) == 1
        assert missing[0].reference == "C1"
        assert missing[0].is_error

    def test_detect_extra_on_pcb(self, schematic_with_extra: Path, pcb_with_extra: Path):
        """Test detection of component on PCB but not in schematic."""
        checker = SchematicPCBChecker(schematic_with_extra, pcb_with_extra)
        result = checker.check()

        # TP1 is on PCB but not in schematic
        extra = [i for i in result.component_issues if i.issue_type == "extra"]
        assert len(extra) == 1
        assert extra[0].reference == "TP1"
        assert extra[0].is_warning

    def test_detect_value_mismatch(self, minimal_schematic: Path, pcb_with_value_mismatch: Path):
        """Test detection of value property mismatch."""
        checker = SchematicPCBChecker(minimal_schematic, pcb_with_value_mismatch)
        result = checker.check()

        # R1 has value 10k in schematic, 4.7k on PCB
        value_mismatches = [
            i
            for i in result.property_issues
            if i.issue_type == "mismatch" and "value" in i.suggestion.lower()
        ]
        assert len(value_mismatches) == 1
        assert value_mismatches[0].schematic_value == "10k"
        assert value_mismatches[0].pcb_value == "4.7k"

    def test_repr(self, minimal_schematic: Path, minimal_pcb: Path):
        """Test string representation."""
        checker = SchematicPCBChecker(minimal_schematic, minimal_pcb)
        repr_str = repr(checker)
        assert "SchematicPCBChecker" in repr_str
        assert "schematic_symbols=" in repr_str
        assert "pcb_footprints=" in repr_str


class TestConsistencyResultOutput:
    """Tests for ConsistencyResult output methods."""

    def test_to_dict_structure(self):
        """Test to_dict produces correct structure."""
        issues = [
            ConsistencyIssue(
                issue_type="missing",
                domain="component",
                schematic_value="R1",
                pcb_value=None,
                reference="R1",
                severity="error",
                suggestion="Add R1",
            ),
        ]
        result = ConsistencyResult(issues=issues)
        d = result.to_dict()

        assert "is_consistent" in d
        assert "error_count" in d
        assert "warning_count" in d
        assert "issues" in d
        assert isinstance(d["issues"], list)
        assert len(d["issues"]) == 1

    def test_iteration(self):
        """Test iterating over result."""
        issues = [
            ConsistencyIssue(
                issue_type="missing",
                domain="component",
                schematic_value="R1",
                pcb_value=None,
                reference="R1",
                severity="error",
                suggestion="Add R1",
            ),
            ConsistencyIssue(
                issue_type="extra",
                domain="component",
                schematic_value=None,
                pcb_value="TP1",
                reference="TP1",
                severity="warning",
                suggestion="Remove TP1",
            ),
        ]
        result = ConsistencyResult(issues=issues)

        refs = [i.reference for i in result]
        assert refs == ["R1", "TP1"]

    def test_len(self):
        """Test len() on result."""
        issues = [
            ConsistencyIssue(
                issue_type="missing",
                domain="component",
                schematic_value="R1",
                pcb_value=None,
                reference="R1",
                severity="error",
                suggestion="Add R1",
            ),
        ]
        result = ConsistencyResult(issues=issues)
        assert len(result) == 1

    def test_bool_empty(self):
        """Test bool() on empty result."""
        result = ConsistencyResult(issues=[])
        assert not result

    def test_bool_with_issues(self):
        """Test bool() on result with issues."""
        issues = [
            ConsistencyIssue(
                issue_type="missing",
                domain="component",
                schematic_value="R1",
                pcb_value=None,
                reference="R1",
                severity="error",
                suggestion="Add R1",
            ),
        ]
        result = ConsistencyResult(issues=issues)
        assert result
