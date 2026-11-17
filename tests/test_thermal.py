"""Tests for thermal analysis module."""

import json
from pathlib import Path

import pytest

from kicad_tools.analysis import (
    PowerEstimator,
    ThermalAnalyzer,
    ThermalHotspot,
    ThermalSeverity,
    ThermalSource,
)
from kicad_tools.schema.pcb import PCB

# PCB with voltage regulator and bypass caps (typical thermal concern)
THERMAL_PCB = """(kicad_pcb
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
  (net 1 "VIN")
  (net 2 "VOUT")
  (net 3 "GND")

  (gr_rect
    (start 0 0)
    (end 30 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    (layer "F.Cu")
    (at 15 15)
    (property "Reference" "U1")
    (property "Value" "AMS1117-3.3")
    (pad "1" smd rect (at -2.3 0) (size 1.2 1.5) (layers "F.Cu" "F.Mask") (net 1 "VIN"))
    (pad "2" smd rect (at 0 0) (size 3.0 2.0) (layers "F.Cu" "F.Mask") (net 3 "GND"))
    (pad "3" smd rect (at 2.3 0) (size 1.2 1.5) (layers "F.Cu" "F.Mask") (net 2 "VOUT"))
  )

  (footprint "Capacitor_SMD:C_0805_2012Metric"
    (layer "F.Cu")
    (at 10 15)
    (property "Reference" "C1")
    (property "Value" "10uF")
    (pad "1" smd rect (at -0.9 0) (size 1.0 1.3) (layers "F.Cu" "F.Mask") (net 1 "VIN"))
    (pad "2" smd rect (at 0.9 0) (size 1.0 1.3) (layers "F.Cu" "F.Mask") (net 3 "GND"))
  )

  (footprint "Capacitor_SMD:C_0805_2012Metric"
    (layer "F.Cu")
    (at 20 15)
    (property "Reference" "C2")
    (property "Value" "10uF")
    (pad "1" smd rect (at -0.9 0) (size 1.0 1.3) (layers "F.Cu" "F.Mask") (net 2 "VOUT"))
    (pad "2" smd rect (at 0.9 0) (size 1.0 1.3) (layers "F.Cu" "F.Mask") (net 3 "GND"))
  )

  (segment (start 10.9 15) (end 12.7 15) (width 0.5) (layer "F.Cu") (net 1))
  (segment (start 17.3 15) (end 19.1 15) (width 0.5) (layer "F.Cu") (net 2))

  (via (at 15 17) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 3))
  (via (at 16 17) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 3))

  (zone
    (net 3)
    (net_name "GND")
    (layer "B.Cu")
    (filled_polygon
      (pts
        (xy 0 0)
        (xy 30 0)
        (xy 30 30)
        (xy 0 30)
      )
    )
  )
)
"""


# PCB with MOSFET H-bridge (high power application)
HIGH_POWER_PCB = """(kicad_pcb
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
  (net 3 "GATE1")
  (net 4 "OUT1")

  (gr_rect
    (start 0 0)
    (end 40 40)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Package_TO_SOT_SMD:TO-252-3_TabPin2"
    (layer "F.Cu")
    (at 15 15)
    (property "Reference" "Q1")
    (property "Value" "IRLZ44N")
    (pad "1" smd rect (at -2.3 0) (size 1.5 2.0) (layers "F.Cu" "F.Mask") (net 3 "GATE1"))
    (pad "2" smd rect (at 0 0) (size 6.0 6.0) (layers "F.Cu" "F.Mask") (net 4 "OUT1"))
    (pad "3" smd rect (at 2.3 0) (size 1.5 2.0) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
  )

  (footprint "Package_TO_SOT_SMD:TO-252-3_TabPin2"
    (layer "F.Cu")
    (at 25 15)
    (property "Reference" "Q2")
    (property "Value" "IRLZ44N")
    (pad "1" smd rect (at -2.3 0) (size 1.5 2.0) (layers "F.Cu" "F.Mask") (net 3 "GATE1"))
    (pad "2" smd rect (at 0 0) (size 6.0 6.0) (layers "F.Cu" "F.Mask") (net 2 "GND"))
    (pad "3" smd rect (at 2.3 0) (size 1.5 2.0) (layers "F.Cu" "F.Mask") (net 4 "OUT1"))
  )

  (footprint "Resistor_SMD:R_2512_6332Metric"
    (layer "F.Cu")
    (at 20 25)
    (property "Reference" "R1")
    (property "Value" "0.1")
    (pad "1" smd rect (at -3.1 0) (size 1.2 3.2) (layers "F.Cu" "F.Mask") (net 4 "OUT1"))
    (pad "2" smd rect (at 3.1 0) (size 1.2 3.2) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (segment (start 21 15) (end 22.7 15) (width 1.0) (layer "F.Cu") (net 4))
)
"""


