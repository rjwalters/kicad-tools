"""Tests for kicad_tools.validate.netlist module (schematic-to-PCB sync)."""

import pytest

from kicad_tools.validate.netlist import NetlistValidator, SyncIssue, SyncResult


class TestSyncIssue:
    """Tests for the SyncIssue dataclass."""

    def test_issue_creation(self):
        """Test creating a basic sync issue."""
        issue = SyncIssue(
            severity="error",
            category="missing_on_pcb",
            message="R5 missing on PCB",
            suggestion="Add footprint for R5",
            reference="R5",
        )
        assert issue.severity == "error"
        assert issue.category == "missing_on_pcb"
        assert issue.message == "R5 missing on PCB"
        assert issue.suggestion == "Add footprint for R5"
        assert issue.reference == "R5"

    def test_issue_with_net_info(self):
        """Test issue with net name information."""
        issue = SyncIssue(
            severity="error",
            category="net_mismatch",
            message='Net "VCC" on schematic is "VDD" on PCB',
            suggestion='Rename net on PCB to "VCC"',
            net_schematic="VCC",
            net_pcb="VDD",
        )
        assert issue.net_schematic == "VCC"
        assert issue.net_pcb == "VDD"

    def test_issue_with_pin_info(self):
        """Test issue with pin information."""
        issue = SyncIssue(
            severity="warning",
            category="pin_mismatch",
            message="Pin 3 of U1 not connected on PCB",
            suggestion="Connect U1 pin 3 to net GND",
            reference="U1",
            pin="3",
        )
        assert issue.pin == "3"
        assert issue.reference == "U1"

    def test_issue_is_hashable(self):
        """Test that issues are hashable (frozen dataclass)."""
        issue = SyncIssue(
            severity="error",
            category="missing_on_pcb",
            message="test",
            suggestion="test",
        )
        # Should not raise
        hash(issue)
        # Should work in sets
        issues = {issue, issue}
        assert len(issues) == 1

    def test_issue_is_error(self):
        """Test is_error and is_warning properties."""
        error = SyncIssue(
            severity="error",
            category="missing_on_pcb",
            message="test",
            suggestion="test",
        )
        warning = SyncIssue(
            severity="warning",
            category="orphaned_on_pcb",
            message="test",
            suggestion="test",
        )

        assert error.is_error is True
        assert error.is_warning is False
        assert warning.is_error is False
        assert warning.is_warning is True

    def test_issue_invalid_severity(self):
        """Test that invalid severity raises ValueError."""
        with pytest.raises(ValueError, match="severity must be"):
            SyncIssue(
                severity="info",
                category="missing_on_pcb",
                message="test",
                suggestion="test",
            )

    def test_issue_invalid_category(self):
        """Test that invalid category raises ValueError."""
        with pytest.raises(ValueError, match="category must be"):
            SyncIssue(
                severity="error",
                category="invalid_category",
                message="test",
                suggestion="test",
            )

    def test_issue_to_dict(self):
        """Test converting issue to dictionary."""
        issue = SyncIssue(
            severity="error",
            category="net_mismatch",
            message="Test message",
            suggestion="Test suggestion",
            reference="U1",
            net_schematic="VCC",
            net_pcb="VDD",
            pin="1",
        )
        d = issue.to_dict()

        assert d["severity"] == "error"
        assert d["category"] == "net_mismatch"
        assert d["message"] == "Test message"
        assert d["suggestion"] == "Test suggestion"
        assert d["reference"] == "U1"
        assert d["net_schematic"] == "VCC"
        assert d["net_pcb"] == "VDD"
        assert d["pin"] == "1"


