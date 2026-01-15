"""Tests for build command routing parameter auto-calculation.

Verifies that the grid is auto-calculated from clearance to prevent
DRC violations when routing (issue #723).

Also tests reading routing parameters from project.kct spec (issue #726).
"""

import pytest

from kicad_tools.cli.build_cmd import _get_routing_params, _parse_dimension_mm
from kicad_tools.spec.schema import (
    ManufacturingRequirements,
    ProjectMetadata,
    ProjectSpec,
    Requirements,
)


class TestGetRoutingParams:
    """Tests for _get_routing_params function."""

    def test_jlcpcb_grid_compatible_with_clearance(self):
        """JLCPCB grid should be compatible with its clearance (issue #723)."""
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params("jlcpcb")

        # Grid must be <= clearance / 2 to allow routing without DRC violations
        assert grid <= clearance / 2, (
            f"Grid {grid}mm must be <= clearance/2 ({clearance / 2}mm) to prevent DRC violations"
        )

        # Verify we got JLCPCB-specific values (~5 mil = 0.127mm clearance)
        assert clearance > 0.1  # JLCPCB has reasonable clearance
        assert trace_width > 0
        assert via_drill > 0
        assert via_diameter > via_drill

    def test_seeed_grid_compatible_with_clearance(self):
        """Seeed grid should be compatible with its clearance."""
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params("seeed")

        assert grid <= clearance / 2, f"Grid {grid}mm must be <= clearance/2 ({clearance / 2}mm)"

    def test_pcbway_grid_compatible_with_clearance(self):
        """PCBWay grid should be compatible with its clearance."""
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params("pcbway")

        assert grid <= clearance / 2, f"Grid {grid}mm must be <= clearance/2 ({clearance / 2}mm)"

    def test_oshpark_grid_compatible_with_clearance(self):
        """OSH Park grid should be compatible with its clearance."""
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params("oshpark")

        assert grid <= clearance / 2, f"Grid {grid}mm must be <= clearance/2 ({clearance / 2}mm)"

    def test_grid_rounded_to_clean_value(self):
        """Grid should be rounded to 0.05mm increments."""
        grid, _, _, _, _ = _get_routing_params("jlcpcb")

        # Check grid is a multiple of 0.05mm
        assert grid * 20 == pytest.approx(round(grid * 20), abs=0.001), (
            f"Grid {grid}mm should be rounded to 0.05mm increments"
        )

    def test_grid_minimum_value(self):
        """Grid should have a minimum value of 0.05mm."""
        grid, _, _, _, _ = _get_routing_params("jlcpcb")

        assert grid >= 0.05, "Grid should be at least 0.05mm"

    def test_unknown_manufacturer_uses_safe_defaults(self):
        """Unknown manufacturer should use safe fallback defaults."""
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params(
            "unknown_mfr_xyz"
        )

        # Fallback defaults should still be DRC-compatible
        assert grid <= clearance / 2, "Fallback grid should be <= clearance/2"
        # Grid is auto-calculated from clearance: 0.15/2 = 0.075, rounded down to 0.05
        assert grid == pytest.approx(0.05, rel=0.01)
        assert clearance == pytest.approx(0.15, rel=0.01)

    def test_via_diameter_greater_than_drill(self):
        """Via diameter should always be greater than via drill."""
        for mfr in ["jlcpcb", "seeed", "pcbway", "oshpark"]:
            _, _, _, via_drill, via_diameter = _get_routing_params(mfr)
            assert via_diameter > via_drill, (
                f"{mfr}: via_diameter ({via_diameter}) should be > via_drill ({via_drill})"
            )

    def test_trace_width_reasonable(self):
        """Trace width should be within reasonable bounds."""
        for mfr in ["jlcpcb", "seeed", "pcbway", "oshpark"]:
            _, _, trace_width, _, _ = _get_routing_params(mfr)
            assert 0.1 <= trace_width <= 0.5, (
                f"{mfr}: trace_width ({trace_width}) should be between 0.1mm and 0.5mm"
            )


class TestParseDimensionMm:
    """Tests for _parse_dimension_mm function."""

    def test_parse_with_mm_suffix(self):
        """Should parse values with mm suffix."""
        assert _parse_dimension_mm("0.3mm") == pytest.approx(0.3)
        assert _parse_dimension_mm("0.2mm") == pytest.approx(0.2)
        assert _parse_dimension_mm("1.5mm") == pytest.approx(1.5)

    def test_parse_with_mm_suffix_space(self):
        """Should parse values with space before mm."""
        assert _parse_dimension_mm("0.3 mm") == pytest.approx(0.3)
        assert _parse_dimension_mm("0.2 mm") == pytest.approx(0.2)

    def test_parse_without_suffix(self):
        """Should parse plain numeric values."""
        assert _parse_dimension_mm("0.3") == pytest.approx(0.3)
        assert _parse_dimension_mm("0.15") == pytest.approx(0.15)

    def test_parse_with_whitespace(self):
        """Should handle leading/trailing whitespace."""
        assert _parse_dimension_mm("  0.3mm  ") == pytest.approx(0.3)
        assert _parse_dimension_mm(" 0.2 ") == pytest.approx(0.2)

    def test_parse_case_insensitive(self):
        """Should handle MM in any case."""
        assert _parse_dimension_mm("0.3MM") == pytest.approx(0.3)
        assert _parse_dimension_mm("0.3Mm") == pytest.approx(0.3)

    def test_parse_none(self):
        """Should return None for None input."""
        assert _parse_dimension_mm(None) is None

    def test_parse_empty(self):
        """Should return None for empty string."""
        assert _parse_dimension_mm("") is None
        assert _parse_dimension_mm("   ") is None

    def test_parse_invalid(self):
        """Should return None for invalid values."""
        assert _parse_dimension_mm("abc") is None
        assert _parse_dimension_mm("mm") is None
        assert _parse_dimension_mm("0.3inches") is None


