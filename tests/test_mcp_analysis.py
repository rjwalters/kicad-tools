"""Tests for kicad_tools.mcp.tools.analysis module."""

import json
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.mcp.tools.analysis import analyze_board
from kicad_tools.mcp.types import (
    BoardAnalysis,
    BoardDimensions,
    ComponentSummary,
    LayerInfo,
    NetFanout,
    NetSummary,
    RoutingStatus,
    ZoneInfo,
)

# Simple 2-layer PCB with SMD components
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


# 4-layer PCB with internal planes
MULTILAYER_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" power)
    (2 "In2.Cu" power)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "+3.3V")
  (net 2 "GND")

  (gr_line (start 0 0) (end 100 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 100 0) (end 100 80) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 100 80) (end 0 80) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 80) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "QFP-32"
    (layer "F.Cu")
    (at 50 40)
    (attr smd)
    (property "Reference" "U1")
    (pad "1" smd rect (at -5 -5) (size 0.5 1.5) (layers "F.Cu") (net 1 "+3.3V"))
    (pad "2" smd rect (at -5 -3) (size 0.5 1.5) (layers "F.Cu") (net 2 "GND"))
    (pad "3" smd rect (at -5 -1) (size 0.5 1.5) (layers "F.Cu") (net 2 "GND"))
    (pad "4" smd rect (at -5 1) (size 0.5 1.5) (layers "F.Cu") (net 2 "GND"))
  )

  (zone (net 2) (net_name "GND") (layer "In1.Cu")
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 0 0) (xy 100 0) (xy 100 80) (xy 0 80)))
  )
)
"""


# PCB with through-hole components
THROUGH_HOLE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VIN")
  (net 2 "VOUT")

  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 30 0) (end 30 30) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 30 30) (end 0 30) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 30) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "DIP-8"
    (layer "F.Cu")
    (at 15 15)
    (attr through_hole)
    (property "Reference" "U1")
    (pad "1" thru_hole oval (at -3.81 -3.81) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 1 "VIN"))
    (pad "2" thru_hole oval (at -3.81 -1.27) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 2 "VOUT"))
    (pad "3" thru_hole oval (at -3.81 1.27) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 0 ""))
    (pad "4" thru_hole oval (at -3.81 3.81) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 0 ""))
    (pad "5" thru_hole oval (at 3.81 3.81) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 0 ""))
    (pad "6" thru_hole oval (at 3.81 1.27) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 0 ""))
    (pad "7" thru_hole oval (at 3.81 -1.27) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 0 ""))
    (pad "8" thru_hole oval (at 3.81 -3.81) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 0 ""))
  )
)
"""


# PCB with vias
PCB_WITH_VIAS = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "NET1")

  (gr_line (start 0 0) (end 20 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 20 0) (end 20 20) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 20 20) (end 0 20) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))

  (footprint "R_0603"
    (layer "F.Cu")
    (at 5 10)
    (attr smd)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "NET1"))
  )

  (footprint "R_0603"
    (layer "B.Cu")
    (at 15 10)
    (attr smd)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "B.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "B.Cu") (net 1 "NET1"))
  )

  (segment (start 5.5 10) (end 10 10) (width 0.25) (layer "F.Cu") (net 1))
  (via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (segment (start 10 10) (end 14.5 10) (width 0.25) (layer "B.Cu") (net 1))
)
"""


def write_temp_pcb(content: str) -> str:
    """Write PCB content to a temporary file and return the path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".kicad_pcb", delete=False) as f:
        f.write(content)
        return f.name


class TestAnalyzeBoardBasic:
    """Basic functionality tests for analyze_board."""

    def test_analyze_simple_pcb(self):
        """Test analyzing a simple 2-layer PCB."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = analyze_board(pcb_path)

            assert isinstance(result, BoardAnalysis)
            assert result.file_path == pcb_path
        finally:
            Path(pcb_path).unlink()

    def test_file_not_found_error(self):
        """Test that FileNotFoundError is raised for missing files."""
        with pytest.raises(KiCadFileNotFoundError):
            analyze_board("/nonexistent/path/to/board.kicad_pcb")

    def test_invalid_file_extension(self):
        """Test that ParseError is raised for invalid file extensions."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"not a pcb file")
            path = f.name
        try:
            with pytest.raises(ParseError):
                analyze_board(path)
        finally:
            Path(path).unlink()

    def test_to_dict_serialization(self):
        """Test that BoardAnalysis can be serialized to dict/JSON."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = analyze_board(pcb_path)
            data = result.to_dict()

            # Verify it's JSON-serializable
            json_str = json.dumps(data)
            assert json_str is not None

            # Verify structure
            assert "file_path" in data
            assert "board_dimensions" in data
            assert "layers" in data
            assert "components" in data
            assert "nets" in data
            assert "zones" in data
            assert "routing_status" in data
        finally:
            Path(pcb_path).unlink()


class TestBoardDimensions:
    """Tests for board dimension extraction."""

    def test_rectangular_outline(self):
        """Test extraction of rectangular board outline."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = analyze_board(pcb_path)
            dims = result.board_dimensions

            assert dims.width_mm == pytest.approx(50.0, rel=0.01)
            assert dims.height_mm == pytest.approx(40.0, rel=0.01)
            assert dims.area_mm2 == pytest.approx(2000.0, rel=0.01)
            assert dims.outline_type == "rectangle"
        finally:
            Path(pcb_path).unlink()

    def test_larger_board_dimensions(self):
        """Test extraction of larger board dimensions."""
        pcb_path = write_temp_pcb(MULTILAYER_PCB)
        try:
            result = analyze_board(pcb_path)
            dims = result.board_dimensions

            assert dims.width_mm == pytest.approx(100.0, rel=0.01)
            assert dims.height_mm == pytest.approx(80.0, rel=0.01)
        finally:
            Path(pcb_path).unlink()


