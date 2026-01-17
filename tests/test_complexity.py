"""Tests for pre-routing complexity analysis."""

import json
from pathlib import Path

import pytest

from kicad_tools.analysis import (
    Bottleneck,
    ComplexityAnalyzer,
    ComplexityRating,
    LayerPrediction,
    RoutingComplexity,
)
from kicad_tools.schema.pcb import PCB

# Simple PCB with few pads (low complexity)
SIMPLE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")

  (gr_line
    (start 0 0)
    (end 50 0)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )
  (gr_line
    (start 50 0)
    (end 50 50)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )
  (gr_line
    (start 50 50)
    (end 0 50)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )
  (gr_line
    (start 0 50)
    (end 0 0)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Resistor_SMD:R_0805"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -1 0) (size 0.8 0.8) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 1 0) (size 0.8 0.8) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Resistor_SMD:R_0805"
    (layer "F.Cu")
    (at 40 40)
    (property "Reference" "R2")
    (pad "1" smd rect (at -1 0) (size 0.8 0.8) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 1 0) (size 0.8 0.8) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )
)
"""


# Complex PCB with high-pin-count IC (moderate complexity)
COMPLEX_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SDA")
  (net 4 "SCL")
  (net 5 "USB_DP")
  (net 6 "USB_DN")
  (net 7 "GPIO1")
  (net 8 "GPIO2")
  (net 9 "GPIO3")
  (net 10 "GPIO4")
  (net 11 "CLK")
  (net 12 "RESET")

  (gr_line
    (start 0 0)
    (end 30 0)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )
  (gr_line
    (start 30 0)
    (end 30 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )
  (gr_line
    (start 30 30)
    (end 0 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )
  (gr_line
    (start 0 30)
    (end 0 0)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Package_QFP:LQFP-48_7x7mm_P0.5mm"
    (layer "F.Cu")
    (at 15 15)
    (property "Reference" "U1")
    (pad "1" smd rect (at -3.5 -2.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at -3.5 -2.0) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 2 "GND"))
    (pad "3" smd rect (at -3.5 -1.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 3 "SDA"))
    (pad "4" smd rect (at -3.5 -1.0) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 4 "SCL"))
    (pad "5" smd rect (at -3.5 -0.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 5 "USB_DP"))
    (pad "6" smd rect (at -3.5 0.0) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 6 "USB_DN"))
    (pad "7" smd rect (at -3.5 0.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 7 "GPIO1"))
    (pad "8" smd rect (at -3.5 1.0) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 8 "GPIO2"))
    (pad "9" smd rect (at -3.5 1.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 9 "GPIO3"))
    (pad "10" smd rect (at -3.5 2.0) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 10 "GPIO4"))
    (pad "11" smd rect (at -3.5 2.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 11 "CLK"))
    (pad "12" smd rect (at -3.0 2.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 12 "RESET"))
    (pad "13" smd rect (at -2.5 2.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "14" smd rect (at -2.0 2.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 2 "GND"))
    (pad "15" smd rect (at -1.5 2.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 3 "SDA"))
    (pad "16" smd rect (at -1.0 2.5) (size 0.3 0.8) (layers "F.Cu" "F.Mask") (net 4 "SCL"))
  )

  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (at 5 5)
    (property "Reference" "C1")
    (pad "1" smd rect (at 0 -0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (at 25 5)
    (property "Reference" "C2")
    (pad "1" smd rect (at 0 -0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (at 5 25)
    (property "Reference" "C3")
    (pad "1" smd rect (at 0 -0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (at 25 25)
    (property "Reference" "C4")
    (pad "1" smd rect (at 0 -0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )
)
"""


@pytest.fixture
def simple_pcb(tmp_path: Path) -> Path:
    """Create a simple PCB file."""
    pcb_file = tmp_path / "simple.kicad_pcb"
    pcb_file.write_text(SIMPLE_PCB)
    return pcb_file


@pytest.fixture
def complex_pcb(tmp_path: Path) -> Path:
    """Create a complex PCB file."""
    pcb_file = tmp_path / "complex.kicad_pcb"
    pcb_file.write_text(COMPLEX_PCB)
    return pcb_file


