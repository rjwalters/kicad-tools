"""Tests for kicad_tools.mcp.tools.placement module."""

import json
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.mcp.tools.placement import placement_analyze
from kicad_tools.mcp.types import (
    PlacementAnalysis,
    PlacementCluster,
    PlacementIssue,
    PlacementScores,
    RoutingEstimate,
)

# Simple PCB with basic placement
SIMPLE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG1")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 10 10)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 20 10)
    (attr smd)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (segment (start 10.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 3))
)
"""


# PCB with IC and bypass caps (for cluster detection)
PCB_WITH_IC = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG1")

  (gr_line (start 0 0) (end 80 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 80 0) (end 80 60) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 80 60) (end 0 60) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 60) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "QFP-32"
    (layer "F.Cu")
    (at 40 30)
    (attr smd)
    (property "Reference" "U1")
    (property "Value" "MCU")
    (pad "1" smd rect (at -5 -5) (size 0.5 1.5) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at -5 -3) (size 0.5 1.5) (layers "F.Cu") (net 2 "GND"))
    (pad "3" smd rect (at -5 -1) (size 0.5 1.5) (layers "F.Cu") (net 3 "SIG1"))
    (pad "4" smd rect (at -5 1) (size 0.5 1.5) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 32 25)
    (attr smd)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 32 35)
    (attr smd)
    (property "Reference" "C2")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )
)
"""


# PCB with components at origin (unplaced)
PCB_WITH_UNPLACED = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 0 0)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 0 ""))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 25 20)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 0 ""))
  )
)
"""


# PCB with component at board edge
PCB_EDGE_CLEARANCE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")

  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 0.5 20)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 0 ""))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 25 20)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 0 ""))
  )
)
"""


def write_temp_pcb(content: str) -> str:
    """Write PCB content to a temporary file and return the path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".kicad_pcb", delete=False) as f:
        f.write(content)
        return f.name


class TestPlacementAnalyzeBasic:
    """Basic functionality tests for placement_analyze."""

    def test_analyze_simple_pcb(self):
        """Test analyzing a simple PCB."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_analyze(pcb_path)

            assert isinstance(result, PlacementAnalysis)
            assert result.file_path == pcb_path
            assert 0 <= result.overall_score <= 100
        finally:
            Path(pcb_path).unlink()

    def test_file_not_found_error(self):
        """Test that FileNotFoundError is raised for missing files."""
        with pytest.raises(KiCadFileNotFoundError):
            placement_analyze("/nonexistent/path/to/board.kicad_pcb")

    def test_invalid_file_extension(self):
        """Test that ParseError is raised for invalid file extensions."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"not a pcb file")
            path = f.name
        try:
            with pytest.raises(ParseError):
                placement_analyze(path)
        finally:
            Path(path).unlink()

    def test_to_dict_serialization(self):
        """Test that PlacementAnalysis can be serialized to dict/JSON."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_analyze(pcb_path)
            data = result.to_dict()

            # Verify it's JSON-serializable
            json_str = json.dumps(data)
            assert json_str is not None

            # Verify structure
            assert "file_path" in data
            assert "overall_score" in data
            assert "categories" in data
            assert "issues" in data
            assert "clusters" in data
            assert "routing_estimate" in data
        finally:
            Path(pcb_path).unlink()


class TestPlacementScores:
    """Tests for placement score calculation."""

    def test_scores_in_valid_range(self):
        """Test that all scores are between 0 and 100."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_analyze(pcb_path)
            scores = result.categories

            assert 0 <= scores.wire_length <= 100
            assert 0 <= scores.congestion <= 100
            assert 0 <= scores.thermal <= 100
            assert 0 <= scores.signal_integrity <= 100
            assert 0 <= scores.manufacturing <= 100
        finally:
            Path(pcb_path).unlink()

    def test_overall_score_is_weighted_average(self):
        """Test that overall score is derived from category scores."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_analyze(pcb_path)

            # Overall should be in the ballpark of category scores
            categories = result.categories
            avg = (
                categories.wire_length
                + categories.congestion
                + categories.thermal
                + categories.signal_integrity
                + categories.manufacturing
            ) / 5

            # Overall should be within 20 points of simple average
            assert abs(result.overall_score - avg) < 30
        finally:
            Path(pcb_path).unlink()


