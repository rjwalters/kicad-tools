"""Tests for build command routing parameter auto-calculation.

Verifies that the grid is auto-calculated from clearance to prevent
DRC violations when routing (issue #723).
"""

import pytest

from kicad_tools.cli.build_cmd import _get_routing_params


class TestGetRoutingParams:
    """Tests for _get_routing_params function."""

    def test_jlcpcb_grid_compatible_with_clearance(self):
        """JLCPCB grid should be compatible with its clearance (issue #723)."""
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params("jlcpcb")

        # Grid must be <= clearance / 2 to allow routing without DRC violations
        assert grid <= clearance / 2, (
            f"Grid {grid}mm must be <= clearance/2 ({clearance/2}mm) "
            f"to prevent DRC violations"
        )

        # Verify we got JLCPCB-specific values (~5 mil = 0.127mm clearance)
        assert clearance > 0.1  # JLCPCB has reasonable clearance
        assert trace_width > 0
        assert via_drill > 0
        assert via_diameter > via_drill

    def test_seeed_grid_compatible_with_clearance(self):
        """Seeed grid should be compatible with its clearance."""
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params("seeed")

        assert grid <= clearance / 2, (
            f"Grid {grid}mm must be <= clearance/2 ({clearance/2}mm)"
        )

    def test_pcbway_grid_compatible_with_clearance(self):
        """PCBWay grid should be compatible with its clearance."""
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params("pcbway")

        assert grid <= clearance / 2, (
            f"Grid {grid}mm must be <= clearance/2 ({clearance/2}mm)"
        )

    def test_oshpark_grid_compatible_with_clearance(self):
        """OSH Park grid should be compatible with its clearance."""
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params("oshpark")

        assert grid <= clearance / 2, (
            f"Grid {grid}mm must be <= clearance/2 ({clearance/2}mm)"
        )

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
        assert grid == pytest.approx(0.075, rel=0.01)
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