class TestSyncResult:
    """Tests for the SyncResult class."""

    def test_empty_results(self):
        """Test empty results."""
        result = SyncResult()
        assert result.error_count == 0
        assert result.warning_count == 0
        assert result.in_sync is True
        assert len(result) == 0
        assert bool(result) is False

    def test_error_count(self):
        """Test counting errors."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="e1",
                    suggestion="s1",
                ),
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="e2",
                    suggestion="s2",
                ),
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message="w1",
                    suggestion="s3",
                ),
            ]
        )
        assert result.error_count == 2

    def test_warning_count(self):
        """Test counting warnings."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="e1",
                    suggestion="s1",
                ),
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message="w1",
                    suggestion="s2",
                ),
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message="w2",
                    suggestion="s3",
                ),
            ]
        )
        assert result.warning_count == 2

    def test_in_sync_with_errors(self):
        """Test in_sync is False when errors exist."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="error",
                    suggestion="fix",
                ),
            ]
        )
        assert result.in_sync is False

    def test_in_sync_with_only_warnings(self):
        """Test in_sync is True when only warnings exist."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message="warning",
                    suggestion="check",
                ),
            ]
        )
        assert result.in_sync is True

    def test_iteration(self):
        """Test iterating over results."""
        i1 = SyncIssue(
            severity="error",
            category="missing_on_pcb",
            message="e1",
            suggestion="s1",
        )
        i2 = SyncIssue(
            severity="warning",
            category="orphaned_on_pcb",
            message="w1",
            suggestion="s2",
        )
        result = SyncResult(issues=[i1, i2])

        issues = list(result)
        assert issues == [i1, i2]

    def test_add_issue(self):
        """Test adding an issue."""
        result = SyncResult()
        issue = SyncIssue(
            severity="error",
            category="missing_on_pcb",
            message="test",
            suggestion="fix",
        )
        result.add(issue)

        assert len(result) == 1
        assert result.issues[0] == issue

    def test_missing_on_pcb_filter(self):
        """Test missing_on_pcb property filters correctly."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="m1",
                    suggestion="s1",
                ),
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message="o1",
                    suggestion="s2",
                ),
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="m2",
                    suggestion="s3",
                ),
            ]
        )
        missing = result.missing_on_pcb
        assert len(missing) == 2
        assert all(i.category == "missing_on_pcb" for i in missing)

    def test_orphaned_on_pcb_filter(self):
        """Test orphaned_on_pcb property filters correctly."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="m1",
                    suggestion="s1",
                ),
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message="o1",
                    suggestion="s2",
                ),
            ]
        )
        orphaned = result.orphaned_on_pcb
        assert len(orphaned) == 1
        assert orphaned[0].category == "orphaned_on_pcb"

    def test_net_mismatches_filter(self):
        """Test net_mismatches property filters correctly."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="net_mismatch",
                    message="n1",
                    suggestion="s1",
                ),
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message="o1",
                    suggestion="s2",
                ),
            ]
        )
        mismatches = result.net_mismatches
        assert len(mismatches) == 1
        assert mismatches[0].category == "net_mismatch"

    def test_pin_mismatches_filter(self):
        """Test pin_mismatches property filters correctly."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="pin_mismatch",
                    message="p1",
                    suggestion="s1",
                ),
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="m1",
                    suggestion="s2",
                ),
            ]
        )
        pin_issues = result.pin_mismatches
        assert len(pin_issues) == 1
        assert pin_issues[0].category == "pin_mismatch"

    def test_errors_property(self):
        """Test errors property returns only errors."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="e1",
                    suggestion="s1",
                ),
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message="w1",
                    suggestion="s2",
                ),
                SyncIssue(
                    severity="error",
                    category="net_mismatch",
                    message="e2",
                    suggestion="s3",
                ),
            ]
        )

        errors = result.errors
        assert len(errors) == 2
        assert all(e.is_error for e in errors)

    def test_warnings_property(self):
        """Test warnings property returns only warnings."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="e1",
                    suggestion="s1",
                ),
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message="w1",
                    suggestion="s2",
                ),
            ]
        )

        warnings = result.warnings
        assert len(warnings) == 1
        assert all(w.is_warning for w in warnings)

    def test_to_dict(self):
        """Test converting results to dictionary."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="error",
                    suggestion="fix",
                ),
            ]
        )
        d = result.to_dict()

        assert d["in_sync"] is False
        assert d["error_count"] == 1
        assert d["warning_count"] == 0
        assert len(d["issues"]) == 1

    def test_summary(self):
        """Test summary generation."""
        result = SyncResult(
            issues=[
                SyncIssue(
                    severity="error",
                    category="missing_on_pcb",
                    message="e1",
                    suggestion="s1",
                ),
                SyncIssue(
                    severity="warning",
                    category="orphaned_on_pcb",
                    message="w1",
                    suggestion="s2",
                ),
            ]
        )
        summary = result.summary()

        assert "OUT OF SYNC" in summary
        assert "1 errors" in summary
        assert "1 warnings" in summary
        assert "Missing on PCB: 1" in summary
        assert "Orphaned on PCB: 1" in summary

    def test_summary_in_sync(self):
        """Test summary generation when in sync."""
        result = SyncResult()
        summary = result.summary()

        assert "IN SYNC" in summary
        assert "0 errors" in summary


class TestModuleImports:
    """Test that public API imports work correctly."""

    def test_import_from_validate(self):
        """Test importing from kicad_tools.validate."""
        from kicad_tools.validate import NetlistValidator, SyncIssue, SyncResult

        assert NetlistValidator is not None
        assert SyncIssue is not None
        assert SyncResult is not None

    def test_import_netlist_directly(self):
        """Test importing netlist module directly."""
        from kicad_tools.validate.netlist import NetlistValidator, SyncIssue, SyncResult

        assert NetlistValidator is not None
        assert SyncIssue is not None
        assert SyncResult is not None


# Minimal schematic for testing
MINIMAL_SCHEMATIC = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
  )
  (symbol
    (lib_id "Device:C")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000003")
    (property "Reference" "C1" (at 120 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100n" (at 120 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402" (at 120 100 0) (effects (hide yes)))
  )
)
"""

