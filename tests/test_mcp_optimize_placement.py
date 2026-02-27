"""Tests for MCP optimize_placement and evaluate_placement tools.

Tests the MCP tool functions that expose placement optimization and evaluation
to AI agents via the tool registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.mcp.tools.optimize_placement import (
    _breakdown_to_dict,
    _build_footprint_sizes,
    _parse_weights,
    _validate_pcb_path,
    evaluate_placement,
    optimize_placement,
)
from kicad_tools.placement.cost import CostBreakdown, PlacementCostConfig

# Use the small voltage divider board for integration tests
VOLTAGE_DIVIDER_PCB = str(
    Path(__file__).parent.parent
    / "boards"
    / "01-voltage-divider"
    / "output"
    / "voltage_divider.kicad_pcb"
)


# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------


class TestValidatePcbPath:
    """Tests for _validate_pcb_path."""

    def test_nonexistent_file_raises(self):
        """Non-existent file raises FileNotFoundError."""
        with pytest.raises(Exception, match="PCB file not found"):
            _validate_pcb_path("/nonexistent/path/board.kicad_pcb")

    def test_wrong_extension_raises(self, tmp_path):
        """File with wrong extension raises ParseError."""
        bad_file = tmp_path / "board.txt"
        bad_file.write_text("not a pcb file")
        with pytest.raises(Exception, match="Invalid file extension"):
            _validate_pcb_path(str(bad_file))

    def test_valid_path_returns_path(self, tmp_path):
        """Valid .kicad_pcb file returns Path object."""
        good_file = tmp_path / "board.kicad_pcb"
        good_file.write_text("(kicad_pcb ...)")
        result = _validate_pcb_path(str(good_file))
        assert isinstance(result, Path)
        assert result == good_file


class TestParseWeights:
    """Tests for _parse_weights."""

    def test_none_returns_defaults(self):
        """None input returns default config."""
        config = _parse_weights(None)
        assert isinstance(config, PlacementCostConfig)
        assert config.wirelength_weight == 1.0

    def test_custom_weights(self):
        """Custom weights override defaults."""
        config = _parse_weights({"wirelength": 2.0, "overlap": 500.0})
        assert config.wirelength_weight == 2.0
        assert config.overlap_weight == 500.0
        # Unspecified weights use defaults
        assert config.drc_weight == 1e4

    def test_empty_dict_returns_defaults(self):
        """Empty dict returns default config."""
        config = _parse_weights({})
        assert config.wirelength_weight == 1.0


class TestBreakdownToDict:
    """Tests for _breakdown_to_dict."""

    def test_converts_breakdown(self):
        """CostBreakdown is properly converted to dict."""
        breakdown = CostBreakdown(
            wirelength=10.1234,
            overlap=0.0,
            boundary=0.0,
            drc=2.0,
            area=50.5678,
        )
        result = _breakdown_to_dict(breakdown)
        assert result["wirelength"] == 10.1234
        assert result["overlap"] == 0.0
        assert result["drc"] == 2.0
        assert result["area"] == 50.5678


class TestBuildFootprintSizes:
    """Tests for _build_footprint_sizes."""

    def test_builds_sizes(self):
        """ComponentDefs are properly converted to size dict."""
        from kicad_tools.placement.vector import ComponentDef

        components = [
            ComponentDef(reference="R1", width=2.0, height=1.0),
            ComponentDef(reference="C1", width=1.5, height=1.5),
        ]
        sizes = _build_footprint_sizes(components)
        assert sizes["R1"] == (2.0, 1.0)
        assert sizes["C1"] == (1.5, 1.5)


# ---------------------------------------------------------------------------
# Integration tests for evaluate_placement
# ---------------------------------------------------------------------------


class TestEvaluatePlacement:
    """Tests for the evaluate_placement MCP tool."""

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="Voltage divider board not available",
    )
    def test_evaluate_placement_success(self):
        """evaluate_placement returns structured result for a valid board."""
        result = evaluate_placement(VOLTAGE_DIVIDER_PCB)

        assert result["success"] is True
        assert "score" in result
        assert isinstance(result["score"], float)
        assert "feasible" in result
        assert isinstance(result["feasible"], bool)
        assert "breakdown" in result
        assert "wirelength" in result["breakdown"]
        assert "overlap" in result["breakdown"]
        assert "drc" in result["breakdown"]
        assert "boundary" in result["breakdown"]
        assert "area" in result["breakdown"]
        assert result["component_count"] > 0
        assert result["net_count"] >= 0
        assert "board_dimensions" in result
        assert "width_mm" in result["board_dimensions"]
        assert "height_mm" in result["board_dimensions"]

    def test_evaluate_placement_missing_file(self):
        """evaluate_placement raises on missing file."""
        with pytest.raises(Exception, match="PCB file not found"):
            evaluate_placement("/nonexistent/board.kicad_pcb")

    def test_evaluate_placement_bad_extension(self, tmp_path):
        """evaluate_placement raises on wrong extension."""
        bad_file = tmp_path / "board.txt"
        bad_file.write_text("not a pcb")
        with pytest.raises(Exception, match="Invalid file extension"):
            evaluate_placement(str(bad_file))

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="Voltage divider board not available",
    )
    def test_evaluate_with_custom_weights(self):
        """evaluate_placement accepts custom weights."""
        result = evaluate_placement(
            VOLTAGE_DIVIDER_PCB,
            weights={"wirelength": 10.0, "overlap": 1e8},
        )
        assert result["success"] is True
        assert isinstance(result["score"], float)


# ---------------------------------------------------------------------------
# Integration tests for optimize_placement
# ---------------------------------------------------------------------------


class TestOptimizePlacement:
    """Tests for the optimize_placement MCP tool."""

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="Voltage divider board not available",
    )
    def test_optimize_placement_success(self):
        """optimize_placement returns structured result for a valid board."""
        # Use very few iterations for fast test
        result = optimize_placement(
            VOLTAGE_DIVIDER_PCB,
            max_iterations=5,
        )

        assert result["success"] is True
        assert "initial_score" in result
        assert "final_score" in result
        assert "total" in result["initial_score"]
        assert "feasible" in result["initial_score"]
        assert "breakdown" in result["initial_score"]
        assert "total" in result["final_score"]
        assert "feasible" in result["final_score"]
        assert "breakdown" in result["final_score"]
        assert "improvement_pct" in result
        assert isinstance(result["improvement_pct"], float)
        assert result["iterations"] >= 0
        assert isinstance(result["converged"], bool)
        assert result["wall_time_s"] >= 0
        assert result["component_count"] > 0
        assert "convergence_data" in result
        assert isinstance(result["convergence_data"], list)

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="Voltage divider board not available",
    )
    def test_optimize_placement_with_output(self, tmp_path):
        """optimize_placement writes output file when output_path is specified."""
        output_file = tmp_path / "optimized.kicad_pcb"
        result = optimize_placement(
            VOLTAGE_DIVIDER_PCB,
            max_iterations=3,
            output_path=str(output_file),
        )

        assert result["success"] is True
        assert result.get("output_path") == str(output_file)
        assert output_file.exists()
        content = output_file.read_text()
        assert "(kicad_pcb" in content or "(footprint" in content

    def test_optimize_placement_missing_file(self):
        """optimize_placement raises on missing file."""
        with pytest.raises(Exception, match="PCB file not found"):
            optimize_placement("/nonexistent/board.kicad_pcb")

    def test_optimize_placement_unknown_strategy(self, tmp_path):
        """optimize_placement returns error for unknown strategy."""
        # Create a minimal PCB file
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(
            "(kicad_pcb (version 20230101) (generator test)"
            " (general) (layers (0 F.Cu signal))"
            " (setup))"
        )
        result = optimize_placement(str(pcb_file), strategy="nonexistent")
        # The file may fail to parse first, or may succeed and report unknown strategy
        # Either way we expect a non-successful result or an exception
        if isinstance(result, dict):
            assert result["success"] is False

    def test_optimize_placement_unknown_seed(self, tmp_path):
        """optimize_placement returns error for unknown seed method."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(
            "(kicad_pcb (version 20230101) (generator test)"
            " (general) (layers (0 F.Cu signal))"
            " (setup))"
        )
        result = optimize_placement(str(pcb_file), seed_method="nonexistent")
        if isinstance(result, dict):
            assert result["success"] is False

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="Voltage divider board not available",
    )
    def test_optimize_with_random_seed(self):
        """optimize_placement works with random seed method."""
        result = optimize_placement(
            VOLTAGE_DIVIDER_PCB,
            max_iterations=3,
            seed_method="random",
        )

        assert result["success"] is True
        assert result["component_count"] > 0


