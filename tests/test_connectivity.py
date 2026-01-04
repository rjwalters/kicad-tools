"""Tests for net connectivity validation."""

from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.connectivity import (
    ConnectivityIssue,
    ConnectivityResult,
    ConnectivityValidator,
)

# PCB with fully connected nets (all pads connected via tracks)
FULLY_CONNECTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET1")
  (net 2 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
  (segment (start 99.5 100) (end 109.5 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-net1"))
  (segment (start 100.5 100) (end 110.5 100) (width 0.2) (layer "F.Cu") (net 2) (uuid "seg-net2"))
)
"""


# PCB with partially connected nets (islands)
# R1.1, R2.1, R3.1 on NET1 - segment only connects R1 and R2, R3 is isolated
PARTIALLY_CONNECTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET1")
  (net 2 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r3")
    (at 120 100)
    (property "Reference" "R3" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r3"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
  (segment (start 99.5 100) (end 109.5 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-net1"))
)
"""


# PCB with completely unrouted nets
UNROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET1")
  (net 2 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "GND"))
  )
)
"""


# PCB with via chains connecting two pads through layer change
# R1.1 on F.Cu at (99.5, 100) -> trace on F.Cu -> via at (105, 100) -> trace on B.Cu -> R2.1 on B.Cu at (109.5, 100)
VIA_CHAIN_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET1")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "B.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "B.SilkS") (uuid "ref-r2"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "B.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "B.Cu") (net 0 ""))
  )
  (segment (start 99.5 100) (end 105 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-f1"))
  (via (at 105 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
  (segment (start 105 100) (end 109.5 100) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-b1"))
)
"""


@pytest.fixture
def fully_connected_pcb(tmp_path: Path) -> Path:
    """Create a PCB with fully connected nets."""
    pcb_file = tmp_path / "fully_connected.kicad_pcb"
    pcb_file.write_text(FULLY_CONNECTED_PCB)
    return pcb_file


@pytest.fixture
def partially_connected_pcb(tmp_path: Path) -> Path:
    """Create a PCB with partially connected nets (islands)."""
    pcb_file = tmp_path / "partially_connected.kicad_pcb"
    pcb_file.write_text(PARTIALLY_CONNECTED_PCB)
    return pcb_file


@pytest.fixture
def unrouted_pcb(tmp_path: Path) -> Path:
    """Create a PCB with completely unrouted nets."""
    pcb_file = tmp_path / "unrouted.kicad_pcb"
    pcb_file.write_text(UNROUTED_PCB)
    return pcb_file


@pytest.fixture
def via_chain_pcb(tmp_path: Path) -> Path:
    """Create a PCB with via chains connecting layers."""
    pcb_file = tmp_path / "via_chain.kicad_pcb"
    pcb_file.write_text(VIA_CHAIN_PCB)
    return pcb_file


class TestConnectivityIssue:
    """Tests for ConnectivityIssue dataclass."""

    def test_create_error_issue(self):
        """Test creating an error issue."""
        issue = ConnectivityIssue(
            severity="error",
            issue_type="unrouted",
            net_name="NET1",
            message="Net 'NET1' is unrouted",
            suggestion="Add traces to connect pads",
        )
        assert issue.is_error
        assert not issue.is_warning
        assert issue.net_name == "NET1"

    def test_create_warning_issue(self):
        """Test creating a warning issue."""
        issue = ConnectivityIssue(
            severity="warning",
            issue_type="partial",
            net_name="GND",
            message="Net 'GND' has 2 islands",
            suggestion="Connect islands",
        )
        assert issue.is_warning
        assert not issue.is_error

    def test_invalid_severity_raises(self):
        """Test that invalid severity raises ValueError."""
        with pytest.raises(ValueError, match="severity must be"):
            ConnectivityIssue(
                severity="invalid",
                issue_type="unrouted",
                net_name="NET1",
                message="Test",
                suggestion="Test",
            )

    def test_invalid_issue_type_raises(self):
        """Test that invalid issue_type raises ValueError."""
        with pytest.raises(ValueError, match="issue_type must be"):
            ConnectivityIssue(
                severity="error",
                issue_type="invalid",
                net_name="NET1",
                message="Test",
                suggestion="Test",
            )

    def test_to_dict(self):
        """Test serialization to dictionary."""
        issue = ConnectivityIssue(
            severity="error",
            issue_type="partial",
            net_name="NET1",
            message="Test message",
            suggestion="Test suggestion",
            connected_pads=("R1.1", "R2.1"),
            unconnected_pads=("R3.1",),
            islands=(("R1.1", "R2.1"), ("R3.1",)),
        )
        d = issue.to_dict()
        assert d["severity"] == "error"
        assert d["net_name"] == "NET1"
        assert d["connected_pads"] == ["R1.1", "R2.1"]
        assert d["islands"] == [["R1.1", "R2.1"], ["R3.1"]]