# Minimal PCB with matching components
MINIMAL_PCB_MATCHING = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# PCB missing R1
MINIMAL_PCB_MISSING = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# PCB with orphaned component
MINIMAL_PCB_ORPHAN = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "LED_SMD:LED_0603"
    (layer "F.Cu")
    (uuid "fp-d1")
    (at 140 100)
    (property "Reference" "D1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "LED" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)
"""

# Schematic with global labels
SCHEMATIC_WITH_LABELS = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (global_label "VCC" (at 90 50 0) (uuid "gl1"))
  (global_label "GND" (at 90 60 0) (uuid "gl2"))
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 97 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 103 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402" (at 100 100 0) (effects (hide yes)))
  )
)
"""

# PCB with different net names
PCB_NET_MISMATCH = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "vcc")
  (net 2 "gnd")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "vcc"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "gnd"))
  )
)
"""


class TestNetlistValidator:
    """Tests for the NetlistValidator class."""

    def test_validator_instantiation(self, tmp_path):
        """Test creating a NetlistValidator with valid files."""
        sch_file = tmp_path / "test.kicad_sch"
        pcb_file = tmp_path / "test.kicad_pcb"

        sch_file.write_text(MINIMAL_SCHEMATIC)
        pcb_file.write_text(MINIMAL_PCB_MATCHING)

        validator = NetlistValidator(sch_file, pcb_file)

        assert validator.schematic is not None
        assert validator.pcb is not None

    def test_matching_components_in_sync(self, tmp_path):
        """Test that matching components are in sync."""
        sch_file = tmp_path / "test.kicad_sch"
        pcb_file = tmp_path / "test.kicad_pcb"

        sch_file.write_text(MINIMAL_SCHEMATIC)
        pcb_file.write_text(MINIMAL_PCB_MATCHING)

        validator = NetlistValidator(sch_file, pcb_file)
        result = validator.validate()

        # No missing or orphaned components
        assert len(result.missing_on_pcb) == 0
        assert len(result.orphaned_on_pcb) == 0
        assert result.in_sync is True

    def test_detect_missing_on_pcb(self, tmp_path):
        """Test detection of symbols missing from PCB."""
        sch_file = tmp_path / "test.kicad_sch"
        pcb_file = tmp_path / "test.kicad_pcb"

        sch_file.write_text(MINIMAL_SCHEMATIC)
        pcb_file.write_text(MINIMAL_PCB_MISSING)

        validator = NetlistValidator(sch_file, pcb_file)
        result = validator.validate()

        # R1 is missing from PCB
        assert len(result.missing_on_pcb) == 1
        assert result.missing_on_pcb[0].reference == "R1"
        assert result.missing_on_pcb[0].is_error is True
        assert "R1" in result.missing_on_pcb[0].message
        assert result.in_sync is False

    def test_detect_orphaned_on_pcb(self, tmp_path):
        """Test detection of orphaned footprints on PCB."""
        sch_file = tmp_path / "test.kicad_sch"
        pcb_file = tmp_path / "test.kicad_pcb"

        sch_file.write_text(MINIMAL_SCHEMATIC)
        pcb_file.write_text(MINIMAL_PCB_ORPHAN)

        validator = NetlistValidator(sch_file, pcb_file)
        result = validator.validate()

        # D1 is on PCB but not in schematic
        assert len(result.orphaned_on_pcb) == 1
        assert result.orphaned_on_pcb[0].reference == "D1"
        assert result.orphaned_on_pcb[0].is_warning is True
        assert "D1" in result.orphaned_on_pcb[0].message
        # Orphaned is warning, so still "in sync" (no errors)
        assert result.in_sync is True

    def test_detect_net_name_case_mismatch(self, tmp_path):
        """Test detection of net name case mismatches."""
        sch_file = tmp_path / "test.kicad_sch"
        pcb_file = tmp_path / "test.kicad_pcb"

        sch_file.write_text(SCHEMATIC_WITH_LABELS)
        pcb_file.write_text(PCB_NET_MISMATCH)

        validator = NetlistValidator(sch_file, pcb_file)
        result = validator.validate()

        # Should detect VCC vs vcc and GND vs gnd mismatches
        net_issues = result.net_mismatches
        assert len(net_issues) >= 1  # At least one case mismatch

    def test_suggestion_includes_footprint(self, tmp_path):
        """Test that missing component suggestions include footprint info."""
        sch_file = tmp_path / "test.kicad_sch"
        pcb_file = tmp_path / "test.kicad_pcb"

        sch_file.write_text(MINIMAL_SCHEMATIC)
        pcb_file.write_text(MINIMAL_PCB_MISSING)

        validator = NetlistValidator(sch_file, pcb_file)
        result = validator.validate()

        # The suggestion should mention the footprint
        missing = result.missing_on_pcb[0]
        assert "R1" in missing.suggestion

    def test_validator_repr(self, tmp_path):
        """Test validator string representation."""
        sch_file = tmp_path / "test.kicad_sch"
        pcb_file = tmp_path / "test.kicad_pcb"

        sch_file.write_text(MINIMAL_SCHEMATIC)
        pcb_file.write_text(MINIMAL_PCB_MATCHING)

        validator = NetlistValidator(sch_file, pcb_file)
        repr_str = repr(validator)

        assert "NetlistValidator" in repr_str
        assert "schematic_symbols=" in repr_str
        assert "pcb_footprints=" in repr_str


class TestProjectCheckSync:
    """Tests for Project.check_sync() method."""

    def test_project_check_sync_method_exists(self):
        """Test that Project class has check_sync method."""
        from kicad_tools.project import Project

        assert hasattr(Project, "check_sync")

    def test_project_check_sync_returns_sync_result(self, tmp_path):
        """Test that Project.check_sync() returns SyncResult."""
        from kicad_tools.project import Project

        # Create project files
        sch_file = tmp_path / "test.kicad_sch"
        pcb_file = tmp_path / "test.kicad_pcb"
        proj_file = tmp_path / "test.kicad_pro"

        sch_file.write_text(MINIMAL_SCHEMATIC)
        pcb_file.write_text(MINIMAL_PCB_MATCHING)
        proj_file.write_text("{}")  # Minimal project file

        project = Project.load(proj_file)
        result = project.check_sync()

        assert isinstance(result, SyncResult)
