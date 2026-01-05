"""Tests for kicad_tools.mcp.tools.placement module."""

import json
import tempfile
from pathlib import Path

import pytest

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.mcp.tools.placement import placement_suggestions
from kicad_tools.mcp.types import (
    PlacementSuggestion,
    PlacementSuggestionsResult,
    Position,
)

# Simple PCB with multiple components for testing placement suggestions
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

  (gr_line (start 0 0) (end 100 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 100 0) (end 100 80) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 100 80) (end 0 80) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 80) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "QFP-32"
    (layer "F.Cu")
    (at 50 40)
    (attr smd)
    (property "Reference" "U1")
    (pad "1" smd rect (at -5 -5) (size 0.5 1.5) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at -5 -3) (size 0.5 1.5) (layers "F.Cu") (net 2 "GND"))
    (pad "3" smd rect (at -5 -1) (size 0.5 1.5) (layers "F.Cu") (net 3 "SIG1"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 20 20)
    (attr smd)
    (property "Reference" "C1")
    (property "Value" "100nF")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 80 60)
    (attr smd)
    (property "Reference" "R1")
    (property "Value" "10k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "R_0603"
    (layer "F.Cu")
    (at 30 60)
    (attr smd)
    (property "Reference" "R2")
    (property "Value" "4.7k")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 3 "SIG1"))
  )
)
"""


# PCB with thermal considerations (multiple ICs close together)
THERMAL_PCB = """(kicad_pcb
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

  (gr_line (start 0 0) (end 80 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 80 0) (end 80 60) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 80 60) (end 0 60) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 60) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "QFP-32"
    (layer "F.Cu")
    (at 25 30)
    (attr smd)
    (property "Reference" "U1")
    (pad "1" smd rect (at -3 -3) (size 0.5 1.5) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at -3 -1) (size 0.5 1.5) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "QFP-32"
    (layer "F.Cu")
    (at 30 32)
    (attr smd)
    (property "Reference" "U2")
    (pad "1" smd rect (at -3 -3) (size 0.5 1.5) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at -3 -1) (size 0.5 1.5) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "C_0603"
    (layer "F.Cu")
    (at 60 30)
    (attr smd)
    (property "Reference" "C1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )
)
"""


def write_temp_pcb(content: str) -> str:
    """Write PCB content to a temporary file and return the path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".kicad_pcb", delete=False) as f:
        f.write(content)
        return f.name


class TestPlacementSuggestionsBasic:
    """Basic functionality tests for placement_suggestions."""

    def test_basic_suggestions(self):
        """Test generating placement suggestions for a simple PCB."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_suggestions(pcb_path)

            assert isinstance(result, PlacementSuggestionsResult)
            assert result.current_score > 0
            assert result.strategy == "balanced"
            assert result.component_filter is None
        finally:
            Path(pcb_path).unlink()

    def test_file_not_found_error(self):
        """Test that FileNotFoundError is raised for missing files."""
        with pytest.raises(KiCadFileNotFoundError):
            placement_suggestions("/nonexistent/path/to/board.kicad_pcb")

    def test_invalid_file_extension(self):
        """Test that ParseError is raised for invalid file extensions."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"not a pcb file")
            path = f.name
        try:
            with pytest.raises(ParseError):
                placement_suggestions(path)
        finally:
            Path(path).unlink()

    def test_to_dict_serialization(self):
        """Test that PlacementSuggestionsResult can be serialized to dict/JSON."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_suggestions(pcb_path)
            data = result.to_dict()

            # Verify it's JSON-serializable
            json_str = json.dumps(data)
            assert json_str is not None

            # Verify structure
            assert "suggestions" in data
            assert "current_score" in data
            assert "potential_score" in data
            assert "strategy" in data
            assert "component_filter" in data
        finally:
            Path(pcb_path).unlink()


class TestPlacementSuggestionsParameters:
    """Tests for placement_suggestions parameter handling."""

    def test_max_suggestions_limit(self):
        """Test that max_suggestions limits the number of results."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_suggestions(pcb_path, max_suggestions=2)

            assert len(result.suggestions) <= 2
        finally:
            Path(pcb_path).unlink()

    def test_component_filter(self):
        """Test filtering suggestions to a specific component."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_suggestions(pcb_path, component="C1")

            assert result.component_filter == "C1"
            # All suggestions should be for C1
            for suggestion in result.suggestions:
                assert suggestion.component == "C1"
        finally:
            Path(pcb_path).unlink()

    def test_component_not_found(self):
        """Test that ValueError is raised for unknown component."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            with pytest.raises(ValueError) as exc_info:
                placement_suggestions(pcb_path, component="NONEXISTENT")

            assert "not found" in str(exc_info.value)
        finally:
            Path(pcb_path).unlink()

    def test_strategy_wire_length(self):
        """Test wire_length optimization strategy."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_suggestions(pcb_path, strategy="wire_length")

            assert result.strategy == "wire_length"
        finally:
            Path(pcb_path).unlink()

    def test_strategy_thermal(self):
        """Test thermal optimization strategy."""
        pcb_path = write_temp_pcb(THERMAL_PCB)
        try:
            result = placement_suggestions(pcb_path, strategy="thermal")

            assert result.strategy == "thermal"
        finally:
            Path(pcb_path).unlink()

    def test_strategy_si(self):
        """Test signal integrity optimization strategy."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_suggestions(pcb_path, strategy="si")

            assert result.strategy == "si"
        finally:
            Path(pcb_path).unlink()


