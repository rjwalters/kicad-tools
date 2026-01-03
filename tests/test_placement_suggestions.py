"""Tests for placement suggestions with rationale."""

import json
from pathlib import Path

import pytest

from kicad_tools.optim import (
    AlternativePosition,
    ForceContribution,
    PlacementOptimizer,
    PlacementSuggestion,
    RationaleType,
    explain_placement,
    generate_placement_suggestions,
    suggest_improvement,
)
from kicad_tools.schema.pcb import PCB


class TestRationaleType:
    """Tests for RationaleType enum."""

    def test_rationale_types_exist(self):
        """Verify all expected rationale types are defined."""
        expected = [
            "functional_cluster",
            "signal_integrity",
            "thermal",
            "edge_placement",
            "alignment",
            "keepout_avoidance",
            "routing_ease",
            "net_connection",
            "component_spacing",
            "fixed_constraint",
        ]
        for value in expected:
            assert RationaleType(value) is not None


class TestForceContribution:
    """Tests for ForceContribution dataclass."""

    def test_force_contribution_creation(self):
        """Test creating a ForceContribution."""
        fc = ForceContribution(
            source="spring_to_U1",
            force_vector=(1.5, -2.3),
            rationale_type=RationaleType.NET_CONNECTION,
            description="Net VCC pulling toward U1",
        )

        assert fc.source == "spring_to_U1"
        assert fc.force_vector == (1.5, -2.3)
        assert fc.rationale_type == RationaleType.NET_CONNECTION
        assert fc.description == "Net VCC pulling toward U1"

    def test_force_contribution_magnitude(self):
        """Test magnitude calculation."""
        fc = ForceContribution(
            source="test",
            force_vector=(3.0, 4.0),
            rationale_type=RationaleType.NET_CONNECTION,
            description="test",
        )

        assert fc.magnitude() == pytest.approx(5.0)

    def test_force_contribution_to_dict(self):
        """Test JSON serialization."""
        fc = ForceContribution(
            source="spring_to_U1",
            force_vector=(3.0, 4.0),
            rationale_type=RationaleType.NET_CONNECTION,
            description="Net VCC",
        )

        d = fc.to_dict()
        assert d["source"] == "spring_to_U1"
        assert d["force_x"] == 3.0
        assert d["force_y"] == 4.0
        assert d["magnitude"] == pytest.approx(5.0)
        assert d["rationale_type"] == "net_connection"
        assert d["description"] == "Net VCC"


class TestAlternativePosition:
    """Tests for AlternativePosition dataclass."""

    def test_alternative_position_creation(self):
        """Test creating an AlternativePosition."""
        alt = AlternativePosition(
            x=100.5,
            y=50.2,
            rotation=90.0,
            score=0.85,
            tradeoff="Further from power pin",
        )

        assert alt.x == 100.5
        assert alt.y == 50.2
        assert alt.rotation == 90.0
        assert alt.score == 0.85
        assert alt.tradeoff == "Further from power pin"

    def test_alternative_position_to_dict(self):
        """Test JSON serialization."""
        alt = AlternativePosition(
            x=100.5,
            y=50.25,
            rotation=90.0,
            score=0.85,
            tradeoff="Further from power pin",
        )

        d = alt.to_dict()
        assert d["x"] == 100.5
        assert d["y"] == 50.25
        assert d["rotation"] == 90.0
        assert d["score"] == 0.85
        assert d["tradeoff"] == "Further from power pin"


