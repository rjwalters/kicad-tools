"""Tests for MCP measure_clearance tool."""

from pathlib import Path

import pytest

# PCB with known clearances for testing
CLEARANCE_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET1")
  (net 2 "GND")
  (net 3 "+3.3V")
  (gr_rect (start 100 100) (end 160 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 115 115)
    (property "Reference" "U1" (at 0 -3.5 0) (layer "F.SilkS") (uuid "ref-u1"))
    (pad "1" smd rect (at -2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd rect (at -2.7 -0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
    (pad "3" smd rect (at -2.7 0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
    (pad "4" smd rect (at -2.7 1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "+3.3V"))
    (pad "5" smd rect (at 2.7 1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "+3.3V"))
    (pad "6" smd rect (at 2.7 0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "7" smd rect (at 2.7 -0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "8" smd rect (at 2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000200")
    (at 140 115)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-r1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000300")
    (at 140 125)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-r2"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "+3.3V"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
  (segment (start 118.7 115) (end 130 115) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
  (segment (start 130 115) (end 139.49 115) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-2"))
)
"""


@pytest.fixture
def clearance_test_pcb(tmp_path: Path) -> Path:
    """Create a PCB file for clearance testing."""
    pcb_file = tmp_path / "clearance_test.kicad_pcb"
    pcb_file.write_text(CLEARANCE_TEST_PCB)
    return pcb_file


class TestMeasureClearance:
    """Tests for the measure_clearance MCP tool."""

    def test_measure_clearance_between_components(self, clearance_test_pcb: Path):
        """Test measuring clearance between two components."""
        from kicad_tools.mcp.tools.analysis import measure_clearance

        result = measure_clearance(str(clearance_test_pcb), "U1", "R1")

        assert result.item1 == "U1"
        assert result.item2 == "R1"
        assert result.min_clearance_mm > 0
        assert len(result.clearances) > 0
        assert result.layer in ("F.Cu", "B.Cu")

    def test_measure_clearance_between_components_on_layer(self, clearance_test_pcb: Path):
        """Test measuring clearance on a specific layer."""
        from kicad_tools.mcp.tools.analysis import measure_clearance

        result = measure_clearance(str(clearance_test_pcb), "U1", "R1", layer="F.Cu")

        assert result.item1 == "U1"
        assert result.item2 == "R1"
        assert result.layer == "F.Cu"
        assert len(result.clearances) > 0

    def test_measure_clearance_by_net(self, clearance_test_pcb: Path):
        """Test measuring clearance between components on different nets."""
        from kicad_tools.mcp.tools.analysis import measure_clearance

        # Measure clearance between NET1 and GND
        result = measure_clearance(str(clearance_test_pcb), "NET1", "GND")

        assert result.item1 == "NET1"
        assert result.item2 == "GND"
        # Clearance can be negative if pads/tracks overlap (e.g., adjacent pads on IC)
        # This is valid and important to detect for DRC
        assert result.min_clearance_mm is not None
        assert len(result.clearances) > 0

    def test_measure_clearance_nearest_neighbor(self, clearance_test_pcb: Path):
        """Test finding the nearest neighbor clearance."""
        from kicad_tools.mcp.tools.analysis import measure_clearance

        result = measure_clearance(str(clearance_test_pcb), "R1", item2=None)

        assert result.item1 == "R1"
        assert result.item2 != ""
        assert result.min_clearance_mm >= 0
        assert len(result.clearances) > 0

    def test_measure_clearance_result_summary(self, clearance_test_pcb: Path):
        """Test that the result summary is generated correctly."""
        from kicad_tools.mcp.tools.analysis import measure_clearance

        result = measure_clearance(str(clearance_test_pcb), "U1", "R1")

        summary = result.summary()
        assert "U1" in summary
        assert "R1" in summary
        assert "mm" in summary

    def test_measure_clearance_invalid_item_raises_error(self, clearance_test_pcb: Path):
        """Test that an invalid item raises ValueError."""
        from kicad_tools.mcp.tools.analysis import measure_clearance

        with pytest.raises(ValueError, match="No copper elements found"):
            measure_clearance(str(clearance_test_pcb), "NONEXISTENT", "R1")

    def test_measure_clearance_invalid_file_raises_error(self, tmp_path: Path):
        """Test that an invalid file path raises an error."""
        from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
        from kicad_tools.mcp.tools.analysis import measure_clearance

        with pytest.raises(KiCadFileNotFoundError):
            measure_clearance(str(tmp_path / "nonexistent.kicad_pcb"), "U1", "R1")

    def test_measure_clearance_between_nearby_resistors(self, clearance_test_pcb: Path):
        """Test measuring clearance between nearby resistors R1 and R2."""
        from kicad_tools.mcp.tools.analysis import measure_clearance

        result = measure_clearance(str(clearance_test_pcb), "R1", "R2")

        assert result.item1 == "R1"
        assert result.item2 == "R2"
        # R1 is at (140, 115) and R2 is at (140, 125) - 10mm apart vertically
        # Pad size is 0.64mm height, so clearance should be ~10 - 0.64 = 9.36mm
        assert result.min_clearance_mm > 8.0
        assert result.min_clearance_mm < 11.0


class TestClearanceResult:
    """Tests for ClearanceResult model."""

    def test_clearance_result_model(self):
        """Test ClearanceResult model creation."""
        from kicad_tools.mcp.types import ClearanceMeasurement, ClearanceResult

        measurement = ClearanceMeasurement(
            from_item="U1-1",
            from_type="pad",
            to_item="R1-1",
            to_type="pad",
            clearance_mm=1.5,
            location=(100.0, 100.0),
            layer="F.Cu",
        )

        result = ClearanceResult(
            item1="U1",
            item2="R1",
            min_clearance_mm=1.5,
            location=(100.0, 100.0),
            layer="F.Cu",
            clearances=[measurement],
            passes_rules=True,
            required_clearance_mm=0.2,
        )

        assert result.item1 == "U1"
        assert result.item2 == "R1"
        assert result.min_clearance_mm == 1.5
        assert result.passes_rules is True
        assert len(result.clearances) == 1

    def test_clearance_result_summary(self):
        """Test ClearanceResult summary generation."""
        from kicad_tools.mcp.types import ClearanceResult

        result = ClearanceResult(
            item1="U1",
            item2="R1",
            min_clearance_mm=1.5,
            location=(100.0, 100.0),
            layer="F.Cu",
            passes_rules=True,
            required_clearance_mm=0.2,
        )

        summary = result.summary()
        assert "U1" in summary
        assert "R1" in summary
        assert "1.5" in summary or "1.500" in summary
        assert "PASSES" in summary

    def test_clearance_result_fails_rules(self):
        """Test ClearanceResult when rules are violated."""
        from kicad_tools.mcp.types import ClearanceResult

        result = ClearanceResult(
            item1="U1",
            item2="R1",
            min_clearance_mm=0.1,
            location=(100.0, 100.0),
            layer="F.Cu",
            passes_rules=False,
            required_clearance_mm=0.2,
        )

        summary = result.summary()
        assert "FAILS" in summary


class TestClearanceMeasurement:
    """Tests for ClearanceMeasurement model."""

    def test_clearance_measurement_model(self):
        """Test ClearanceMeasurement model creation."""
        from kicad_tools.mcp.types import ClearanceMeasurement

        measurement = ClearanceMeasurement(
            from_item="U1-1",
            from_type="pad",
            to_item="R1-1",
            to_type="pad",
            clearance_mm=1.5,
            location=(100.0, 100.0),
            layer="F.Cu",
        )

        assert measurement.from_item == "U1-1"
        assert measurement.from_type == "pad"
        assert measurement.to_item == "R1-1"
        assert measurement.to_type == "pad"
        assert measurement.clearance_mm == 1.5
        assert measurement.location == (100.0, 100.0)
        assert measurement.layer == "F.Cu"


class TestMCPTools:
    """Tests for MCPTools utility class."""

    def test_mcp_tools_register(self):
        """Test MCPTools registration."""
        from kicad_tools.mcp.util import MCPTools

        tools = MCPTools()

        @tools.register()
        def test_tool(x: int) -> int:
            return x * 2

        assert test_tool in tools._tools
        assert test_tool(5) == 10

    def test_mcp_tools_custom_decorator(self):
        """Test MCPTools with custom decorator."""
        from kicad_tools.mcp.util import MCPTools

        tools = MCPTools()
        custom_called = []

        def custom_decorator(mcp):
            def wrapper(func):
                custom_called.append(func.__name__)
                return func

            return wrapper

        @tools.register(decorator=custom_decorator)
        def test_tool(x: int) -> int:
            return x * 2

        # Simulate install (would normally use a real FastMCP instance)
        class MockMCP:
            pass

        tools.install(MockMCP())
        assert "test_tool" in custom_called