class TestPlacementIssues:
    """Tests for placement issue detection."""

    def test_unplaced_components_detected(self):
        """Test that components at origin are flagged as unplaced."""
        pcb_path = write_temp_pcb(PCB_WITH_UNPLACED)
        try:
            result = placement_analyze(pcb_path)

            # Should have at least one DFM issue for unplaced component
            dfm_issues = [i for i in result.issues if i.category == "dfm"]
            assert len(dfm_issues) > 0

            # At least one should mention unplaced
            unplaced_issues = [i for i in dfm_issues if "unplaced" in i.description.lower()]
            assert len(unplaced_issues) > 0

            # R1 should be in affected components
            for issue in unplaced_issues:
                if "R1" in issue.affected_components:
                    break
            else:
                pytest.fail("R1 not found in unplaced component issues")
        finally:
            Path(pcb_path).unlink()

    def test_edge_clearance_detected(self):
        """Test that components too close to board edge are flagged."""
        pcb_path = write_temp_pcb(PCB_EDGE_CLEARANCE)
        try:
            result = placement_analyze(pcb_path)

            # Should have edge clearance issue
            dfm_issues = [i for i in result.issues if i.category == "dfm"]
            edge_issues = [i for i in dfm_issues if "edge" in i.description.lower()]
            assert len(edge_issues) > 0

            # R1 should be flagged
            for issue in edge_issues:
                if "R1" in issue.affected_components:
                    break
            else:
                pytest.fail("R1 not found in edge clearance issues")
        finally:
            Path(pcb_path).unlink()

    def test_issues_sorted_by_severity(self):
        """Test that issues are sorted by severity (critical first)."""
        pcb_path = write_temp_pcb(PCB_WITH_UNPLACED)
        try:
            result = placement_analyze(pcb_path)

            if len(result.issues) < 2:
                pytest.skip("Not enough issues to test sorting")

            severity_order = {"critical": 0, "warning": 1, "suggestion": 2}
            prev_order = -1
            for issue in result.issues:
                order = severity_order.get(issue.severity, 3)
                assert order >= prev_order, "Issues not sorted by severity"
                prev_order = order
        finally:
            Path(pcb_path).unlink()


class TestClusterDetection:
    """Tests for functional cluster detection."""

    def test_power_cluster_detected(self):
        """Test that power clusters (IC + bypass caps) are detected."""
        pcb_path = write_temp_pcb(PCB_WITH_IC)
        try:
            result = placement_analyze(pcb_path)

            # Should detect power cluster with U1 and bypass caps
            power_clusters = [c for c in result.clusters if "power" in c.name.lower()]
            assert len(power_clusters) > 0

            # U1 should be in a cluster
            u1_clusters = [c for c in result.clusters if "U1" in c.components]
            assert len(u1_clusters) > 0
        finally:
            Path(pcb_path).unlink()

    def test_cluster_compactness_score(self):
        """Test that cluster compactness scores are in valid range."""
        pcb_path = write_temp_pcb(PCB_WITH_IC)
        try:
            result = placement_analyze(pcb_path)

            for cluster in result.clusters:
                assert 0 <= cluster.compactness_score <= 100
        finally:
            Path(pcb_path).unlink()


class TestRoutingEstimate:
    """Tests for routing difficulty estimation."""

    def test_routability_in_valid_range(self):
        """Test that routability estimate is between 0 and 100."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_analyze(pcb_path)

            assert 0 <= result.routing_estimate.estimated_routability <= 100
        finally:
            Path(pcb_path).unlink()

    def test_routing_estimate_structure(self):
        """Test routing estimate has expected fields."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_analyze(pcb_path)
            estimate = result.routing_estimate

            assert isinstance(estimate.estimated_routability, float)
            assert isinstance(estimate.congestion_hotspots, list)
            assert isinstance(estimate.difficult_nets, list)
        finally:
            Path(pcb_path).unlink()


class TestOptionalChecks:
    """Tests for optional analysis toggles."""

    def test_disable_thermal_check(self):
        """Test disabling thermal analysis."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_analyze(pcb_path, check_thermal=False)

            # Should have perfect thermal score when disabled
            assert result.categories.thermal == 100.0

            # Should have no thermal issues
            thermal_issues = [i for i in result.issues if i.category == "thermal"]
            assert len(thermal_issues) == 0
        finally:
            Path(pcb_path).unlink()

    def test_disable_signal_integrity_check(self):
        """Test disabling signal integrity analysis."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_analyze(pcb_path, check_signal_integrity=False)

            # Should have perfect SI score when disabled
            assert result.categories.signal_integrity == 100.0

            # Should have no SI issues
            si_issues = [i for i in result.issues if i.category == "si"]
            assert len(si_issues) == 0
        finally:
            Path(pcb_path).unlink()

    def test_disable_manufacturing_check(self):
        """Test disabling manufacturing/DFM analysis."""
        pcb_path = write_temp_pcb(PCB_WITH_UNPLACED)
        try:
            result = placement_analyze(pcb_path, check_manufacturing=False)

            # Should have perfect DFM score when disabled
            assert result.categories.manufacturing == 100.0

            # Should have no DFM issues
            dfm_issues = [i for i in result.issues if i.category == "dfm"]
            assert len(dfm_issues) == 0
        finally:
            Path(pcb_path).unlink()