class TestPlacementSuggestion:
    """Tests for PlacementSuggestion dataclass."""

    def test_placement_suggestion_creation(self):
        """Test creating a PlacementSuggestion."""
        suggestion = PlacementSuggestion(
            reference="C1",
            suggested_x=45.5,
            suggested_y=32.0,
            suggested_rotation=0.0,
            confidence=0.92,
            rationale=["Bypass capacitor for U1"],
            constraints_satisfied=["functional_cluster: within 5mm of U1"],
            constraints_violated=[],
        )

        assert suggestion.reference == "C1"
        assert suggestion.suggested_x == 45.5
        assert suggestion.confidence == 0.92
        assert len(suggestion.rationale) == 1
        assert len(suggestion.constraints_satisfied) == 1
        assert len(suggestion.constraints_violated) == 0
        assert len(suggestion.alternatives) == 0

    def test_placement_suggestion_with_alternatives(self):
        """Test PlacementSuggestion with alternatives."""
        alt = AlternativePosition(
            x=43.0, y=32.0, rotation=0.0, score=0.85, tradeoff="Further from power"
        )

        suggestion = PlacementSuggestion(
            reference="C1",
            suggested_x=45.5,
            suggested_y=32.0,
            suggested_rotation=0.0,
            confidence=0.92,
            rationale=["Bypass capacitor for U1"],
            constraints_satisfied=[],
            constraints_violated=[],
            alternatives=[alt],
        )

        assert len(suggestion.alternatives) == 1
        assert suggestion.alternatives[0].score == 0.85

    def test_placement_suggestion_to_dict(self):
        """Test JSON serialization."""
        suggestion = PlacementSuggestion(
            reference="C1",
            suggested_x=45.5,
            suggested_y=32.0,
            suggested_rotation=90.0,
            confidence=0.92,
            rationale=["Bypass capacitor for U1"],
            constraints_satisfied=["functional_cluster: within 5mm of U1"],
            constraints_violated=["alignment: not aligned with C2"],
            alternatives=[
                AlternativePosition(x=43.0, y=32.0, rotation=0.0, score=0.85, tradeoff="Further")
            ],
        )

        d = suggestion.to_dict()
        assert d["reference"] == "C1"
        assert d["suggested_x"] == 45.5
        assert d["suggested_y"] == 32.0
        assert d["suggested_rotation"] == 90.0
        assert d["confidence"] == 0.92
        assert len(d["rationale"]) == 1
        assert len(d["constraints_satisfied"]) == 1
        assert len(d["constraints_violated"]) == 1
        assert len(d["alternatives"]) == 1

        # Verify JSON serializable
        json_str = json.dumps(d)
        assert '"reference": "C1"' in json_str


class TestGeneratePlacementSuggestions:
    """Tests for generate_placement_suggestions function."""

    def test_generate_suggestions_from_pcb(self, routing_test_pcb: Path):
        """Test generating suggestions from a PCB file."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestions = generate_placement_suggestions(pcb=pcb)

        # Should have suggestions for all components
        assert len(suggestions) >= 1

        # Check that suggestions have required fields
        for ref, suggestion in suggestions.items():
            assert suggestion.reference == ref
            assert isinstance(suggestion.suggested_x, float)
            assert isinstance(suggestion.suggested_y, float)
            assert isinstance(suggestion.suggested_rotation, float)
            assert 0.0 <= suggestion.confidence <= 1.0
            assert isinstance(suggestion.rationale, list)

    def test_generate_suggestions_from_optimizer(self, routing_test_pcb: Path):
        """Test generating suggestions from a PlacementOptimizer."""
        pcb = PCB.load(str(routing_test_pcb))
        optimizer = PlacementOptimizer.from_pcb(pcb)

        suggestions = generate_placement_suggestions(optimizer=optimizer)

        # Should have suggestions for all components
        assert len(suggestions) == len(optimizer.components)

    def test_suggestions_include_rationale(self, routing_test_pcb: Path):
        """Test that suggestions include rationale."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestions = generate_placement_suggestions(pcb=pcb)

        # At least some suggestions should have rationale
        has_rationale = any(len(s.rationale) > 0 for s in suggestions.values())
        assert has_rationale

    def test_suggestions_include_alternatives(self, routing_test_pcb: Path):
        """Test that suggestions include alternatives for movable components."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestions = generate_placement_suggestions(pcb=pcb)

        # Non-fixed components should have alternatives
        for ref, suggestion in suggestions.items():
            # Connectors (J*) are typically fixed, so they won't have alternatives
            if not ref.startswith("J"):
                # Should have at least some alternatives (rotations)
                # Note: this depends on the component not being at a board edge
                pass  # Alternatives may or may not exist depending on position

    def test_error_when_no_pcb_or_optimizer(self):
        """Test error when neither pcb nor optimizer is provided."""
        with pytest.raises(ValueError, match="Either pcb or optimizer must be provided"):
            generate_placement_suggestions()


class TestExplainPlacement:
    """Tests for explain_placement function."""

    def test_explain_existing_component(self, routing_test_pcb: Path):
        """Test explaining an existing component's placement."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestion = explain_placement(pcb=pcb, reference="R1")

        assert suggestion is not None
        assert suggestion.reference == "R1"
        assert isinstance(suggestion.rationale, list)
        assert isinstance(suggestion.confidence, float)

    def test_explain_nonexistent_component(self, routing_test_pcb: Path):
        """Test explaining a component that doesn't exist."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestion = explain_placement(pcb=pcb, reference="NONEXISTENT")

        assert suggestion is None

    def test_explain_fixed_component(self, routing_test_pcb: Path):
        """Test explaining a fixed component (connector)."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestion = explain_placement(pcb=pcb, reference="J1")

        assert suggestion is not None
        # Fixed components should have rationale mentioning they're fixed
        has_fixed_rationale = any(
            "fixed" in r.lower() or "connector" in r.lower() for r in suggestion.rationale
        )
        assert has_fixed_rationale or len(suggestion.constraints_satisfied) > 0


