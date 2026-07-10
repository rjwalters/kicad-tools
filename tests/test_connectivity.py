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


class TestPositionTolerance:
    """Tests for ConnectivityValidator position tolerance.

    Verifies that the widened POSITION_TOLERANCE (0.01 mm) correctly
    absorbs floating-point coordinate drift from trace optimisation.
    See issue #1434.
    """

    def test_tolerance_value(self):
        """POSITION_TOLERANCE must be at least 0.01 mm."""
        assert ConnectivityValidator.POSITION_TOLERANCE >= 0.01

    def test_endpoint_within_new_tolerance_detected_as_connected(self, tmp_path: Path):
        """A segment endpoint displaced 0.009 mm from pad centre
        (within new 0.01 mm tolerance, outside old 0.001 mm tolerance)
        should register as connected.
        """
        # Pad at (99.5, 100), segment endpoint displaced by 0.009 mm
        pcb_text = """(kicad_pcb
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
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (segment (start 99.509 100) (end 109.5 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-1"))
)
"""
        pcb_file = tmp_path / "tolerance_test.kicad_pcb"
        pcb_file.write_text(pcb_text)

        validator = ConnectivityValidator(pcb_file)
        result = validator.validate()

        # NET1 should be fully connected despite the 0.009mm displacement
        net1_errors = [i for i in result.errors if i.net_name == "NET1"]
        assert len(net1_errors) == 0, (
            f"NET1 reported as disconnected with 0.009mm displacement: {net1_errors}"
        )

    def test_endpoint_outside_tolerance_detected_as_disconnected(self, tmp_path: Path):
        """A segment endpoint displaced 0.02 mm (outside 0.01 mm tolerance)
        should be detected as disconnected.
        """
        pcb_text = """(kicad_pcb
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
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
  (segment (start 99.52 100) (end 109.5 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-1"))
)
"""
        pcb_file = tmp_path / "tolerance_test_outside.kicad_pcb"
        pcb_file.write_text(pcb_text)

        validator = ConnectivityValidator(pcb_file)
        result = validator.validate()

        # NET1 should be reported as disconnected -- the 0.02mm displacement
        # exceeds the 0.01mm tolerance.
        net1_errors = [i for i in result.errors if i.net_name == "NET1"]
        assert len(net1_errors) > 0, "NET1 should be disconnected with 0.02mm displacement"