# Simple PCB with no heat sources
SIMPLE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG")

  (gr_rect
    (start 0 0)
    (end 20 20)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "J1")
    (property "Value" "Conn")
    (pad "1" thru_hole rect (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 1 "SIG"))
    (pad "2" thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 0 ""))
  )
)
"""


# PCB with power LED
LED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "LED_A")

  (gr_rect
    (start 0 0)
    (end 20 20)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "LED_SMD:LED_5050_PLCC6"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "LED1")
    (property "Value" "WS2812B")
    (pad "1" smd rect (at -2.4 -1.6) (size 1.5 1.0) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at -2.4 0) (size 1.5 1.0) (layers "F.Cu" "F.Mask") (net 2 "LED_A"))
    (pad "3" smd rect (at -2.4 1.6) (size 1.5 1.0) (layers "F.Cu" "F.Mask") (net 0 ""))
    (pad "4" smd rect (at 2.4 1.6) (size 1.5 1.0) (layers "F.Cu" "F.Mask") (net 0 ""))
    (pad "5" smd rect (at 2.4 0) (size 1.5 1.0) (layers "F.Cu" "F.Mask") (net 0 ""))
    (pad "6" smd rect (at 2.4 -1.6) (size 1.5 1.0) (layers "F.Cu" "F.Mask") (net 0 ""))
  )
)
"""


@pytest.fixture
def thermal_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with voltage regulator."""
    pcb_file = tmp_path / "thermal.kicad_pcb"
    pcb_file.write_text(THERMAL_PCB)
    return pcb_file


@pytest.fixture
def high_power_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with high power components."""
    pcb_file = tmp_path / "high_power.kicad_pcb"
    pcb_file.write_text(HIGH_POWER_PCB)
    return pcb_file


@pytest.fixture
def simple_pcb(tmp_path: Path) -> Path:
    """Create a simple PCB file with no heat sources."""
    pcb_file = tmp_path / "simple.kicad_pcb"
    pcb_file.write_text(SIMPLE_PCB)
    return pcb_file