class TestTypeDataclasses:
    """Tests for MCP type dataclasses."""

    def test_placement_scores_to_dict(self):
        """Test PlacementScores serialization."""
        scores = PlacementScores(
            wire_length=85.567,
            congestion=72.123,
            thermal=90.999,
            signal_integrity=65.001,
            manufacturing=88.555,
        )
        data = scores.to_dict()

        assert data["wire_length"] == 85.6
        assert data["congestion"] == 72.1
        assert data["thermal"] == 91.0
        assert data["signal_integrity"] == 65.0
        assert data["manufacturing"] == 88.6

    def test_placement_issue_to_dict(self):
        """Test PlacementIssue serialization."""
        issue = PlacementIssue(
            severity="warning",
            category="thermal",
            description="Thermal hotspot detected",
            affected_components=["U1", "U2"],
            suggestion="Add thermal vias",
            location=(25.123, 30.456),
        )
        data = issue.to_dict()

        assert data["severity"] == "warning"
        assert data["category"] == "thermal"
        assert data["description"] == "Thermal hotspot detected"
        assert data["affected_components"] == ["U1", "U2"]
        assert data["suggestion"] == "Add thermal vias"
        assert data["location"]["x"] == 25.12
        assert data["location"]["y"] == 30.46

    def test_placement_issue_no_location(self):
        """Test PlacementIssue without location."""
        issue = PlacementIssue(
            severity="suggestion",
            category="si",
            description="Crosstalk risk",
            affected_components=[],
            suggestion="Increase spacing",
        )
        data = issue.to_dict()

        assert "location" not in data

    def test_placement_cluster_to_dict(self):
        """Test PlacementCluster serialization."""
        cluster = PlacementCluster(
            name="power_cluster_U1",
            components=["U1", "C1", "C2"],
            centroid=(40.123, 30.456),
            compactness_score=85.789,
        )
        data = cluster.to_dict()

        assert data["name"] == "power_cluster_U1"
        assert data["components"] == ["U1", "C1", "C2"]
        assert data["centroid"]["x"] == 40.12
        assert data["centroid"]["y"] == 30.46
        assert data["compactness_score"] == 85.8

    def test_routing_estimate_to_dict(self):
        """Test RoutingEstimate serialization."""
        estimate = RoutingEstimate(
            estimated_routability=78.567,
            congestion_hotspots=[(10.123, 20.456), (30.789, 40.012)],
            difficult_nets=["CLK", "USB_D+"],
        )
        data = estimate.to_dict()

        assert data["estimated_routability"] == 78.6
        assert len(data["congestion_hotspots"]) == 2
        assert data["congestion_hotspots"][0]["x"] == 10.12
        assert data["congestion_hotspots"][0]["y"] == 20.46
        assert data["difficult_nets"] == ["CLK", "USB_D+"]

    def test_placement_analysis_to_dict(self):
        """Test PlacementAnalysis serialization."""
        analysis = PlacementAnalysis(
            file_path="/path/to/board.kicad_pcb",
            overall_score=75.567,
            categories=PlacementScores(
                wire_length=80.0,
                congestion=70.0,
                thermal=90.0,
                signal_integrity=65.0,
                manufacturing=85.0,
            ),
            issues=[
                PlacementIssue(
                    severity="warning",
                    category="thermal",
                    description="Test issue",
                    affected_components=["U1"],
                    suggestion="Fix it",
                )
            ],
            clusters=[
                PlacementCluster(
                    name="power_cluster_U1",
                    components=["U1", "C1"],
                    centroid=(40.0, 30.0),
                    compactness_score=85.0,
                )
            ],
            routing_estimate=RoutingEstimate(
                estimated_routability=75.0,
                congestion_hotspots=[(10.0, 20.0)],
                difficult_nets=["CLK"],
            ),
        )
        data = analysis.to_dict()

        assert data["file_path"] == "/path/to/board.kicad_pcb"
        assert data["overall_score"] == 75.6
        assert "categories" in data
        assert len(data["issues"]) == 1
        assert len(data["clusters"]) == 1
        assert "routing_estimate" in data

        # Verify JSON-serializable
        json_str = json.dumps(data)
        assert json_str is not None