class TestLayerInfo:
    """Tests for layer information extraction."""

    def test_two_layer_board(self):
        """Test extraction of 2-layer board info."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = analyze_board(pcb_path)
            layers = result.layers

            assert layers.copper_layers == 2
            assert "F.Cu" in layers.layer_names
            assert "B.Cu" in layers.layer_names
            assert layers.has_internal_planes is False
        finally:
            Path(pcb_path).unlink()

    def test_four_layer_board_with_planes(self):
        """Test extraction of 4-layer board with internal planes."""
        pcb_path = write_temp_pcb(MULTILAYER_PCB)
        try:
            result = analyze_board(pcb_path)
            layers = result.layers

            assert layers.copper_layers == 4
            assert "F.Cu" in layers.layer_names
            assert "In1.Cu" in layers.layer_names
            assert "In2.Cu" in layers.layer_names
            assert "B.Cu" in layers.layer_names
            assert layers.has_internal_planes is True
        finally:
            Path(pcb_path).unlink()


class TestComponentSummary:
    """Tests for component summary extraction."""

    def test_smd_component_count(self):
        """Test counting of SMD components."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = analyze_board(pcb_path)
            comps = result.components

            assert comps.total_count == 2
            assert comps.smd_count == 2
            assert comps.through_hole_count == 0
        finally:
            Path(pcb_path).unlink()

    def test_through_hole_component_count(self):
        """Test counting of through-hole components."""
        pcb_path = write_temp_pcb(THROUGH_HOLE_PCB)
        try:
            result = analyze_board(pcb_path)
            comps = result.components

            assert comps.total_count == 1
            assert comps.through_hole_count == 1
            assert comps.smd_count == 0
        finally:
            Path(pcb_path).unlink()

    def test_component_type_classification(self):
        """Test classification of component types."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = analyze_board(pcb_path)
            comps = result.components

            assert "resistor" in comps.by_type
            assert comps.by_type["resistor"] == 1
            assert "capacitor" in comps.by_type
            assert comps.by_type["capacitor"] == 1
        finally:
            Path(pcb_path).unlink()


class TestNetSummary:
    """Tests for net summary extraction."""

    def test_net_count(self):
        """Test counting of nets."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = analyze_board(pcb_path)
            nets = result.nets

            # 3 nets: VCC, GND, SIG1 (net 0 is excluded)
            assert nets.total_nets == 3
        finally:
            Path(pcb_path).unlink()

    def test_power_net_identification(self):
        """Test identification of power nets."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = analyze_board(pcb_path)
            nets = result.nets

            assert "VCC" in nets.power_nets
            assert "GND" in nets.power_nets
        finally:
            Path(pcb_path).unlink()

    def test_voltage_net_identification(self):
        """Test identification of voltage-based power nets."""
        pcb_path = write_temp_pcb(MULTILAYER_PCB)
        try:
            result = analyze_board(pcb_path)
            nets = result.nets

            assert "+3.3V" in nets.power_nets
            assert "GND" in nets.power_nets
        finally:
            Path(pcb_path).unlink()


class TestZoneInfo:
    """Tests for zone extraction."""

    def test_zone_extraction(self):
        """Test extraction of copper zones."""
        pcb_path = write_temp_pcb(MULTILAYER_PCB)
        try:
            result = analyze_board(pcb_path)

            assert len(result.zones) == 1
            zone = result.zones[0]
            assert zone.net_name == "GND"
            assert zone.layer == "In1.Cu"
            assert zone.is_filled is True
        finally:
            Path(pcb_path).unlink()

    def test_no_zones(self):
        """Test PCB with no zones."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = analyze_board(pcb_path)
            assert len(result.zones) == 0
        finally:
            Path(pcb_path).unlink()


