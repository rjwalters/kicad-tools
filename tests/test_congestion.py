"""Tests for routing congestion analysis."""

import json
from pathlib import Path

import pytest

from kicad_tools.analysis import CongestionAnalyzer, CongestionReport, Severity
from kicad_tools.schema.pcb import PCB


# PCB with multiple traces and vias in a small area (congested)
CONGESTED_PCB = """(kicad_pcb
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
  (net 5 "GPIO1")
  (net 6 "GPIO2")

  (gr_rect
    (start 0 0)
    (end 20 20)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Package_QFP:LQFP-48_7x7mm_P0.5mm"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "U1")
    (pad "1" smd rect (at -3.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at -3.0 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 2 "GND"))
    (pad "3" smd rect (at -2.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 3 "SDA"))
    (pad "4" smd rect (at -2.0 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 4 "SCL"))
    (pad "5" smd rect (at -1.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 5 "GPIO1"))
    (pad "6" smd rect (at -1.0 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 6 "GPIO2"))
    (pad "7" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
  )

  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (at 8 10)
    (property "Reference" "C1")
    (pad "1" smd rect (at 0 -0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (at 12 10)
    (property "Reference" "C2")
    (pad "1" smd rect (at 0 -0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 1 "VCC"))
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu" "F.Mask") (net 2 "GND"))
  )

  (segment (start 6.5 10) (end 7.5 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 7.5 10) (end 8 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 8 10) (end 9 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 9 10) (end 10 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 10 10) (end 11 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 11 10) (end 12 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 12 10) (end 13 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 13 10) (end 14 10) (width 0.25) (layer "F.Cu") (net 1))

  (segment (start 6.5 10.5) (end 14 10.5) (width 0.25) (layer "F.Cu") (net 2))
  (segment (start 6.5 9.5) (end 14 9.5) (width 0.25) (layer "F.Cu") (net 3))
  (segment (start 6.5 9) (end 14 9) (width 0.25) (layer "F.Cu") (net 4))
  (segment (start 6.5 11) (end 14 11) (width 0.25) (layer "F.Cu") (net 5))
  (segment (start 6.5 11.5) (end 14 11.5) (width 0.25) (layer "F.Cu") (net 6))

  (via (at 8 9) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via (at 9 9) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))
  (via (at 10 9) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 3))
  (via (at 11 9) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 4))
  (via (at 12 9) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 5))
  (via (at 8 11) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via (at 9 11) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))
  (via (at 10 11) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 3))
  (via (at 11 11) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 4))
  (via (at 12 11) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 5))
)
"""