class TestZoneConnectivity:
    """Tests for zone boundary polygon containment in connectivity validation.

    Verifies that pads inside zone boundary polygons on matching copper
    layers are detected as electrically connected through the zone pour.
    """

    # PCB with two pads on the same net connected only by a filled zone
    # (no traces).  The zone polygon and filled copper enclose both pads.
    # NOTE (Issue #3514): zone fixtures include filled_polygon data because
    # zones without filled copper provide no connectivity.
    ZONE_CONNECTED_SAME_LAYER_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "zone-gnd")
    (fill yes)
    (polygon
      (pts
        (xy 95 95)
        (xy 120 95)
        (xy 120 105)
        (xy 95 105)
      )
    )
    (filled_polygon
      (layer "F.Cu")
      (pts
        (xy 95 95)
        (xy 120 95)
        (xy 120 105)
        (xy 95 105)
      )
    )
  )
)
"""

    # PCB with two pads: one on F.Cu inside a zone on F.Cu, and one on B.Cu
    # outside the zone.  Without a via, they should NOT be connected.
    ZONE_DIFFERENT_LAYER_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "B.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "B.SilkS") (uuid "ref-r2"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "B.Cu") (net 1 "GND"))
  )
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "zone-gnd")
    (fill yes)
    (polygon
      (pts
        (xy 95 95)
        (xy 120 95)
        (xy 120 105)
        (xy 95 105)
      )
    )
    (filled_polygon
      (layer "F.Cu")
      (pts
        (xy 95 95)
        (xy 120 95)
        (xy 120 105)
        (xy 95 105)
      )
    )
  )
)
"""

    # PCB with pad outside zone boundary -- should not be zone-connected.
    ZONE_PAD_OUTSIDE_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 200 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "zone-gnd")
    (fill yes)
    (polygon
      (pts
        (xy 95 95)
        (xy 105 95)
        (xy 105 105)
        (xy 95 105)
      )
    )
    (filled_polygon
      (layer "F.Cu")
      (pts
        (xy 95 95)
        (xy 105 95)
        (xy 105 105)
        (xy 95 105)
      )
    )
  )
)
"""

    # PCB with through-hole pads (*.Cu) matching a zone on F.Cu
    ZONE_THRU_HOLE_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (footprint "Connector:Conn_01x02"
    (layer "F.Cu")
    (uuid "fp-j1")
    (at 100 100)
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-j1"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 1 "GND"))
  )
  (footprint "Connector:Conn_01x02"
    (layer "F.Cu")
    (uuid "fp-j2")
    (at 110 100)
    (property "Reference" "J2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-j2"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 1 "GND"))
  )
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "zone-gnd")
    (fill yes)
    (polygon
      (pts
        (xy 95 95)
        (xy 120 95)
        (xy 120 105)
        (xy 95 105)
      )
    )
    (filled_polygon
      (layer "F.Cu")
      (pts
        (xy 95 95)
        (xy 120 95)
        (xy 120 105)
        (xy 95 105)
      )
    )
  )
)
"""

    # PCB with zone but empty polygon data
    ZONE_EMPTY_POLYGON_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "zone-gnd")
    (fill yes)
  )
)
"""

    def test_pads_inside_same_net_zone_same_layer_connected(self, tmp_path: Path):
        """Pads geometrically inside a same-net zone on the same layer
        should be detected as connected (high confidence)."""
        pcb_file = tmp_path / "zone_same_layer.kicad_pcb"
        pcb_file.write_text(self.ZONE_CONNECTED_SAME_LAYER_PCB)

        validator = ConnectivityValidator(pcb_file)
        result = validator.validate()

        # Both pads are inside the GND zone on F.Cu -- should be connected
        gnd_errors = [i for i in result.errors if i.net_name == "GND"]
        assert len(gnd_errors) == 0, (
            f"GND should be connected via zone containment, got: {gnd_errors}"
        )
        assert result.zone_connected_nets >= 1

    def test_pads_inside_zone_different_layer_not_connected(self, tmp_path: Path):
        """A pad on B.Cu should NOT be connected to a zone on F.Cu
        without a via bridging the layers."""
        pcb_file = tmp_path / "zone_diff_layer.kicad_pcb"
        pcb_file.write_text(self.ZONE_DIFFERENT_LAYER_PCB)

        validator = ConnectivityValidator(pcb_file)
        result = validator.validate()

        # R2.2 is on B.Cu, zone is on F.Cu only -- should be disconnected
        gnd_errors = [i for i in result.errors if i.net_name == "GND"]
        assert len(gnd_errors) > 0, "GND should be disconnected (layer mismatch)"

    def test_pad_outside_zone_boundary_not_connected(self, tmp_path: Path):
        """A pad outside the zone boundary polygon should NOT be
        zone-connected even if on the same net and layer."""
        pcb_file = tmp_path / "zone_outside.kicad_pcb"
        pcb_file.write_text(self.ZONE_PAD_OUTSIDE_PCB)

        validator = ConnectivityValidator(pcb_file)
        result = validator.validate()

        # R2.2 is at (200.5, 100) which is far outside the zone at (95-105, 95-105)
        gnd_errors = [i for i in result.errors if i.net_name == "GND"]
        assert len(gnd_errors) > 0, "GND should be disconnected (pad outside zone)"

    def test_thru_hole_pads_match_zone_via_wildcard(self, tmp_path: Path):
        """Through-hole pads with *.Cu layers should match zones on any
        copper layer."""
        pcb_file = tmp_path / "zone_thru_hole.kicad_pcb"
        pcb_file.write_text(self.ZONE_THRU_HOLE_PCB)

        validator = ConnectivityValidator(pcb_file)
        result = validator.validate()

        # Both through-hole pads are inside the zone and *.Cu matches F.Cu
        gnd_errors = [i for i in result.errors if i.net_name == "GND"]
        assert len(gnd_errors) == 0, (
            f"GND should be connected (thru-hole *.Cu matches F.Cu zone): {gnd_errors}"
        )

    def test_zone_with_empty_polygon_no_crash(self, tmp_path: Path):
        """A zone with no polygon data should not cause a crash and
        should not claim connectivity."""
        pcb_file = tmp_path / "zone_empty.kicad_pcb"
        pcb_file.write_text(self.ZONE_EMPTY_POLYGON_PCB)

        validator = ConnectivityValidator(pcb_file)
        result = validator.validate()

        # With no polygon data, pads cannot be verified as zone-connected
        gnd_errors = [i for i in result.errors if i.net_name == "GND"]
        assert len(gnd_errors) > 0, "GND should be disconnected (no zone polygon)"

    def test_zone_connected_nets_count_in_result(self, tmp_path: Path):
        """ConnectivityResult.zone_connected_nets should count nets
        that were connected through zone containment geometry."""
        pcb_file = tmp_path / "zone_count.kicad_pcb"
        pcb_file.write_text(self.ZONE_CONNECTED_SAME_LAYER_PCB)

        validator = ConnectivityValidator(pcb_file)
        result = validator.validate()

        assert result.zone_connected_nets > 0
        assert "zone_connected_nets" in result.to_dict()
        assert result.to_dict()["zone_connected_nets"] > 0

    def test_point_in_polygon_basic(self):
        """Test the point_in_polygon static method directly."""
        # Square from (0,0) to (10,10)
        polygon = [(0, 0), (10, 0), (10, 10), (0, 10)]

        assert ConnectivityValidator._point_in_polygon((5, 5), polygon) is True
        assert ConnectivityValidator._point_in_polygon((15, 5), polygon) is False
        assert ConnectivityValidator._point_in_polygon((-1, 5), polygon) is False

    def test_pad_layer_matches_zone_exact(self):
        """Test exact layer matching."""
        assert ConnectivityValidator._pad_layer_matches_zone(["F.Cu"], "F.Cu") is True
        assert ConnectivityValidator._pad_layer_matches_zone(["B.Cu"], "F.Cu") is False

    def test_pad_layer_matches_zone_wildcard(self):
        """Test wildcard *.Cu matching."""
        assert ConnectivityValidator._pad_layer_matches_zone(["*.Cu"], "F.Cu") is True
        assert ConnectivityValidator._pad_layer_matches_zone(["*.Cu"], "B.Cu") is True
        assert ConnectivityValidator._pad_layer_matches_zone(["*.Cu"], "In1.Cu") is True


# ---- PCB fixtures for zero-fill zone connectivity tests (Issue #3514) ----

# Shared body: two GND pads inside the zone outline, no traces, no vias.
# The zone definition is parameterized so each case differs only in its
# fill / filled_polygon content.  Mirrors the Issue #3482 fixtures used for
# NetStatusAnalyzer in tests/test_analysis_net_status.py.
_ZONE_FILL_PCB_TEMPLATE = """(kicad_pcb
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
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "zone-gnd")
    {fill_clause}
    (polygon
      (pts
        (xy 95 95)
        (xy 120 95)
        (xy 120 105)
        (xy 95 105)
      )
    )
{filled_polygons}  )
)
"""

# Zone with fill ENABLED but zero filled polygons (e.g. fully shadowed by a
# higher-priority overlapping zone, or carved away entirely by clearances).
# Both pads sit inside the zone BOUNDARY but there is no copper: the net is
# physically open on the manufactured board.
ZERO_FILL_ZONE_PCB = _ZONE_FILL_PCB_TEMPLATE.format(
    fill_clause="(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))",
    filled_polygons="",
)

# Zone with fill DISABLED (boundary-only zone).
BOUNDARY_ONLY_ZONE_PCB = _ZONE_FILL_PCB_TEMPLATE.format(
    fill_clause="(fill (thermal_gap 0.2) (thermal_bridge_width 0.2))",
    filled_polygons="",
)

# Zone whose fill produced copper over only PART of the outline: the filled
# polygon covers x in [95, 105] while the boundary extends to x = 120.
# R1.2 (100.5, 100) lands inside filled copper; R2.2 (110.5, 100) is inside
# the boundary only (thermal-relief-cutout territory).  The Issue #479
# boundary heuristic applies because the zone genuinely has filled copper.
PARTIAL_FILL_ZONE_PCB = _ZONE_FILL_PCB_TEMPLATE.format(
    fill_clause="(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))",
    filled_polygons="""    (filled_polygon
      (layer "F.Cu")
      (pts
        (xy 95 95)
        (xy 105 95)
        (xy 105 105)
        (xy 95 105)
      )
    )