class TestRoutingStatus:
    """Tests for routing status extraction."""

    def test_via_count(self):
        """Test counting of vias."""
        pcb_path = write_temp_pcb(PCB_WITH_VIAS)
        try:
            result = analyze_board(pcb_path)
            status = result.routing_status

            assert status.via_count == 1
        finally:
            Path(pcb_path).unlink()

    def test_trace_length(self):
        """Test calculation of trace length."""
        pcb_path = write_temp_pcb(PCB_WITH_VIAS)
        try:
            result = analyze_board(pcb_path)
            status = result.routing_status

            # Two segments: 4.5mm each (5.5->10, 10->14.5)
            assert status.total_trace_length_mm > 0
        finally:
            Path(pcb_path).unlink()

    def test_routing_completion(self):
        """Test routing completion percentage."""
        pcb_path = write_temp_pcb(SIMPLE_PCB)
        try:
            result = analyze_board(pcb_path)
            status = result.routing_status

            # Should have some routing completion
            assert status.completion_percent >= 0
            assert status.completion_percent <= 100
        finally:
            Path(pcb_path).unlink()


class TestRealPCBFiles:
    """Tests using real KiCad PCB fixture files."""

    @pytest.fixture
    def test_project_pcb(self) -> str:
        """Path to test project PCB fixture."""
        return str(Path(__file__).parent / "fixtures" / "projects" / "test_project.kicad_pcb")

    @pytest.fixture
    def multilayer_zones_pcb(self) -> str:
        """Path to multilayer zones PCB fixture."""
        return str(Path(__file__).parent / "fixtures" / "projects" / "multilayer_zones.kicad_pcb")

    def test_analyze_test_project(self, test_project_pcb):
        """Test analyzing the test project PCB fixture."""
        if not Path(test_project_pcb).exists():
            pytest.skip("Test fixture not found")

        result = analyze_board(test_project_pcb)

        # Verify basic structure
        assert isinstance(result, BoardAnalysis)
        assert result.components.total_count > 0
        assert result.layers.copper_layers >= 2

    def test_analyze_multilayer_zones(self, multilayer_zones_pcb):
        """Test analyzing the multilayer zones PCB fixture."""
        if not Path(multilayer_zones_pcb).exists():
            pytest.skip("Test fixture not found")

        result = analyze_board(multilayer_zones_pcb)

        # Verify basic structure
        assert isinstance(result, BoardAnalysis)
        assert result.layers.copper_layers >= 2


class TestTypeDataclasses:
    """Tests for MCP type dataclasses."""

    def test_board_dimensions_to_dict(self):
        """Test BoardDimensions serialization."""
        dims = BoardDimensions(
            width_mm=50.123,
            height_mm=40.456,
            area_mm2=2000.789,
            outline_type="rectangle",
        )
        data = dims.to_dict()

        assert data["width_mm"] == 50.12
        assert data["height_mm"] == 40.46
        assert data["area_mm2"] == 2000.79
        assert data["outline_type"] == "rectangle"

    def test_layer_info_to_dict(self):
        """Test LayerInfo serialization."""
        layers = LayerInfo(
            copper_layers=4,
            layer_names=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
            has_internal_planes=True,
        )
        data = layers.to_dict()

        assert data["copper_layers"] == 4
        assert len(data["layer_names"]) == 4
        assert data["has_internal_planes"] is True

    def test_component_summary_to_dict(self):
        """Test ComponentSummary serialization."""
        comps = ComponentSummary(
            total_count=10,
            smd_count=8,
            through_hole_count=2,
            by_type={"resistor": 5, "capacitor": 3, "ic": 2},
            fixed_count=1,
            unplaced_count=0,
        )
        data = comps.to_dict()

        assert data["total_count"] == 10
        assert data["smd_count"] == 8
        assert data["through_hole_count"] == 2
        assert data["by_type"]["resistor"] == 5

    def test_net_fanout_to_dict(self):
        """Test NetFanout serialization."""
        fanout = NetFanout(net_name="GND", connection_count=50)
        data = fanout.to_dict()

        assert data["net_name"] == "GND"
        assert data["connection_count"] == 50

    def test_net_summary_to_dict(self):
        """Test NetSummary serialization."""
        summary = NetSummary(
            total_nets=50,
            routed_nets=45,
            unrouted_nets=5,
            power_nets=["VCC", "GND"],
            high_fanout_nets=[NetFanout("GND", 100), NetFanout("VCC", 50)],
        )
        data = summary.to_dict()

        assert data["total_nets"] == 50
        assert data["routed_nets"] == 45
        assert data["unrouted_nets"] == 5
        assert "VCC" in data["power_nets"]
        assert len(data["high_fanout_nets"]) == 2

    def test_zone_info_to_dict(self):
        """Test ZoneInfo serialization."""
        zone = ZoneInfo(
            net_name="GND",
            layer="In1.Cu",
            priority=0,
            is_filled=True,
        )
        data = zone.to_dict()

        assert data["net_name"] == "GND"
        assert data["layer"] == "In1.Cu"
        assert data["priority"] == 0
        assert data["is_filled"] is True

    def test_routing_status_to_dict(self):
        """Test RoutingStatus serialization."""
        status = RoutingStatus(
            completion_percent=95.567,
            total_airwires=3,
            total_trace_length_mm=1234.567,
            via_count=25,
        )
        data = status.to_dict()

        assert data["completion_percent"] == 95.6
        assert data["total_airwires"] == 3
        assert data["total_trace_length_mm"] == 1234.57
        assert data["via_count"] == 25