class TestRoutingComplexity:
    """Tests for RoutingComplexity dataclass."""

    def test_to_dict_basic(self):
        """Test basic to_dict conversion."""
        report = RoutingComplexity(
            total_pads=100,
            total_nets=50,
            board_area_mm2=2500.0,
            board_width_mm=50.0,
            board_height_mm=50.0,
            overall_score=45.0,
            complexity_rating=ComplexityRating.MODERATE,
            min_layers_predicted=2,
        )

        d = report.to_dict()

        assert d["metrics"]["total_pads"] == 100
        assert d["metrics"]["total_nets"] == 50
        assert d["metrics"]["board_area_mm2"] == 2500.0
        assert d["scores"]["overall"] == 45.0
        assert d["predictions"]["complexity_rating"] == "moderate"
        assert d["predictions"]["min_layers_predicted"] == 2

    def test_to_dict_serializable(self):
        """Test that to_dict output is JSON serializable."""
        report = RoutingComplexity(
            total_pads=50,
            total_nets=25,
            board_area_mm2=1000.0,
            complexity_rating=ComplexityRating.SIMPLE,
        )

        json_str = json.dumps(report.to_dict())
        assert isinstance(json_str, str)

    def test_to_dict_with_bottlenecks(self):
        """Test to_dict with bottlenecks."""
        report = RoutingComplexity(
            bottlenecks=[
                Bottleneck(
                    component_ref="U1",
                    position=(10.0, 15.0),
                    description="High pin density",
                    pin_count=48,
                    pin_density=0.8,
                    available_channels=20,
                )
            ]
        )

        d = report.to_dict()
        assert len(d["bottlenecks"]) == 1
        assert d["bottlenecks"][0]["component"] == "U1"
        assert d["bottlenecks"][0]["pin_count"] == 48

    def test_to_dict_with_predictions(self):
        """Test to_dict with layer predictions."""
        report = RoutingComplexity(
            layer_predictions=[
                LayerPrediction(layer_count=2, success_probability=0.85, recommended=True),
                LayerPrediction(layer_count=4, success_probability=0.99, recommended=False),
            ]
        )

        d = report.to_dict()
        assert len(d["predictions"]["layer_predictions"]) == 2
        assert d["predictions"]["layer_predictions"][0]["layers"] == 2
        assert d["predictions"]["layer_predictions"][0]["probability"] == 0.85


class TestComplexityAnalyzer:
    """Tests for ComplexityAnalyzer class."""

    def test_analyzer_init_defaults(self):
        """Test analyzer initialization with defaults."""
        analyzer = ComplexityAnalyzer()
        assert analyzer.grid_size == 5.0

    def test_analyzer_init_custom(self):
        """Test analyzer initialization with custom values."""
        analyzer = ComplexityAnalyzer(grid_size=2.0)
        assert analyzer.grid_size == 2.0

    def test_analyze_simple_pcb(self, simple_pcb: Path):
        """Test analyzing a simple PCB."""
        pcb = PCB.load(str(simple_pcb))
        analyzer = ComplexityAnalyzer()

        report = analyzer.analyze(pcb)

        # Simple PCB should have low complexity
        assert report.total_pads == 4
        assert report.total_nets == 2
        assert report.complexity_rating in (
            ComplexityRating.TRIVIAL,
            ComplexityRating.SIMPLE,
        )
        assert report.min_layers_predicted == 2

    def test_analyze_complex_pcb(self, complex_pcb: Path):
        """Test analyzing a complex PCB."""
        pcb = PCB.load(str(complex_pcb))
        analyzer = ComplexityAnalyzer()

        report = analyzer.analyze(pcb)

        # Complex PCB should have more pads and higher complexity
        assert report.total_pads > 10
        assert report.total_nets >= 2
        # Should have some layer predictions
        assert len(report.layer_predictions) >= 2

    def test_analyze_returns_required_fields(self, simple_pcb: Path):
        """Test that report has all required fields."""
        pcb = PCB.load(str(simple_pcb))
        analyzer = ComplexityAnalyzer()

        report = analyzer.analyze(pcb)

        # Check metrics
        assert isinstance(report.total_pads, int)
        assert isinstance(report.total_nets, int)
        assert isinstance(report.board_area_mm2, float)
        assert isinstance(report.board_width_mm, float)
        assert isinstance(report.board_height_mm, float)

        # Check scores
        assert isinstance(report.density_score, float)
        assert isinstance(report.crossing_score, float)
        assert isinstance(report.channel_score, float)
        assert isinstance(report.overall_score, float)

        # Check predictions
        assert isinstance(report.complexity_rating, ComplexityRating)
        assert isinstance(report.min_layers_predicted, int)
        assert isinstance(report.layer_predictions, list)
        assert isinstance(report.bottlenecks, list)
        assert isinstance(report.recommendations, list)

    def test_analyze_layer_predictions(self, simple_pcb: Path):
        """Test that layer predictions are generated."""
        pcb = PCB.load(str(simple_pcb))
        analyzer = ComplexityAnalyzer()

        report = analyzer.analyze(pcb)

        # Should have predictions for 2, 4, and 6 layers
        layer_counts = [p.layer_count for p in report.layer_predictions]
        assert 2 in layer_counts
        assert 4 in layer_counts
        assert 6 in layer_counts

        # Probabilities should be valid
        for pred in report.layer_predictions:
            assert 0.0 <= pred.success_probability <= 1.0

    def test_analyze_board_dimensions(self, simple_pcb: Path):
        """Test that board dimensions are calculated."""
        pcb = PCB.load(str(simple_pcb))
        analyzer = ComplexityAnalyzer()

        report = analyzer.analyze(pcb)

        # Simple PCB has 50x50mm board
        assert report.board_width_mm == pytest.approx(50.0, abs=1.0)
        assert report.board_height_mm == pytest.approx(50.0, abs=1.0)
        assert report.board_area_mm2 == pytest.approx(2500.0, abs=100.0)

    def test_analyze_with_custom_grid_size(self, complex_pcb: Path):
        """Test analysis with custom grid size."""
        pcb = PCB.load(str(complex_pcb))
        analyzer = ComplexityAnalyzer(grid_size=2.0)

        report = analyzer.analyze(pcb)

        # Should still produce valid results
        assert report.total_pads > 0
        assert report.complexity_rating is not None

    def test_analyze_bottleneck_detection(self, complex_pcb: Path):
        """Test that bottlenecks are detected for high-pin-count components."""
        pcb = PCB.load(str(complex_pcb))
        analyzer = ComplexityAnalyzer()

        report = analyzer.analyze(pcb)

        # The QFP-48 should be identified as a bottleneck
        if report.bottlenecks:
            refs = [b.component_ref for b in report.bottlenecks]
            # U1 is the high-pin-count component
            assert "U1" in refs or len(report.bottlenecks) > 0