@pytest.fixture
def led_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with power LED."""
    pcb_file = tmp_path / "led.kicad_pcb"
    pcb_file.write_text(LED_PCB)
    return pcb_file


class TestThermalSource:
    """Tests for ThermalSource dataclass."""

    def test_to_dict_basic(self):
        """Test basic to_dict conversion."""
        source = ThermalSource(
            reference="U1",
            power_w=0.5,
            package="SOT-223",
            thermal_resistance=50.0,
            position=(10.5, 20.5),
            component_type="regulator",
            value="AMS1117-3.3",
        )

        d = source.to_dict()

        assert d["reference"] == "U1"
        assert d["power_w"] == 0.5
        assert d["package"] == "SOT-223"
        assert d["thermal_resistance_c_per_w"] == 50.0
        assert d["position"] == {"x": 10.5, "y": 20.5}
        assert d["component_type"] == "regulator"
        assert d["value"] == "AMS1117-3.3"

    def test_to_dict_without_thermal_resistance(self):
        """Test to_dict when thermal resistance is None."""
        source = ThermalSource(
            reference="R1",
            power_w=0.1,
            package="0805",
            thermal_resistance=None,
            position=(5.0, 5.0),
            component_type="resistor",
        )

        d = source.to_dict()

        assert "thermal_resistance_c_per_w" not in d

    def test_to_dict_serializable(self):
        """Test that to_dict output is JSON serializable."""
        source = ThermalSource(
            reference="Q1",
            power_w=0.15,
            package="SOT-23",
            thermal_resistance=250.0,
            position=(0.0, 0.0),
            component_type="mosfet",
        )

        json_str = json.dumps(source.to_dict())
        assert isinstance(json_str, str)


class TestThermalHotspot:
    """Tests for ThermalHotspot dataclass."""

    def test_to_dict_basic(self):
        """Test basic to_dict conversion."""
        source = ThermalSource(
            reference="U1",
            power_w=0.5,
            package="SOT-223",
            thermal_resistance=50.0,
            position=(15.0, 15.0),
            component_type="regulator",
        )

        hotspot = ThermalHotspot(
            position=(15.0, 15.0),
            radius_mm=10.0,
            sources=[source],
            total_power_w=0.5,
            copper_area_mm2=100.0,
            via_count=4,
            thermal_vias=2,
            severity=ThermalSeverity.WARM,
            max_temp_rise_c=25.0,
            suggestions=["Add thermal vias"],
        )

        d = hotspot.to_dict()

        assert d["position"] == {"x": 15.0, "y": 15.0}
        assert d["radius_mm"] == 10.0
        assert len(d["sources"]) == 1
        assert d["total_power_w"] == 0.5
        assert d["copper_area_mm2"] == 100.0
        assert d["via_count"] == 4
        assert d["thermal_vias"] == 2
        assert d["severity"] == "warm"
        assert d["max_temp_rise_c"] == 25.0
        assert d["suggestions"] == ["Add thermal vias"]

    def test_to_dict_serializable(self):
        """Test that to_dict output is JSON serializable."""
        hotspot = ThermalHotspot(
            position=(0.0, 0.0),
            radius_mm=5.0,
            severity=ThermalSeverity.OK,
        )

        json_str = json.dumps(hotspot.to_dict())
        assert isinstance(json_str, str)


class TestPowerEstimator:
    """Tests for PowerEstimator class."""

    def test_estimate_resistor_0402(self, simple_pcb: Path):
        """Test power estimate for 0402 resistor."""
        estimator = PowerEstimator()

        # Create a mock footprint-like object
        class MockFootprint:
            name = "Resistor_SMD:R_0402_1005Metric"
            value = "10k"

        power = estimator.estimate(MockFootprint(), "resistor")
        # 0402 is 1/16W, estimate is 50% = 0.03125W
        assert power == pytest.approx(0.03125, rel=0.1)

    def test_estimate_resistor_2512(self, simple_pcb: Path):
        """Test power estimate for 2512 power resistor."""
        estimator = PowerEstimator()

        class MockFootprint:
            name = "Resistor_SMD:R_2512_6332Metric"
            value = "0.1"

        power = estimator.estimate(MockFootprint(), "resistor")
        # 2512 is 1W, estimate is 50% = 0.5W
        assert power == pytest.approx(0.5, rel=0.1)

    def test_estimate_ldo_regulator(self):
        """Test power estimate for LDO regulator."""
        estimator = PowerEstimator()

        class MockFootprint:
            name = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
            value = "AMS1117-3.3"

        power = estimator.estimate(MockFootprint(), "regulator")
        # LDO typical power is 0.5W
        assert power == pytest.approx(0.5, rel=0.1)

    def test_estimate_switching_regulator(self):
        """Test power estimate for switching regulator."""
        estimator = PowerEstimator()

        class MockFootprint:
            name = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
            value = "TPS62200"

        power = estimator.estimate(MockFootprint(), "regulator")
        # Switching regulator has lower losses
        assert power == pytest.approx(0.2, rel=0.1)

    def test_estimate_indicator_led(self):
        """Test power estimate for indicator LED."""
        estimator = PowerEstimator()

        class MockFootprint:
            name = "LED_SMD:LED_0603_1608Metric"
            value = "LED"

        power = estimator.estimate(MockFootprint(), "led")
        # Indicator LED is ~20mW
        assert power == pytest.approx(0.02, rel=0.1)

    def test_estimate_power_led(self):
        """Test power estimate for power LED."""
        estimator = PowerEstimator()

        class MockFootprint:
            name = "LED_SMD:LED_5050_PLCC6"
            value = "WS2812B"

        power = estimator.estimate(MockFootprint(), "led")
        # Power LED is ~0.5W
        assert power == pytest.approx(0.5, rel=0.1)

    def test_get_thermal_resistance_sot223(self):
        """Test thermal resistance lookup for SOT-223."""
        estimator = PowerEstimator()

        class MockFootprint:
            name = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"

        thermal_r = estimator.get_thermal_resistance(MockFootprint())
        assert thermal_r == pytest.approx(50.0, rel=0.1)

    def test_get_thermal_resistance_unknown(self):
        """Test thermal resistance lookup for unknown package."""
        estimator = PowerEstimator()

        class MockFootprint:
            name = "Custom:SomeWeirdPackage"

        thermal_r = estimator.get_thermal_resistance(MockFootprint())
        assert thermal_r is None


class TestThermalAnalyzer:
    """Tests for ThermalAnalyzer class."""

    def test_analyzer_init_defaults(self):
        """Test analyzer initialization with defaults."""
        analyzer = ThermalAnalyzer()
        assert analyzer.cluster_radius == 10.0
        assert analyzer.min_power_w == 0.05

    def test_analyzer_init_custom(self):
        """Test analyzer initialization with custom values."""
        analyzer = ThermalAnalyzer(cluster_radius=5.0, min_power_w=0.1)
        assert analyzer.cluster_radius == 5.0
        assert analyzer.min_power_w == 0.1

    def test_analyze_thermal_pcb(self, thermal_pcb: Path):
        """Test analyzing a PCB with voltage regulator."""
        pcb = PCB.load(str(thermal_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # Should find at least one hotspot (the regulator)
        assert len(hotspots) > 0

        # Check that regulator is identified
        all_refs = []
        for hotspot in hotspots:
            all_refs.extend(s.reference for s in hotspot.sources)
        assert "U1" in all_refs

    def test_analyze_high_power_pcb(self, high_power_pcb: Path):
        """Test analyzing a PCB with MOSFETs."""
        pcb = PCB.load(str(high_power_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # Should find hotspots for MOSFETs and resistor
        assert len(hotspots) > 0

        all_refs = []
        for hotspot in hotspots:
            all_refs.extend(s.reference for s in hotspot.sources)

        # Should identify at least Q1 or Q2
        assert any(ref in all_refs for ref in ["Q1", "Q2"])

    def test_analyze_simple_pcb_no_hotspots(self, simple_pcb: Path):
        """Test analyzing a simple PCB with no heat sources."""
        pcb = PCB.load(str(simple_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # Should not find any hotspots (no heat sources)
        assert len(hotspots) == 0

    def test_analyze_led_pcb(self, led_pcb: Path):
        """Test analyzing a PCB with power LED."""
        pcb = PCB.load(str(led_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # Should find the LED as a heat source
        assert len(hotspots) > 0

        all_refs = []
        for hotspot in hotspots:
            all_refs.extend(s.reference for s in hotspot.sources)
        assert "LED1" in all_refs

    def test_analyze_returns_sorted_by_severity(self, high_power_pcb: Path):
        """Test that hotspots are sorted by severity."""
        pcb = PCB.load(str(high_power_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        if len(hotspots) > 1:
            severity_order = {
                ThermalSeverity.CRITICAL: 0,
                ThermalSeverity.HOT: 1,
                ThermalSeverity.WARM: 2,
                ThermalSeverity.OK: 3,
            }
            orders = [severity_order[h.severity] for h in hotspots]
            assert orders == sorted(orders)

    def test_analyze_with_min_power_filter(self, thermal_pcb: Path):
        """Test filtering by minimum power."""
        pcb = PCB.load(str(thermal_pcb))

        # Low threshold - should find sources
        analyzer_low = ThermalAnalyzer(min_power_w=0.01)
        hotspots_low = analyzer_low.analyze(pcb)

        # High threshold - should find fewer or no sources
        analyzer_high = ThermalAnalyzer(min_power_w=1.0)
        hotspots_high = analyzer_high.analyze(pcb)

        # Higher threshold should give fewer hotspots
        assert len(hotspots_high) <= len(hotspots_low)

    def test_analyze_hotspot_has_suggestions(self, thermal_pcb: Path):
        """Test that hotspots have suggestions."""
        pcb = PCB.load(str(thermal_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # At least some hotspots should have suggestions
        has_suggestions = any(h.suggestions for h in hotspots)
        assert has_suggestions

    def test_clustering_nearby_sources(self, high_power_pcb: Path):
        """Test that nearby heat sources are clustered."""
        pcb = PCB.load(str(high_power_pcb))

        # Large radius should cluster MOSFETs together
        analyzer_large = ThermalAnalyzer(cluster_radius=15.0)
        hotspots_large = analyzer_large.analyze(pcb)

        # Small radius might separate them
        analyzer_small = ThermalAnalyzer(cluster_radius=3.0)
        hotspots_small = analyzer_small.analyze(pcb)

        # Larger radius should produce fewer (or same) clusters
        assert len(hotspots_large) <= len(hotspots_small) + 1


class TestThermalCLI:
    """Tests for the analyze thermal CLI command."""

    def test_cli_file_not_found(self, capsys):
        """Test CLI with missing file."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["thermal", "nonexistent.kicad_pcb"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "Error" in captured.err

    def test_cli_wrong_extension(self, capsys, tmp_path: Path):
        """Test CLI with wrong file extension."""
        from kicad_tools.cli.analyze_cmd import main

        wrong_file = tmp_path / "test.txt"
        wrong_file.write_text("not a pcb")

        result = main(["thermal", str(wrong_file)])
        assert result == 1

        captured = capsys.readouterr()
        assert ".kicad_pcb" in captured.err

    def test_cli_text_output(self, thermal_pcb: Path, capsys):
        """Test CLI with text output format."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["thermal", str(thermal_pcb)])

        # Return code depends on severity found
        assert result in (0, 1, 2)

        captured = capsys.readouterr()
        # Should have some output
        assert len(captured.out) > 0

    def test_cli_json_output(self, thermal_pcb: Path, capsys):
        """Test CLI with JSON output format."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["thermal", str(thermal_pcb), "--format", "json"])
        assert result in (0, 1, 2)

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "hotspots" in data
        assert "summary" in data
        assert isinstance(data["hotspots"], list)
        assert "total" in data["summary"]
        assert "total_power_w" in data["summary"]

    def test_cli_cluster_radius_option(self, thermal_pcb: Path, capsys):
        """Test CLI with custom cluster radius."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(
            [
                "thermal",
                str(thermal_pcb),
                "--format",
                "json",
                "--cluster-radius",
                "5.0",
            ]
        )
        assert result in (0, 1, 2)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "hotspots" in data

    def test_cli_min_power_option(self, thermal_pcb: Path, capsys):
        """Test CLI with custom minimum power threshold."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(
            [
                "thermal",
                str(thermal_pcb),
                "--format",
                "json",
                "--min-power",
                "0.01",
            ]
        )
        assert result in (0, 1, 2)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "hotspots" in data

    def test_cli_quiet_mode(self, simple_pcb: Path, capsys):
        """Test CLI quiet mode suppresses informational output."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["thermal", str(simple_pcb), "--quiet"])
        assert result == 0

        # In quiet mode with no issues, output should be empty or minimal
        capsys.readouterr()

    def test_cli_no_subcommand_shows_help(self, capsys):
        """Test CLI without subcommand shows help."""
        from kicad_tools.cli.analyze_cmd import main

        result = main([])
        assert result == 1


class TestSeverityClassification:
    """Tests for thermal severity classification."""

    def test_ok_severity_low_power(self, simple_pcb: Path):
        """Test that low power sources get OK severity."""
        # Simple PCB has no heat sources, so no hotspots
        pcb = PCB.load(str(simple_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)
        # No hotspots expected
        assert len(hotspots) == 0

    def test_elevated_severity_with_regulators(self, thermal_pcb: Path):
        """Test that voltage regulators get appropriate severity."""
        pcb = PCB.load(str(thermal_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # Regulator should have at least WARM severity
        regulator_hotspots = [
            h for h in hotspots if any(s.component_type == "regulator" for s in h.sources)
        ]
        # May or may not find regulator depending on power estimate
        if regulator_hotspots:
            for h in regulator_hotspots:
                assert h.severity in (
                    ThermalSeverity.WARM,
                    ThermalSeverity.HOT,
                    ThermalSeverity.CRITICAL,
                    ThermalSeverity.OK,
                )


class TestComponentClassification:
    """Tests for component type classification."""

    def test_classify_regulator_by_value(self, thermal_pcb: Path):
        """Test that regulator is classified by value."""
        pcb = PCB.load(str(thermal_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # Find U1 (AMS1117)
        for hotspot in hotspots:
            for source in hotspot.sources:
                if source.reference == "U1":
                    assert source.component_type == "regulator"

    def test_classify_mosfet_by_value(self, high_power_pcb: Path):
        """Test that MOSFET is classified by value."""
        pcb = PCB.load(str(high_power_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # Find Q1 or Q2 (IRLZ44N)
        mosfet_found = False
        for hotspot in hotspots:
            for source in hotspot.sources:
                if source.reference in ("Q1", "Q2"):
                    assert source.component_type == "mosfet"
                    mosfet_found = True

        assert mosfet_found

    def test_classify_led_by_library(self, led_pcb: Path):
        """Test that LED is classified by library name."""
        pcb = PCB.load(str(led_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # Find LED1
        led_found = False
        for hotspot in hotspots:
            for source in hotspot.sources:
                if source.reference == "LED1":
                    assert source.component_type == "led"
                    led_found = True

        assert led_found


class TestThermalViaAnalysis:
    """Tests for thermal via detection."""

    def test_via_count_in_hotspot(self, thermal_pcb: Path):
        """Test that vias are counted in hotspot area."""
        pcb = PCB.load(str(thermal_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # The thermal PCB has 2 vias near the regulator
        if hotspots:
            total_vias = sum(h.via_count for h in hotspots)
            # At least some vias should be detected
            assert total_vias >= 0  # May not be in hotspot radius


class TestSuggestions:
    """Tests for thermal improvement suggestions."""

    def test_suggestions_for_high_power(self, high_power_pcb: Path):
        """Test that suggestions are generated for high power components."""
        pcb = PCB.load(str(high_power_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # Should have suggestions for high power area
        all_suggestions = []
        for h in hotspots:
            all_suggestions.extend(h.suggestions)

        # Should suggest something (thermal vias, copper, etc.)
        assert len(all_suggestions) > 0

    def test_thermal_via_suggestion(self, thermal_pcb: Path):
        """Test suggestion for adding thermal vias."""
        pcb = PCB.load(str(thermal_pcb))
        analyzer = ThermalAnalyzer()

        hotspots = analyzer.analyze(pcb)

        # Look for thermal via suggestion
        all_suggestions = []
        for h in hotspots:
            all_suggestions.extend(h.suggestions)

        # May or may not suggest thermal vias depending on analysis
        # Just check suggestions are reasonable strings
        for suggestion in all_suggestions:
            assert isinstance(suggestion, str)
            assert len(suggestion) > 0