# ---------------------------------------------------------------------------
# Registry integration tests
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    """Tests that tools are properly registered in the MCP registry."""

    def test_optimize_placement_registered(self):
        """optimize_placement tool is in the registry."""
        from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

        assert "optimize_placement" in TOOL_REGISTRY

    def test_evaluate_placement_registered(self):
        """evaluate_placement tool is in the registry."""
        from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

        assert "evaluate_placement" in TOOL_REGISTRY

    def test_optimize_placement_spec(self):
        """optimize_placement tool has correct spec."""
        from kicad_tools.mcp.tools.registry import get_tool

        tool = get_tool("optimize_placement")
        assert tool is not None
        assert tool.category == "placement"
        assert "pcb_path" in tool.parameters["properties"]
        assert "pcb_path" in tool.parameters["required"]
        assert callable(tool.handler)

    def test_evaluate_placement_spec(self):
        """evaluate_placement tool has correct spec."""
        from kicad_tools.mcp.tools.registry import get_tool

        tool = get_tool("evaluate_placement")
        assert tool is not None
        assert tool.category == "placement"
        assert "pcb_path" in tool.parameters["properties"]
        assert "pcb_path" in tool.parameters["required"]
        assert callable(tool.handler)

    def test_placement_tools_in_category(self):
        """Both new tools appear in placement category listing."""
        from kicad_tools.mcp.tools.registry import list_tools

        placement_tools = list_tools(category="placement")
        names = [t.name for t in placement_tools]
        assert "optimize_placement" in names
        assert "evaluate_placement" in names

    def test_handler_dispatches_correctly(self):
        """Registry handler correctly dispatches to the tool function."""
        from kicad_tools.mcp.tools.registry import get_tool

        tool = get_tool("evaluate_placement")
        assert tool is not None

        # Calling with invalid path should raise KiCad FileNotFoundError
        from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError

        with pytest.raises(KiCadFileNotFoundError, match="PCB file not found"):
            tool.handler({"pcb_path": "/nonexistent/board.kicad_pcb"})