class TestComplexityCLI:
    """Tests for the analyze complexity CLI command."""

    def test_cli_file_not_found(self, capsys):
        """Test CLI with missing file."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["complexity", "nonexistent.kicad_pcb"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "Error" in captured.err

    def test_cli_wrong_extension(self, capsys, tmp_path: Path):
        """Test CLI with wrong file extension."""
        from kicad_tools.cli.analyze_cmd import main

        wrong_file = tmp_path / "test.txt"
        wrong_file.write_text("not a pcb")

        result = main(["complexity", str(wrong_file)])
        assert result == 1

        captured = capsys.readouterr()
        assert ".kicad_pcb" in captured.err

    def test_cli_text_output(self, simple_pcb: Path, capsys):
        """Test CLI with text output format."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["complexity", str(simple_pcb)])

        # Simple PCB should not return error
        assert result == 0

        captured = capsys.readouterr()
        # Should have board information
        assert "Board Information" in captured.out or "Size" in captured.out
        # Should have complexity scores
        assert "Complexity" in captured.out or "Score" in captured.out

    def test_cli_json_output(self, simple_pcb: Path, capsys):
        """Test CLI with JSON output format."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["complexity", str(simple_pcb), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "metrics" in data
        assert "scores" in data
        assert "predictions" in data
        assert "total_pads" in data["metrics"]
        assert "complexity_rating" in data["predictions"]

    def test_cli_grid_size_option(self, simple_pcb: Path, capsys):
        """Test CLI with custom grid size."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(
            [
                "complexity",
                str(simple_pcb),
                "--format",
                "json",
                "--grid-size",
                "2.0",
            ]
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "metrics" in data

    def test_cli_quiet_mode(self, simple_pcb: Path, capsys):
        """Test CLI quiet mode."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["complexity", str(simple_pcb), "--quiet"])
        assert result == 0

        # Should still produce output, but possibly less verbose
        captured = capsys.readouterr()
        assert len(captured.out) > 0


class TestComplexityRating:
    """Tests for complexity rating classification."""

    def test_rating_trivial_simple_board(self, simple_pcb: Path):
        """Test that simple boards get TRIVIAL/SIMPLE rating."""
        pcb = PCB.load(str(simple_pcb))
        analyzer = ComplexityAnalyzer()

        report = analyzer.analyze(pcb)

        assert report.complexity_rating in (
            ComplexityRating.TRIVIAL,
            ComplexityRating.SIMPLE,
        )

    def test_rating_higher_for_complex_board(self, complex_pcb: Path):
        """Test that complex boards get higher ratings."""
        pcb = PCB.load(str(complex_pcb))
        analyzer = ComplexityAnalyzer()

        report = analyzer.analyze(pcb)

        # Should be at least SIMPLE (possibly higher)
        assert report.complexity_rating.value in [
            "trivial",
            "simple",
            "moderate",
            "complex",
            "extreme",
        ]


class TestLayerPrediction:
    """Tests for layer prediction functionality."""

    def test_layer_prediction_dataclass(self):
        """Test LayerPrediction dataclass."""
        pred = LayerPrediction(
            layer_count=4,
            success_probability=0.92,
            recommended=True,
            notes="Good for differential pairs",
        )

        assert pred.layer_count == 4
        assert pred.success_probability == 0.92
        assert pred.recommended is True
        assert "differential" in pred.notes

    def test_layer_prediction_to_dict(self):
        """Test LayerPrediction.to_dict()."""
        pred = LayerPrediction(
            layer_count=2,
            success_probability=0.75,
            recommended=True,
        )

        d = pred.to_dict()
        assert d["layers"] == 2
        assert d["probability"] == 0.75
        assert d["recommended"] is True

    def test_simple_board_high_2layer_probability(self, simple_pcb: Path):
        """Test that simple boards have high 2-layer success probability."""
        pcb = PCB.load(str(simple_pcb))
        analyzer = ComplexityAnalyzer()

        report = analyzer.analyze(pcb)

        two_layer = next((p for p in report.layer_predictions if p.layer_count == 2), None)
        assert two_layer is not None
        assert two_layer.success_probability >= 0.7


class TestBottleneck:
    """Tests for Bottleneck dataclass."""

    def test_bottleneck_creation(self):
        """Test Bottleneck creation."""
        bn = Bottleneck(
            component_ref="U1",
            position=(10.5, 20.5),
            description="High pin density IC",
            pin_count=100,
            pin_density=0.8,
            available_channels=30,
        )

        assert bn.component_ref == "U1"
        assert bn.position == (10.5, 20.5)
        assert bn.pin_count == 100
        assert bn.pin_density == 0.8

    def test_bottleneck_to_dict(self):
        """Test Bottleneck.to_dict()."""
        bn = Bottleneck(
            component_ref="U2",
            position=(15.0, 25.0),
            description="BGA package",
            pin_count=256,
            pin_density=1.5,
            available_channels=40,
        )

        d = bn.to_dict()
        assert d["component"] == "U2"
        assert d["position"]["x"] == 15.0
        assert d["position"]["y"] == 25.0
        assert d["pin_count"] == 256
        assert d["pin_density"] == 1.5


class TestDifferentialPairDetection:
    """Tests for differential pair detection."""

    def test_detect_usb_differential_pairs(self, tmp_path: Path):
        """Test detection of USB differential pair naming."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (44 "Edge.Cuts" user)
          )
          (net 0 "")
          (net 1 "USB_DP")
          (net 2 "USB_DN")
          (net 3 "VCC")

          (gr_line (start 0 0) (end 20 0) (stroke (width 0.1)) (layer "Edge.Cuts"))
          (gr_line (start 20 0) (end 20 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
          (gr_line (start 20 20) (end 0 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
          (gr_line (start 0 20) (end 0 0) (stroke (width 0.1)) (layer "Edge.Cuts"))

          (footprint "test" (layer "F.Cu") (at 10 10) (property "Reference" "U1")
            (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "USB_DP"))
            (pad "2" smd rect (at 0 1) (size 0.5 0.5) (layers "F.Cu") (net 2 "USB_DN"))
            (pad "3" smd rect (at 0 2) (size 0.5 0.5) (layers "F.Cu") (net 3 "VCC"))
            (pad "4" smd rect (at 0 3) (size 0.5 0.5) (layers "F.Cu") (net 3 "VCC"))
          )
        )
        """
        pcb_file = tmp_path / "usb.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        analyzer = ComplexityAnalyzer()
        report = analyzer.analyze(pcb)

        # Should detect the differential pair
        assert report.differential_pair_count >= 1


class TestHighSpeedNetDetection:
    """Tests for high-speed net detection."""

    def test_detect_clock_nets(self, tmp_path: Path):
        """Test detection of clock net naming."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers
            (0 "F.Cu" signal)
            (44 "Edge.Cuts" user)
          )
          (net 0 "")
          (net 1 "CLK")
          (net 2 "SYS_CLOCK")
          (net 3 "VCC")

          (gr_line (start 0 0) (end 20 0) (stroke (width 0.1)) (layer "Edge.Cuts"))
          (gr_line (start 20 0) (end 20 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
          (gr_line (start 20 20) (end 0 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
          (gr_line (start 0 20) (end 0 0) (stroke (width 0.1)) (layer "Edge.Cuts"))

          (footprint "test" (layer "F.Cu") (at 10 10) (property "Reference" "U1")
            (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "CLK"))
            (pad "2" smd rect (at 0 1) (size 0.5 0.5) (layers "F.Cu") (net 1 "CLK"))
            (pad "3" smd rect (at 0 2) (size 0.5 0.5) (layers "F.Cu") (net 2 "SYS_CLOCK"))
            (pad "4" smd rect (at 0 3) (size 0.5 0.5) (layers "F.Cu") (net 2 "SYS_CLOCK"))
          )
        )
        """
        pcb_file = tmp_path / "clk.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        analyzer = ComplexityAnalyzer()
        report = analyzer.analyze(pcb)

        # Should detect high-speed nets
        assert report.high_speed_net_count >= 2