class TestGetRoutingParamsWithSpec:
    """Tests for _get_routing_params with project spec (issue #726)."""

    def _make_spec(
        self,
        min_trace: str | None = None,
        min_space: str | None = None,
        min_via: str | None = None,
        min_drill: str | None = None,
    ) -> ProjectSpec:
        """Helper to create a ProjectSpec with manufacturing requirements."""
        return ProjectSpec(
            project=ProjectMetadata(name="Test Project"),
            requirements=Requirements(
                manufacturing=ManufacturingRequirements(
                    min_trace=min_trace,
                    min_space=min_space,
                    min_via=min_via,
                    min_drill=min_drill,
                )
            ),
        )

    def test_spec_min_space_overrides_clearance(self):
        """min_space from spec should override manufacturer clearance (issue #726)."""
        spec = self._make_spec(min_space="0.2mm")
        grid, clearance, _, _, _ = _get_routing_params("jlcpcb", spec)

        assert clearance == pytest.approx(0.2), "Spec min_space should set clearance"
        # Grid should be auto-calculated from the spec clearance
        assert grid <= clearance / 2, "Grid should be compatible with spec clearance"

    def test_spec_min_trace_overrides_trace_width(self):
        """min_trace from spec should override manufacturer trace width."""
        spec = self._make_spec(min_trace="0.3mm")
        _, _, trace_width, _, _ = _get_routing_params("jlcpcb", spec)

        assert trace_width == pytest.approx(0.3), "Spec min_trace should set trace_width"

    def test_spec_min_via_overrides_via_diameter(self):
        """min_via from spec should override manufacturer via diameter."""
        spec = self._make_spec(min_via="0.5mm")
        _, _, _, _, via_diameter = _get_routing_params("jlcpcb", spec)

        assert via_diameter == pytest.approx(0.5), "Spec min_via should set via_diameter"

    def test_spec_min_drill_overrides_via_drill(self):
        """min_drill from spec should override manufacturer via drill."""
        spec = self._make_spec(min_drill="0.25mm")
        _, _, _, via_drill, _ = _get_routing_params("jlcpcb", spec)

        assert via_drill == pytest.approx(0.25), "Spec min_drill should set via_drill"

    def test_partial_spec_uses_mfr_defaults(self):
        """Missing spec values should fall back to manufacturer defaults."""
        spec = self._make_spec(min_space="0.2mm")  # Only clearance specified
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params("jlcpcb", spec)

        # Clearance from spec
        assert clearance == pytest.approx(0.2)

        # Other values should come from JLCPCB defaults (non-zero, reasonable)
        assert trace_width > 0
        assert via_drill > 0
        assert via_diameter > via_drill

    def test_no_spec_uses_mfr_defaults(self):
        """None spec should use manufacturer defaults entirely."""
        grid1, clearance1, _, _, _ = _get_routing_params("jlcpcb", None)
        grid2, clearance2, _, _, _ = _get_routing_params("jlcpcb")

        # Should be the same as calling without spec
        assert grid1 == grid2
        assert clearance1 == clearance2

    def test_grid_auto_calculated_from_spec_clearance(self):
        """Grid should be auto-calculated from spec clearance value."""
        spec = self._make_spec(min_space="0.2mm")
        grid, clearance, _, _, _ = _get_routing_params("jlcpcb", spec)

        # Grid must be <= clearance / 2 for DRC compliance
        assert grid <= clearance / 2, f"Grid {grid}mm must be <= clearance/2 ({clearance / 2}mm)"
        # Grid should be rounded to 0.05mm increments
        assert grid * 20 == pytest.approx(round(grid * 20), abs=0.001)

    def test_voltage_divider_project_values(self):
        """Test with values from the voltage-divider project.kct (issue #726 example)."""
        # From boards/01-voltage-divider/project.kct:
        #   min_trace: "0.3mm"
        #   min_space: "0.2mm"
        spec = self._make_spec(min_trace="0.3mm", min_space="0.2mm")
        grid, clearance, trace_width, _, _ = _get_routing_params("jlcpcb", spec)

        assert clearance == pytest.approx(0.2), "Should use 0.2mm from spec"
        assert trace_width == pytest.approx(0.3), "Should use 0.3mm from spec"
        # Grid for 0.2mm clearance: 0.2/2 = 0.1mm, rounded down to 0.1mm
        assert grid == pytest.approx(0.1), "Grid should be 0.1mm for 0.2mm clearance"
        assert grid <= clearance / 2, "Grid should be DRC-compatible"