""",
)


class TestZeroFillZoneConnectivity:
    """Regression tests for Issue #3514 (ConnectivityValidator twin of #3482).

    A zone with zero filled polygons produces NO copper on the manufactured
    board, so its boundary polygon must not mark pads or vias as connected.
    Before the fix, the boundary-containment heuristic marked every pad
    inside a zero-fill zone outline as zone-connected, so
    ``kct validate --connectivity`` passed physically open pour nets
    (false-positive connectivity).
    """

    @pytest.fixture
    def zero_fill_pcb(self, tmp_path: Path) -> Path:
        pcb_file = tmp_path / "zero_fill.kicad_pcb"
        pcb_file.write_text(ZERO_FILL_ZONE_PCB)
        return pcb_file

    @pytest.fixture
    def boundary_only_pcb(self, tmp_path: Path) -> Path:
        pcb_file = tmp_path / "boundary_only.kicad_pcb"
        pcb_file.write_text(BOUNDARY_ONLY_ZONE_PCB)
        return pcb_file

    @pytest.fixture
    def partial_fill_pcb(self, tmp_path: Path) -> Path:
        pcb_file = tmp_path / "partial_fill.kicad_pcb"
        pcb_file.write_text(PARTIAL_FILL_ZONE_PCB)
        return pcb_file

    def test_zero_fill_zone_is_not_connectivity(self, zero_fill_pcb: Path):
        """Two pads bridged only by a zero-fill zone must report an error.

        Fill is enabled but the zone produced no filled polygons, so the
        net is an open circuit -- the validator must NOT report it as
        connected.
        """
        validator = ConnectivityValidator(zero_fill_pcb)
        result = validator.validate()

        gnd_errors = [i for i in result.errors if i.net_name == "GND"]
        assert len(gnd_errors) > 0, (
            "GND should be reported disconnected: zero-fill zone provides no copper"
        )
        # The zone must not count as zone-connectivity either.
        assert result.zone_connected_nets == 0

    def test_boundary_only_zone_is_not_connectivity(self, boundary_only_pcb: Path):
        """A boundary-only zone (fill disabled) must not provide connectivity."""
        validator = ConnectivityValidator(boundary_only_pcb)
        result = validator.validate()

        gnd_errors = [i for i in result.errors if i.net_name == "GND"]
        assert len(gnd_errors) > 0, (
            "GND should be reported disconnected: fill-disabled zone is not copper"
        )
        assert result.zone_connected_nets == 0

    def test_partial_fill_zone_provides_connectivity(self, partial_fill_pcb: Path):
        """A zone with at least one filled polygon retains the boundary
        heuristic: pads inside the outline (including thermal-relief
        cutouts) are zone-connected (Issue #479 behavior, no regression).
        """
        validator = ConnectivityValidator(partial_fill_pcb)
        result = validator.validate()

        gnd_errors = [i for i in result.errors if i.net_name == "GND"]
        assert len(gnd_errors) == 0, (
            f"Partially filled zone should connect pads inside its boundary, got: {gnd_errors}"
        )
        assert result.zone_connected_nets >= 1

    def test_fully_filled_zone_regression(self, tmp_path: Path):
        """Fully filled zone behavior must not regress: a zone with a
        filled polygon covering both pads connects them."""
        pcb_file = tmp_path / "full_fill.kicad_pcb"
        pcb_file.write_text(TestZoneConnectivity.ZONE_CONNECTED_SAME_LAYER_PCB)

        validator = ConnectivityValidator(pcb_file)
        result = validator.validate()

        gnd_errors = [i for i in result.errors if i.net_name == "GND"]
        assert len(gnd_errors) == 0, (
            f"Fully filled zone should connect both pads, got: {gnd_errors}"
        )
        assert result.zone_connected_nets >= 1


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


class TestConnectivityProResolution:
    """Tests for .kicad_pro -> .kicad_pcb resolution in validate --connectivity."""

    def test_kicad_pro_resolves_to_pcb(self, tmp_path: Path):
        """run_validate_connectivity_command resolves .kicad_pro to .kicad_pcb."""
        from types import SimpleNamespace

        from kicad_tools.cli.commands.validation import run_validate_connectivity_command

        # Create a .kicad_pro and corresponding .kicad_pcb
        pro_file = tmp_path / "board.kicad_pro"
        pro_file.write_text("{}")
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(FULLY_CONNECTED_PCB)

        args = SimpleNamespace(
            connectivity=True,
            validate_files=[str(pro_file)],
            validate_pcb=None,
            validate_format="table",
            validate_errors_only=False,
            validate_strict=False,
            validate_verbose=False,
        )
        exit_code = run_validate_connectivity_command(args)
        assert exit_code == 0

    def test_kicad_pro_missing_pcb_returns_error(self, tmp_path: Path, capsys):
        """run_validate_connectivity_command errors when .kicad_pcb is missing."""
        from types import SimpleNamespace

        from kicad_tools.cli.commands.validation import run_validate_connectivity_command

        pro_file = tmp_path / "board.kicad_pro"
        pro_file.write_text("{}")
        # Do NOT create .kicad_pcb

        args = SimpleNamespace(
            connectivity=True,
            validate_files=[str(pro_file)],
            validate_pcb=None,
            validate_format="table",
            validate_errors_only=False,
            validate_strict=False,
            validate_verbose=False,
        )
        exit_code = run_validate_connectivity_command(args)
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "PCB file not found" in captured.out


# ---------------------------------------------------------------------------
# Layer-aware segment chaining (issue #3783)
#
# A via-less F.Cu/B.Cu crossover — two different-net traces that merely cross
# at the same (x, y) on opposite copper layers with NO via — is a legal,
# DRC-clean layer crossover and must NOT be fused into one component.  A via
# (or a multi-layer / through-hole pad) at that point DOES bridge the layers,
# so a same-net trace that legitimately hops layers through a via must still
# flood as one component.
# ---------------------------------------------------------------------------


def _crossover_pcb(*, with_via: bool, same_net: bool) -> str:
    """Build a 2-layer PCB with NODE_B (F.Cu) crossing NODE_C (B.Cu).

    Geometry mirrors the board-02 false-positive (issue #3783): a vertical
    F.Cu trace from R1.1 and a horizontal B.Cu trace from R2.1 cross at
    (110, 110).  ``with_via`` drops a through-hole via at the crossing.
    ``same_net`` labels both traces/pads on the same net (only meaningful
    together with ``with_via`` to model a real layer hop).
    """
    net_b = '(net 1 "NODE_B")'
    pad2_net = '(net 1 "NODE_B")' if same_net else '(net 2 "NODE_C")'
    via_block = ""
    if with_via:
        via_block = (
            '  (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
            '(net 1) (uuid "00000000-0000-0000-0000-0000000000f9"))\n'
        )
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NODE_B")
  (net 2 "NODE_C")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000c1")
    (at 110 105)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c1-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) {net_b})
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000c2")
    (at 105 110)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "B.SilkS") (uuid "fp-c2-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "B.Fab") (uuid "fp-c2-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "B.Cu" "B.Paste" "B.Mask")
      (roundrect_rratio 0.25) {pad2_net})
  )
  (segment (start 110 105) (end 110 110) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000c3"))
  (segment (start 110 110) (end 110 115) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000c4"))
  (segment (start 105 110) (end 110 110) (width 0.25) (layer "B.Cu") (net {1 if same_net else 2})
    (uuid "00000000-0000-0000-0000-0000000000c5"))
  (segment (start 110 110) (end 115 110) (width 0.25) (layer "B.Cu") (net {1 if same_net else 2})
    (uuid "00000000-0000-0000-0000-0000000000c6"))
{via_block})
"""


def _four_layer_thruvia_pcb() -> str:
    """4-layer PCB: a through-hole via must bridge an inner-layer hop.

    R1.1 routes on F.Cu to a via at (110, 110); the trace continues on the
    inner layer In1.Cu to R2.1.  ``via.layers`` only names ["F.Cu","B.Cu"],
    so the chainer must expand the through-hole span to include In1.Cu —
    otherwise the same-net hop falsely splits (the board-05 regression in
    issue #3783).
    """
    return """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000d1")
    (at 110 105)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-d1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-d1-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "SIG"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000d2")
    (at 115 110)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-d2-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-d2-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "In1.Cu")
      (roundrect_rratio 0.25) (net 1 "SIG"))
  )
  (segment (start 110 105) (end 110 110) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000d3"))
  (segment (start 110 110) (end 115 110) (width 0.25) (layer "In1.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000d4"))
  (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000d5"))
)
"""


def _hdi_blind_via_under_foreign_pad_pcb() -> str:
    """4-layer HDI PCB: a blind via (B.Cu-In2.Cu) sits at the same XY as an
    F.Cu-only SMD pad on a *different* net.

    The blind via's bridged span is {In2.Cu, B.Cu}; the SMD pad lives only on
    F.Cu.  They share no copper layer, so an XY coincidence must NOT fuse them
    (HDI false-connect, issue #4022).  Each footprint has a second pad so the
    net has a real anchor and never trivially collapses.
    """
    return """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG_TOP")
  (net 2 "SIG_BOT")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000f1")
    (at 110 110)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-f1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-f1-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "SIG_TOP"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000f2")
    (at 120 110)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-f2-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-f2-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "SIG_TOP"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000f3")
    (at 130 110)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "B.SilkS") (uuid "fp-f3-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "B.Fab") (uuid "fp-f3-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "B.Cu" "B.Paste" "B.Mask")
      (roundrect_rratio 0.25) (net 2 "SIG_BOT"))
  )
  (segment (start 110 110) (end 120 110) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000f4"))
  (segment (start 110 110) (end 130 110) (width 0.25) (layer "B.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-0000000000f5"))
  (via (at 110 110) (size 0.4) (drill 0.2) (layers "In2.Cu" "B.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-0000000000f6"))
)
"""


def _through_via_under_pad_pcb() -> str:
    """4-layer PCB: a through via (F.Cu-B.Cu) sits at the same XY as an F.Cu
    SMD pad on a *different* net, with no trace tying them.

    The via's span includes F.Cu (through-hole), so it DOES share a copper
    layer with the F.Cu pad.  Their XY coincidence therefore SHOULD still
    fuse (proves the layer gate narrows correctly rather than breaking
    legitimate stacked pad/via fusion, issue #4022).
    """
    return """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG_A")
  (net 2 "SIG_B")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000g1")
    (at 110 110)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-g1-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-g1-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "SIG_A"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000g3")
    (at 130 110)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "B.SilkS") (uuid "fp-g3-ref"))
    (property "Value" "1k" (at 0 1.5 0) (layer "B.Fab") (uuid "fp-g3-val"))
    (pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "B.Cu" "B.Paste" "B.Mask")
      (roundrect_rratio 0.25) (net 2 "SIG_B"))
  )
  (segment (start 130 110) (end 110 110) (width 0.25) (layer "B.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-0000000000g5"))
  (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-0000000000g6"))
)
"""


class TestLayerAwareSegmentChaining:
    """Issue #3783: cross-layer copper only fuses where a via/pad bridges."""

    def test_via_less_crossover_is_two_components(self, tmp_path: Path) -> None:
        """Different-net F.Cu/B.Cu traces crossing with NO via -> 2 groups."""
        pcb_file = tmp_path / "crossover.kicad_pcb"
        pcb_file.write_text(_crossover_pcb(with_via=False, same_net=False))
        partition = ConnectivityValidator(pcb_file).extract_pad_partition()
        # R1.1 (NODE_B, F.Cu) and R2.1 (NODE_C, B.Cu) must NOT fuse.
        assert frozenset({"R1.1"}) in partition
        assert frozenset({"R2.1"}) in partition
        assert len(partition) == 2

    def test_via_bridged_crossover_is_one_component(self, tmp_path: Path) -> None:
        """Same-net F.Cu/B.Cu traces joined by a via -> 1 group."""
        pcb_file = tmp_path / "via_bridge.kicad_pcb"
        pcb_file.write_text(_crossover_pcb(with_via=True, same_net=True))
        partition = ConnectivityValidator(pcb_file).extract_pad_partition()
        # The via at the crossing bridges F.Cu<->B.Cu, so both pads fuse.
        assert frozenset({"R1.1", "R2.1"}) in partition
        assert len(partition) == 1

    def test_via_at_crossover_bridges_different_nets(self, tmp_path: Path) -> None:
        """A via present at a crossover DOES bridge layers (sanity check).

        With a via at the crossing the two traces become galvanically one
        component even when labeled as different nets — this is what makes
        the label-free extractor catch a *real* via-on-crossover short, and
        confirms the layer-aware gate is not blanket-suppressing vias.
        """
        pcb_file = tmp_path / "via_short.kicad_pcb"
        pcb_file.write_text(_crossover_pcb(with_via=True, same_net=False))
        partition = ConnectivityValidator(pcb_file).extract_pad_partition()
        assert frozenset({"R1.1", "R2.1"}) in partition
        assert len(partition) == 1

    def test_through_via_bridges_inner_layer_hop(self, tmp_path: Path) -> None:
        """A through-hole via bridges inner layers not named in via.layers."""
        pcb_file = tmp_path / "thruvia.kicad_pcb"
        pcb_file.write_text(_four_layer_thruvia_pcb())
        partition = ConnectivityValidator(pcb_file).extract_pad_partition()
        # F.Cu -> via -> In1.Cu hop must keep the net as one component even
        # though via.layers only lists F.Cu/B.Cu.
        assert frozenset({"R1.1", "R2.1"}) in partition
        assert len(partition) == 1

    def test_copper_layer_order_expands_thru_via(self, tmp_path: Path) -> None:
        """The via-span expander includes inner copper layers."""
        pcb_file = tmp_path / "thruvia.kicad_pcb"
        pcb_file.write_text(_four_layer_thruvia_pcb())
        validator = ConnectivityValidator(pcb_file)
        order = validator._copper_layer_order()
        assert order == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        bridged = validator._via_bridged_layers(["F.Cu", "B.Cu"])
        assert bridged == frozenset({"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"})

    def test_blind_via_under_foreign_pad_does_not_fuse(self, tmp_path: Path) -> None:
        """Issue #4022: step 2e must be layer-aware.

        A blind via spanning In2.Cu-B.Cu that lands at the same XY as an
        F.Cu-only SMD pad on a different net shares NO copper layer with the
        pad — the XY coincidence alone must NOT fuse them.
        """
        pcb_file = tmp_path / "hdi_blind_via.kicad_pcb"
        pcb_file.write_text(_hdi_blind_via_under_foreign_pad_pcb())
        partition = ConnectivityValidator(pcb_file).extract_pad_partition()
        # SIG_TOP (F.Cu, R1/R2) and SIG_BOT (B.Cu, R3) must stay separate: the
        # blind via under R1.1 does not reach F.Cu, so it cannot bridge them.
        assert frozenset({"R1.1", "R2.1"}) in partition
        assert frozenset({"R3.1"}) in partition
        assert len(partition) == 2

    def test_through_via_under_pad_still_fuses(self, tmp_path: Path) -> None:
        """Issue #4022: the layer gate must not break legitimate fusion.

        A through via (F.Cu-B.Cu) coincident with an F.Cu pad DOES share the
        F.Cu copper layer, so the XY coincidence must STILL fuse — proving the
        gate narrows correctly rather than suppressing valid stacked ties.
        """
        pcb_file = tmp_path / "through_via_under_pad.kicad_pcb"
        pcb_file.write_text(_through_via_under_pad_pcb())
        partition = ConnectivityValidator(pcb_file).extract_pad_partition()
        # The through via (net 2, reached from R3.1 on B.Cu) is F.Cu-B.Cu, so
        # it fuses to R1.1's F.Cu pad at the shared XY -> one component.
        assert frozenset({"R1.1", "R3.1"}) in partition
        assert len(partition) == 1


# ---------------------------------------------------------------------------
# Via-into-pour bonding in extract_pad_partition (issue #3794)
# ---------------------------------------------------------------------------


def _shapely_present() -> bool:
    try:
        import shapely  # noqa: F401

        return True
    except ImportError:
        return False


_VIA_INTO_POUR_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000e1")
    (at 5 5)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-e1-ref"))
    (property "Value" "100n" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-e1-val"))
    (pad "2" smd roundrect (at 0 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000e2")
    (at 15 15)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "B.SilkS") (uuid "fp-e2-ref"))
    (property "Value" "100n" (at 0 1.5 0) (layer "B.Fab") (uuid "fp-e2-val"))
    (pad "2" smd roundrect (at 0 0) (size 1.5 1.5) (layers "B.Cu" "B.Paste" "B.Mask")
      (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000e3")
    (at 30 30)
    (property "Reference" "C3" (at 0 -1.5 0) (layer "B.SilkS") (uuid "fp-e3-ref"))
    (property "Value" "100n" (at 0 1.5 0) (layer "B.Fab") (uuid "fp-e3-val"))
    (pad "2" smd roundrect (at 0 0) (size 1.5 1.5) (layers "B.Cu" "B.Paste" "B.Mask")
      (roundrect_rratio 0.25) (net 2 "SIG"))
  )
  (segment (start 5 5) (end 10 10) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000s2"))
  (via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000v3") (net 1))
  (zone
    (net 1 "GND")
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-0000000000z5")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3))
    (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))
  )
)
"""


class TestExtractPartitionViaIntoPour:
    """extract_pad_partition bonds a via/trace endpoint landing in a pour (#3794).

    The label-free partition previously only tested pad boxes against a pour
    and only fused a via's *coincident* pads, so a pad reaching a pour through
    ``pad -> trace -> via -> opposite-layer pour`` stranded as a false same-net
    open.  These tests pin the new via-into-pour bond AND its soundness guard:
    the bond unions only copper that physically lands in the pour, so it cannot
    fabricate a short across a foreign net sitting outside the pour.
    """

    @pytest.mark.skipif(not _shapely_present(), reason="requires shapely")
    def test_pad_reaching_pour_via_stitch_via_is_bonded(self, tmp_path: Path) -> None:
        pcb_file = tmp_path / "via_pour.kicad_pcb"
        pcb_file.write_text(_VIA_INTO_POUR_PCB)
        partition = ConnectivityValidator(pcb_file).extract_pad_partition()
        comp = next(c for c in partition if "C1.2" in c)
        # C1.2 reaches the B.Cu pour only through its F.Cu trace + stitch via.
        assert "C2.2" in comp

    @pytest.mark.skipif(not _shapely_present(), reason="requires shapely")
    def test_via_into_pour_does_not_short_a_foreign_net(self, tmp_path: Path) -> None:
        """The bond must NOT pull a foreign-net pad outside the pour into it.

        C3.2 (SIG) sits at (30, 30) — outside the 0..20 GND pour and touched by
        no GND copper.  The via-into-pour bond unions only the GND via's island,
        so C3.2 must stay isolated: no GND<->SIG short is manufactured.
        """
        pcb_file = tmp_path / "via_pour.kicad_pcb"
        pcb_file.write_text(_VIA_INTO_POUR_PCB)
        partition = ConnectivityValidator(pcb_file).extract_pad_partition()
        assert frozenset({"C3.2"}) in partition
        gnd_comp = next(c for c in partition if "C1.2" in c)
        assert "C3.2" not in gnd_comp
        # No synthetic via node leaks into the returned partition.
        assert all(not p.startswith("__via") for c in partition for p in c)


# ---------------------------------------------------------------------------
# Segment-endpoint pad layer gating (PR #4003)
# ---------------------------------------------------------------------------

# Two single-pad footprints with F.Cu-only SMD pads on the SAME net, joined
# by one segment whose layer is templated: on F.Cu it is a real connection,
# on B.Cu it merely passes *under* both pads with no copper contact.
_SMD_SAME_NET_SEGMENT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG"))
  )
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "{segment_layer}") (net 1) (uuid "seg-1"))
)
"""

# Two through-hole (``*.Cu`` wildcard) pads on different declared nets,
# joined by a B.Cu segment: the barrel has copper on every layer, so this
# IS a physical connection and the label-free partition must fuse them.
_THRU_HOLE_PADS_SEGMENT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NETA")
  (net 2 "NETB")
  (footprint "Connector_PinHeader:PinHeader_1x01"
    (layer "F.Cu")
    (uuid "fp-j1")
    (at 100 100)
    (property "Reference" "J1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-j1"))
    (pad "1" thru_hole circle (at 0 0) (size 1.0 1.0) (drill 0.5) (layers "*.Cu" "*.Mask") (net 1 "NETA"))
  )
  (footprint "Connector_PinHeader:PinHeader_1x01"
    (layer "F.Cu")
    (uuid "fp-j2")
    (at 110 100)
    (property "Reference" "J2" (at 0 0 0) (layer "F.SilkS") (uuid "ref-j2"))
    (pad "1" thru_hole circle (at 0 0) (size 1.0 1.0) (drill 0.5) (layers "*.Cu" "*.Mask") (net 2 "NETB"))
  )
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-1"))
)
"""

# An F.Cu-only pad with a via at the pad centre bridging to a B.Cu escape
# trace that ends on a B.Cu-only pad: the via barrel makes this a real
# cross-layer connection, so via probes must not be narrowed to the
# segment's layer.
_VIA_AT_PAD_BRIDGE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NETA")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NETA"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "B.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "B.SilkS") (uuid "ref-r2"))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "B.Cu" "B.Paste" "B.Mask") (net 1 "NETA"))
  )
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "B.Cu") (net 1) (uuid "seg-1"))
)
"""


class TestSegmentEndpointPadLayerGating:
    """Segment-endpoint pad hits are layer-gated (PR #4003 + e9d0625d).

    A track-segment endpoint only lands ON a pad when the pad has copper
    on the segment's layer; a B.Cu trace ending at the XY of an F.Cu-only
    SMD pad passes *under* the pad with no copper contact.  The
    label-free ``extract_pad_partition`` side of this contract (false
    copper-LVS shorts) is pinned by
    ``test_copper_lvs.py::test_trace_under_smd_pad_on_other_layer_does_not_fuse``;
    this class pins the remaining surfaces:

    * the per-net ``validate()`` path (``_build_net_copper_graph``): the
      same under-pad geometry must not count a same-net pad as *reached*,
      or a genuinely open net would be reported fully routed;
    * through-hole (``*.Cu``) pads still match a segment on any copper
      layer;
    * via probes are gated by the via's bridged layer span, not the
      segment's layer, because a via barrel bridges layers.
    """

    def _partition(self, tmp_path: Path, name: str, content: str) -> list[frozenset[str]]:
        pcb_file = tmp_path / name
        pcb_file.write_text(content)
        return ConnectivityValidator(pcb_file).extract_pad_partition()

    def _group_of(self, partition: list[frozenset[str]], pad_id: str) -> frozenset[str]:
        return next(c for c in partition if pad_id in c)

    def test_validate_bcu_trace_under_fcu_pads_is_not_routed(self, tmp_path: Path):
        """A same-net B.Cu trace ending under two F.Cu-only pads is an open.

        Without the layer gate in ``_build_net_copper_graph`` the
        validator matched the endpoints to the pads by XY alone and
        reported SIG fully routed even though no copper connects the two
        pads (no via anywhere on the board).
        """
        pcb_file = tmp_path / "bcu_under_fcu.kicad_pcb"
        pcb_file.write_text(_SMD_SAME_NET_SEGMENT_PCB.format(segment_layer="B.Cu"))
        result = ConnectivityValidator(pcb_file).validate()

        assert not result.is_fully_routed
        sig_issues = [i for i in result.issues if i.net_name == "SIG"]
        assert len(sig_issues) >= 1

    def test_validate_fcu_trace_same_geometry_is_routed(self, tmp_path: Path):
        """Control: the identical segment on F.Cu IS a real connection.

        Guards the open-net test against passing vacuously (e.g. because
        of a position/tolerance mistake in the fixture): the only
        difference between the two runs is the segment's layer.
        """
        pcb_file = tmp_path / "fcu_on_fcu.kicad_pcb"
        pcb_file.write_text(_SMD_SAME_NET_SEGMENT_PCB.format(segment_layer="F.Cu"))
        result = ConnectivityValidator(pcb_file).validate()

        assert result.is_fully_routed
        assert result.error_count == 0

    def test_thru_hole_wildcard_pads_match_any_layer_segment(self, tmp_path: Path):
        """``*.Cu`` through-hole pads still match a B.Cu segment endpoint."""
        partition = self._partition(tmp_path, "thru_hole.kicad_pcb", _THRU_HOLE_PADS_SEGMENT_PCB)
        assert self._group_of(partition, "J1.1") == self._group_of(partition, "J2.1")

    def test_via_probe_bridges_fcu_pad_to_bcu_trace(self, tmp_path: Path):
        """A via at an F.Cu pad still bridges that pad to B.Cu copper.

        The segment-layer gate applies to *segment endpoint* probes only:
        the via barrel is copper on every layer it spans, so the
        F.Cu-only pad, the via, the B.Cu trace, and the B.Cu pad form one
        island in the label-free partition.
        """
        partition = self._partition(tmp_path, "via_bridge.kicad_pcb", _VIA_AT_PAD_BRIDGE_PCB)
        assert self._group_of(partition, "R1.1") == self._group_of(partition, "R2.1")