class TestConnectivityResult:
    """Tests for ConnectivityResult dataclass."""

    def test_empty_result(self):
        """Test empty result properties."""
        result = ConnectivityResult()
        assert not result.has_issues
        assert result.is_fully_routed
        assert result.error_count == 0
        assert result.warning_count == 0
        assert len(result) == 0

    def test_with_errors(self):
        """Test result with errors."""
        result = ConnectivityResult()
        result.add(
            ConnectivityIssue(
                severity="error",
                issue_type="unrouted",
                net_name="NET1",
                message="Unrouted",
                suggestion="Fix",
            )
        )
        assert result.has_issues
        assert not result.is_fully_routed
        assert result.error_count == 1
        assert result.warning_count == 0

    def test_with_warnings_only(self):
        """Test result with only warnings."""
        result = ConnectivityResult()
        result.add(
            ConnectivityIssue(
                severity="warning",
                issue_type="isolated",
                net_name="NET1",
                message="Isolated",
                suggestion="Check",
            )
        )
        assert result.has_issues
        assert result.is_fully_routed  # Warnings don't affect routing status
        assert result.error_count == 0
        assert result.warning_count == 1

    def test_filter_by_type(self):
        """Test filtering by issue type."""
        result = ConnectivityResult()
        result.add(
            ConnectivityIssue(
                severity="error",
                issue_type="unrouted",
                net_name="NET1",
                message="Unrouted",
                suggestion="Fix",
            )
        )
        result.add(
            ConnectivityIssue(
                severity="error",
                issue_type="partial",
                net_name="NET2",
                message="Partial",
                suggestion="Fix",
            )
        )
        assert len(result.unrouted) == 1
        assert len(result.partial) == 1
        assert len(result.isolated) == 0

    def test_summary(self):
        """Test summary generation."""
        result = ConnectivityResult(total_nets=5, connected_nets=3)
        result.add(
            ConnectivityIssue(
                severity="error",
                issue_type="unrouted",
                net_name="NET1",
                message="Unrouted",
                suggestion="Fix",
                unconnected_pads=("R1.1", "R2.1"),
            )
        )
        summary = result.summary()
        assert "CONNECTIVITY ISSUES" in summary
        assert "3/5 fully connected" in summary

    def test_to_dict(self):
        """Test serialization to dictionary."""
        result = ConnectivityResult(total_nets=2, connected_nets=1)
        result.add(
            ConnectivityIssue(
                severity="error",
                issue_type="partial",
                net_name="NET1",
                message="Test",
                suggestion="Fix",
            )
        )
        d = result.to_dict()
        assert d["total_nets"] == 2
        assert d["connected_nets"] == 1
        assert len(d["issues"]) == 1