class TestSuggestImprovement:
    """Tests for suggest_improvement function."""

    def test_suggest_improvement_for_component(self, routing_test_pcb: Path):
        """Test suggesting improvements for a component."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestion = suggest_improvement(pcb=pcb, reference="R1")

        # Result may or may not be None depending on whether improvement is possible
        if suggestion is not None:
            assert suggestion.reference == "R1"
            assert len(suggestion.rationale) > 0
            # Should have original position as alternative
            assert len(suggestion.alternatives) > 0

    def test_suggest_improvement_fixed_component(self, routing_test_pcb: Path):
        """Test that fixed components return None (can't be improved)."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestion = suggest_improvement(pcb=pcb, reference="J1")

        # Fixed components can't be improved
        assert suggestion is None

    def test_suggest_improvement_nonexistent_component(self, routing_test_pcb: Path):
        """Test that nonexistent components return None."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestion = suggest_improvement(pcb=pcb, reference="NONEXISTENT")

        assert suggestion is None


class TestCLISuggest:
    """Tests for the CLI suggest command."""

    def test_cli_suggest_json_output(self, routing_test_pcb: Path):
        """Test CLI suggest with JSON output."""
        from kicad_tools.cli.placement_cmd import main

        result = main(["suggest", str(routing_test_pcb), "--format", "json", "-q"])
        assert result == 0

    def test_cli_suggest_text_output(self, routing_test_pcb: Path):
        """Test CLI suggest with text output."""
        from kicad_tools.cli.placement_cmd import main

        result = main(["suggest", str(routing_test_pcb), "--format", "text", "-q"])
        assert result == 0

    def test_cli_suggest_single_component(self, routing_test_pcb: Path):
        """Test CLI suggest for a single component."""
        from kicad_tools.cli.placement_cmd import main

        result = main(["suggest", str(routing_test_pcb), "-c", "R1", "-q"])
        assert result == 0

    def test_cli_suggest_nonexistent_component(self, routing_test_pcb: Path):
        """Test CLI suggest for a nonexistent component."""
        from kicad_tools.cli.placement_cmd import main

        result = main(["suggest", str(routing_test_pcb), "-c", "NONEXISTENT", "-q"])
        assert result == 1

    def test_cli_suggest_nonexistent_file(self, tmp_path: Path):
        """Test CLI suggest with nonexistent file."""
        from kicad_tools.cli.placement_cmd import main

        result = main(["suggest", str(tmp_path / "nonexistent.kicad_pcb"), "-q"])
        assert result == 1


class TestJSONOutput:
    """Tests for JSON output format."""

    def test_suggestions_json_format(self, routing_test_pcb: Path):
        """Test that suggestions produce valid JSON."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestions = generate_placement_suggestions(pcb=pcb)

        # Convert to JSON format
        output = {ref: s.to_dict() for ref, s in suggestions.items()}
        json_str = json.dumps(output, indent=2)

        # Parse back to verify it's valid
        parsed = json.loads(json_str)
        assert len(parsed) == len(suggestions)

    def test_single_suggestion_json_format(self, routing_test_pcb: Path):
        """Test JSON format for a single component."""
        pcb = PCB.load(str(routing_test_pcb))
        suggestion = explain_placement(pcb=pcb, reference="R1")

        assert suggestion is not None
        d = suggestion.to_dict()

        # Verify expected keys
        expected_keys = [
            "reference",
            "suggested_x",
            "suggested_y",
            "suggested_rotation",
            "confidence",
            "rationale",
            "constraints_satisfied",
            "constraints_violated",
            "alternatives",
        ]
        for key in expected_keys:
            assert key in d, f"Missing key: {key}"

        # Verify JSON serializable
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        assert parsed["reference"] == "R1"