class TestSuggestionContent:
    """Tests for placement suggestion content and structure."""

    def test_suggestion_has_required_fields(self):
        """Test that suggestions contain all required fields."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_suggestions(pcb_path, max_suggestions=5)

            for suggestion in result.suggestions:
                assert isinstance(suggestion, PlacementSuggestion)
                assert suggestion.priority >= 1
                assert suggestion.component is not None
                assert suggestion.action in ("move", "rotate", "swap")
                assert isinstance(suggestion.current_position, Position)
                assert isinstance(suggestion.suggested_position, Position)
                assert suggestion.score_improvement >= 0
                assert suggestion.rationale is not None
                assert 0.0 <= suggestion.confidence <= 1.0
                assert isinstance(suggestion.side_effects, list)
        finally:
            Path(pcb_path).unlink()

    def test_suggestions_are_prioritized(self):
        """Test that suggestions are sorted by priority."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_suggestions(pcb_path, max_suggestions=10)

            if len(result.suggestions) > 1:
                priorities = [s.priority for s in result.suggestions]
                assert priorities == list(range(1, len(result.suggestions) + 1))
        finally:
            Path(pcb_path).unlink()

    def test_position_to_dict(self):
        """Test Position serialization."""
        pos = Position(x=45.123, y=32.456, rotation=90.0)
        data = pos.to_dict()

        assert data["x"] == 45.123
        assert data["y"] == 32.456
        assert data["rotation"] == 90.0

    def test_suggestion_to_dict(self):
        """Test PlacementSuggestion serialization."""
        suggestion = PlacementSuggestion(
            priority=1,
            component="C1",
            action="move",
            current_position=Position(20.0, 30.0, 0.0),
            suggested_position=Position(25.0, 35.0, 0.0),
            score_improvement=3.5,
            rationale="Move closer to IC",
            confidence=0.85,
            side_effects=["May affect routing"],
        )
        data = suggestion.to_dict()

        assert data["priority"] == 1
        assert data["component"] == "C1"
        assert data["action"] == "move"
        assert data["current_position"]["x"] == 20.0
        assert data["suggested_position"]["x"] == 25.0
        assert data["score_improvement"] == 3.5
        assert data["rationale"] == "Move closer to IC"
        assert data["confidence"] == 0.85
        assert data["side_effects"] == ["May affect routing"]


class TestScoreCalculation:
    """Tests for placement score calculations."""

    def test_potential_score_less_than_current(self):
        """Test that potential score is less than or equal to current score."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_suggestions(pcb_path)

            # Potential score should be better (lower) if suggestions exist
            if result.suggestions:
                assert result.potential_score <= result.current_score
        finally:
            Path(pcb_path).unlink()

    def test_score_improvement_sums_correctly(self):
        """Test that individual improvements sum to expected delta."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = placement_suggestions(pcb_path)

            total_improvement = sum(s.score_improvement for s in result.suggestions)
            expected_delta = result.current_score - result.potential_score

            # Allow small floating point tolerance
            assert abs(total_improvement - expected_delta) < 0.01
        finally:
            Path(pcb_path).unlink()


class TestTypeDataclasses:
    """Tests for MCP placement type dataclasses."""

    def test_position_defaults(self):
        """Test Position default values."""
        pos = Position(x=10.0, y=20.0)

        assert pos.x == 10.0
        assert pos.y == 20.0
        assert pos.rotation == 0.0

    def test_placement_suggestion_defaults(self):
        """Test PlacementSuggestion default values."""
        suggestion = PlacementSuggestion(
            priority=1,
            component="R1",
            action="move",
            current_position=Position(0, 0),
            suggested_position=Position(10, 10),
            score_improvement=1.0,
            rationale="Test",
        )

        assert suggestion.confidence == 0.8
        assert suggestion.side_effects == []

    def test_placement_suggestions_result_defaults(self):
        """Test PlacementSuggestionsResult default values."""
        result = PlacementSuggestionsResult(
            suggestions=[],
            current_score=100.0,
            potential_score=100.0,
        )

        assert result.strategy == "balanced"
        assert result.component_filter is None

    def test_placement_suggestions_result_to_dict(self):
        """Test PlacementSuggestionsResult serialization."""
        result = PlacementSuggestionsResult(
            suggestions=[
                PlacementSuggestion(
                    priority=1,
                    component="C1",
                    action="move",
                    current_position=Position(10, 20),
                    suggested_position=Position(15, 25),
                    score_improvement=2.5,
                    rationale="Improve routing",
                )
            ],
            current_score=85.5,
            potential_score=83.0,
            strategy="balanced",
            component_filter=None,
        )
        data = result.to_dict()

        assert len(data["suggestions"]) == 1
        assert data["current_score"] == 85.5
        assert data["potential_score"] == 83.0
        assert data["strategy"] == "balanced"
        assert data["component_filter"] is None


class TestRealPCBFiles:
    """Tests using real KiCad PCB fixture files."""

    @pytest.fixture
    def test_project_pcb(self) -> str:
        """Path to test project PCB fixture."""
        return str(Path(__file__).parent / "fixtures" / "projects" / "test_project.kicad_pcb")

    def test_analyze_test_project(self, test_project_pcb):
        """Test analyzing the test project PCB fixture."""
        if not Path(test_project_pcb).exists():
            pytest.skip("Test fixture not found")

        result = placement_suggestions(test_project_pcb, max_suggestions=5)

        # Verify basic structure
        assert isinstance(result, PlacementSuggestionsResult)
        assert result.current_score > 0