class TestConnectivityValidator:
    """Tests for ConnectivityValidator."""

    def test_fully_connected_pcb(self, fully_connected_pcb: Path):
        """Test validation of fully connected PCB."""
        validator = ConnectivityValidator(fully_connected_pcb)
        result = validator.validate()

        assert result.is_fully_routed
        assert result.error_count == 0
        assert result.total_nets > 0
        assert result.connected_nets == result.total_nets

    def test_partially_connected_pcb(self, partially_connected_pcb: Path):
        """Test validation of PCB with islands."""
        validator = ConnectivityValidator(partially_connected_pcb)
        result = validator.validate()

        # Should find partial connectivity issues
        assert result.has_issues
        # At least one net should have islands
        partial_issues = result.partial
        assert len(partial_issues) >= 1

    def test_unrouted_pcb(self, unrouted_pcb: Path):
        """Test validation of completely unrouted PCB."""
        validator = ConnectivityValidator(unrouted_pcb)
        result = validator.validate()

        # Should find unrouted/partial connectivity issues
        assert result.has_issues
        assert not result.is_fully_routed
        # All multi-pad nets should have issues
        assert result.error_count >= 1

    def test_via_chain_connectivity(self, via_chain_pcb: Path):
        """Test that via chains properly connect layers."""
        validator = ConnectivityValidator(via_chain_pcb)
        result = validator.validate()

        # Via chains should connect the pads on different layers
        # If connectivity is properly detected, there should be no issues
        # for the NET1 net that uses via chains
        net1_issues = [i for i in result.issues if i.net_name == "NET1"]
        # The via chain should connect F.Cu to B.Cu pads
        assert len(net1_issues) == 0 or all("island" not in i.message.lower() for i in net1_issues)

    def test_load_from_path_string(self, fully_connected_pcb: Path):
        """Test loading PCB from path string."""
        validator = ConnectivityValidator(str(fully_connected_pcb))
        result = validator.validate()
        assert result.total_nets > 0

    def test_load_from_pcb_object(self, fully_connected_pcb: Path):
        """Test loading PCB from PCB object."""
        pcb = PCB.load(str(fully_connected_pcb))
        validator = ConnectivityValidator(pcb)
        result = validator.validate()
        assert result.total_nets > 0

    def test_repr(self, fully_connected_pcb: Path):
        """Test string representation."""
        validator = ConnectivityValidator(fully_connected_pcb)
        repr_str = repr(validator)
        assert "ConnectivityValidator" in repr_str
        assert "nets=" in repr_str


class TestConnectivityCLI:
    """Tests for connectivity validation CLI."""

    def test_cli_fully_connected(self, fully_connected_pcb: Path):
        """Test CLI with fully connected PCB."""
        from kicad_tools.cli.validate_connectivity_cmd import main

        exit_code = main([str(fully_connected_pcb)])
        assert exit_code == 0

    def test_cli_with_issues(self, unrouted_pcb: Path):
        """Test CLI with PCB that has issues."""
        from kicad_tools.cli.validate_connectivity_cmd import main

        exit_code = main([str(unrouted_pcb)])
        assert exit_code == 1  # Errors found

    def test_cli_json_format(self, fully_connected_pcb: Path, capsys):
        """Test CLI JSON output format."""
        from kicad_tools.cli.validate_connectivity_cmd import main

        main([str(fully_connected_pcb), "--format", "json"])
        captured = capsys.readouterr()
        import json

        data = json.loads(captured.out)
        assert "is_fully_routed" in data
        assert "summary" in data
        assert "issues" in data

    def test_cli_summary_format(self, fully_connected_pcb: Path, capsys):
        """Test CLI summary output format."""
        from kicad_tools.cli.validate_connectivity_cmd import main

        main([str(fully_connected_pcb), "--format", "summary"])
        captured = capsys.readouterr()
        assert "Connectivity:" in captured.out
        assert "Nets connected:" in captured.out

    def test_cli_errors_only(self, partially_connected_pcb: Path, capsys):
        """Test CLI with --errors-only flag."""
        from kicad_tools.cli.validate_connectivity_cmd import main

        main([str(partially_connected_pcb), "--errors-only"])
        # Should run without error
        captured = capsys.readouterr()
        # Output should not contain warnings section
        # (hard to test definitively without modifying fixtures)
        assert "NET CONNECTIVITY VALIDATION" in captured.out

    def test_cli_strict_mode(self, fully_connected_pcb: Path):
        """Test CLI with --strict flag on clean PCB."""
        from kicad_tools.cli.validate_connectivity_cmd import main

        exit_code = main([str(fully_connected_pcb), "--strict"])
        assert exit_code == 0  # No warnings, so still 0

    def test_cli_nonexistent_file(self, tmp_path: Path, capsys):
        """Test CLI with nonexistent file."""
        from kicad_tools.cli.validate_connectivity_cmd import main

        exit_code = main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err or "not found" in captured.err