# Simple PCB with minimal routing (not congested)
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
  (net 1 "VCC")
  (net 2 "GND")

  (gr_rect
    (start 0 0)
    (end 50 50)
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

  (segment (start 11 10) (end 39 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 39 10) (end 39 40) (width 0.25) (layer "F.Cu") (net 1))
)
"""


@pytest.fixture
def congested_pcb(tmp_path: Path) -> Path:
    """Create a congested PCB file."""
    pcb_file = tmp_path / "congested.kicad_pcb"
    pcb_file.write_text(CONGESTED_PCB)
    return pcb_file


@pytest.fixture
def simple_pcb(tmp_path: Path) -> Path:
    """Create a simple non-congested PCB file."""
    pcb_file = tmp_path / "simple.kicad_pcb"
    pcb_file.write_text(SIMPLE_PCB)
    return pcb_file


class TestCongestionReport:
    """Tests for CongestionReport dataclass."""

    def test_to_dict_basic(self):
        """Test basic to_dict conversion."""
        report = CongestionReport(
            center=(10.5, 20.5),
            radius=2.0,
            track_density=1.5,
            via_count=5,
            unrouted_connections=2,
            components=["U1", "C1"],
            nets=["VCC", "GND"],
            severity=Severity.HIGH,
            suggestions=["Move components", "Use inner layers"],
        )

        d = report.to_dict()

        assert d["center"] == {"x": 10.5, "y": 20.5}
        assert d["radius"] == 2.0
        assert d["track_density"] == 1.5
        assert d["via_count"] == 5
        assert d["unrouted_connections"] == 2
        assert d["components"] == ["U1", "C1"]
        assert d["nets"] == ["VCC", "GND"]
        assert d["severity"] == "high"
        assert len(d["suggestions"]) == 2

    def test_to_dict_serializable(self):
        """Test that to_dict output is JSON serializable."""
        report = CongestionReport(
            center=(10.0, 20.0),
            radius=2.0,
            track_density=1.5,
            via_count=5,
            unrouted_connections=0,
            severity=Severity.MEDIUM,
        )

        # Should not raise
        json_str = json.dumps(report.to_dict())
        assert isinstance(json_str, str)


class TestCongestionAnalyzer:
    """Tests for CongestionAnalyzer class."""

    def test_analyzer_init_defaults(self):
        """Test analyzer initialization with defaults."""
        analyzer = CongestionAnalyzer()
        assert analyzer.grid_size == 2.0
        assert analyzer.merge_radius == 5.0

    def test_analyzer_init_custom(self):
        """Test analyzer initialization with custom values."""
        analyzer = CongestionAnalyzer(grid_size=1.0, merge_radius=3.0)
        assert analyzer.grid_size == 1.0
        assert analyzer.merge_radius == 3.0

    def test_analyze_congested_pcb(self, congested_pcb: Path):
        """Test analyzing a congested PCB."""
        pcb = PCB.load(str(congested_pcb))
        analyzer = CongestionAnalyzer(grid_size=2.0)

        reports = analyzer.analyze(pcb)

        # Should find at least one congested area
        assert len(reports) > 0

        # Reports should be sorted by severity
        if len(reports) > 1:
            severities = [r.severity for r in reports]
            severity_order = {
                Severity.CRITICAL: 0,
                Severity.HIGH: 1,
                Severity.MEDIUM: 2,
                Severity.LOW: 3,
            }
            orders = [severity_order[s] for s in severities]
            assert orders == sorted(orders)

    def test_analyze_simple_pcb(self, simple_pcb: Path):
        """Test analyzing a simple non-congested PCB."""
        pcb = PCB.load(str(simple_pcb))
        analyzer = CongestionAnalyzer(grid_size=2.0)

        reports = analyzer.analyze(pcb)

        # May find some low-severity areas but should not find critical/high
        critical_high = [r for r in reports if r.severity in (Severity.CRITICAL, Severity.HIGH)]
        assert len(critical_high) == 0

    def test_analyze_reports_have_required_fields(self, congested_pcb: Path):
        """Test that reports have all required fields."""
        pcb = PCB.load(str(congested_pcb))
        analyzer = CongestionAnalyzer()

        reports = analyzer.analyze(pcb)

        for report in reports:
            assert isinstance(report.center, tuple)
            assert len(report.center) == 2
            assert isinstance(report.radius, float)
            assert isinstance(report.track_density, float)
            assert isinstance(report.via_count, int)
            assert isinstance(report.unrouted_connections, int)
            assert isinstance(report.components, list)
            assert isinstance(report.nets, list)
            assert isinstance(report.severity, Severity)
            assert isinstance(report.suggestions, list)

    def test_analyze_suggestions_generated(self, congested_pcb: Path):
        """Test that suggestions are generated for congested areas."""
        pcb = PCB.load(str(congested_pcb))
        analyzer = CongestionAnalyzer()

        reports = analyzer.analyze(pcb)

        # At least some reports should have suggestions
        reports_with_suggestions = [r for r in reports if r.suggestions]
        assert len(reports_with_suggestions) > 0

    def test_analyze_with_small_grid(self, congested_pcb: Path):
        """Test analysis with smaller grid size."""
        pcb = PCB.load(str(congested_pcb))
        analyzer = CongestionAnalyzer(grid_size=1.0)

        reports = analyzer.analyze(pcb)

        # Smaller grid should still work
        assert isinstance(reports, list)

    def test_analyze_components_identified(self, congested_pcb: Path):
        """Test that components are identified in reports."""
        pcb = PCB.load(str(congested_pcb))
        analyzer = CongestionAnalyzer()

        reports = analyzer.analyze(pcb)

        # Some reports should identify components
        all_components = set()
        for report in reports:
            all_components.update(report.components)

        # Should identify at least U1 from the congested area
        assert len(all_components) > 0

    def test_analyze_nets_identified(self, congested_pcb: Path):
        """Test that nets are identified in reports."""
        pcb = PCB.load(str(congested_pcb))
        analyzer = CongestionAnalyzer()

        reports = analyzer.analyze(pcb)

        # Some reports should identify nets
        all_nets = set()
        for report in reports:
            all_nets.update(report.nets)

        # Should identify some nets
        assert len(all_nets) > 0


class TestCongestionCLI:
    """Tests for the analyze congestion CLI command."""

    def test_cli_file_not_found(self, capsys):
        """Test CLI with missing file."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["congestion", "nonexistent.kicad_pcb"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "Error" in captured.err

    def test_cli_wrong_extension(self, capsys, tmp_path: Path):
        """Test CLI with wrong file extension."""
        from kicad_tools.cli.analyze_cmd import main

        wrong_file = tmp_path / "test.txt"
        wrong_file.write_text("not a pcb")

        result = main(["congestion", str(wrong_file)])
        assert result == 1

        captured = capsys.readouterr()
        assert ".kicad_pcb" in captured.err

    def test_cli_text_output(self, congested_pcb: Path, capsys):
        """Test CLI with text output format."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["congestion", str(congested_pcb)])

        # Return code depends on severity found
        assert result in (0, 1, 2)

        captured = capsys.readouterr()
        # Should have some output
        assert len(captured.out) > 0

    def test_cli_json_output(self, congested_pcb: Path, capsys):
        """Test CLI with JSON output format."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["congestion", str(congested_pcb), "--format", "json"])
        assert result in (0, 1, 2)

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "reports" in data
        assert "summary" in data
        assert isinstance(data["reports"], list)
        assert "total" in data["summary"]

    def test_cli_min_severity_filter(self, congested_pcb: Path, capsys):
        """Test CLI with minimum severity filter."""
        from kicad_tools.cli.analyze_cmd import main

        # Run with critical filter - should filter out lower severity
        result = main(
            [
                "congestion",
                str(congested_pcb),
                "--format",
                "json",
                "--min-severity",
                "critical",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # All remaining reports should be critical
        for report in data["reports"]:
            assert report["severity"] == "critical"

    def test_cli_grid_size_option(self, congested_pcb: Path, capsys):
        """Test CLI with custom grid size."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(
            [
                "congestion",
                str(congested_pcb),
                "--format",
                "json",
                "--grid-size",
                "1.0",
            ]
        )
        assert result in (0, 1, 2)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "reports" in data

    def test_cli_quiet_mode(self, simple_pcb: Path, capsys):
        """Test CLI quiet mode suppresses informational output."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["congestion", str(simple_pcb), "--quiet"])
        assert result == 0

        captured = capsys.readouterr()
        # In quiet mode with no issues, output should be minimal
        # (no header, no "No congestion issues found" message)


class TestSeverityClassification:
    """Tests for severity classification logic."""

    def test_severity_low_threshold(self, tmp_path: Path):
        """Test that low density gets LOW severity."""
        # PCB with minimal routing
        pcb_content = SIMPLE_PCB
        pcb_file = tmp_path / "low.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        analyzer = CongestionAnalyzer()

        reports = analyzer.analyze(pcb)

        # All should be LOW severity (if any)
        for report in reports:
            assert report.severity == Severity.LOW

    def test_severity_high_with_many_vias(self, congested_pcb: Path):
        """Test that areas with many vias get higher severity."""
        pcb = PCB.load(str(congested_pcb))
        analyzer = CongestionAnalyzer()

        reports = analyzer.analyze(pcb)

        # The congested PCB has many vias in a small area
        # Should find at least one area with elevated severity
        elevated = [
            r for r in reports if r.severity in (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)
        ]
        assert len(elevated) > 0
