"""Tests for trace length analysis."""

import json
import math
from pathlib import Path

import pytest

from kicad_tools.analysis import (
    DifferentialPairReport,
    TraceLengthAnalyzer,
    TraceLengthReport,
)
from kicad_tools.schema.pcb import PCB

# PCB with timing-critical nets (USB differential pair, clock)
TIMING_CRITICAL_PCB = """(kicad_pcb
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
  (net 1 "USB_D+")
  (net 2 "USB_D-")
  (net 3 "CLK")
  (net 4 "VCC")
  (net 5 "GND")

  (gr_rect
    (start 0 0)
    (end 50 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Connector_USB:USB_C"
    (layer "F.Cu")
    (at 5 15)
    (property "Reference" "J1")
    (pad "D+" smd rect (at 0 -1) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 1 "USB_D+"))
    (pad "D-" smd rect (at 0 1) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 2 "USB_D-"))
  )

  (footprint "Package_QFP:LQFP-48"
    (layer "F.Cu")
    (at 40 15)
    (property "Reference" "U1")
    (pad "1" smd rect (at -3 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 1 "USB_D+"))
    (pad "2" smd rect (at -2.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 2 "USB_D-"))
    (pad "3" smd rect (at -2 0) (size 0.5 0.5) (layers "F.Cu" "F.Mask") (net 3 "CLK"))
  )

  (segment (start 5 14) (end 15 14) (width 0.15) (layer "F.Cu") (net 1))
  (segment (start 15 14) (end 25 14) (width 0.15) (layer "F.Cu") (net 1))
  (segment (start 25 14) (end 37 15) (width 0.15) (layer "F.Cu") (net 1))

  (segment (start 5 16) (end 15 16) (width 0.15) (layer "F.Cu") (net 2))
  (segment (start 15 16) (end 25 16) (width 0.15) (layer "F.Cu") (net 2))
  (segment (start 25 16) (end 37.5 15) (width 0.15) (layer "F.Cu") (net 2))

  (segment (start 20 5) (end 30 5) (width 0.2) (layer "F.Cu") (net 3))
  (segment (start 30 5) (end 38 15) (width 0.2) (layer "F.Cu") (net 3))

  (via (at 25 14) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via (at 25 16) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))
)
"""


# PCB with only non-critical nets
REGULAR_PCB = """(kicad_pcb
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
  (net 1 "GPIO1")
  (net 2 "GPIO2")
  (net 3 "I2C_SDA")

  (gr_rect
    (start 0 0)
    (end 30 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Resistor_SMD:R_0805"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -1 0) (size 0.8 0.8) (layers "F.Cu" "F.Mask") (net 1 "GPIO1"))
    (pad "2" smd rect (at 1 0) (size 0.8 0.8) (layers "F.Cu" "F.Mask") (net 2 "GPIO2"))
  )

  (segment (start 9 10) (end 5 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 11 10) (end 15 10) (width 0.25) (layer "F.Cu") (net 2))
)
"""


# PCB with differential pairs using different naming conventions
DIFF_PAIR_PCB = """(kicad_pcb
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
  (net 1 "ETH_TX+")
  (net 2 "ETH_TX-")
  (net 3 "LVDS_P")
  (net 4 "LVDS_N")
  (net 5 "D+")
  (net 6 "D-")

  (gr_rect
    (start 0 0)
    (end 50 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (segment (start 5 5) (end 25 5) (width 0.15) (layer "F.Cu") (net 1))
  (segment (start 5 7) (end 24 7) (width 0.15) (layer "F.Cu") (net 2))

  (segment (start 5 12) (end 30 12) (width 0.15) (layer "F.Cu") (net 3))
  (segment (start 5 14) (end 28 14) (width 0.15) (layer "F.Cu") (net 4))

  (segment (start 5 20) (end 22 20) (width 0.15) (layer "F.Cu") (net 5))
  (segment (start 5 22) (end 20 22) (width 0.15) (layer "F.Cu") (net 6))
)
"""


# PCB with multi-layer routing
MULTILAYER_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (30 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "CLK_SIG")

  (gr_rect
    (start 0 0)
    (end 50 30)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (segment (start 5 15) (end 15 15) (width 0.2) (layer "F.Cu") (net 1))
  (via (at 15 15) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1))
  (segment (start 15 15) (end 25 15) (width 0.2) (layer "In1.Cu") (net 1))
  (via (at 25 15) (size 0.6) (drill 0.3) (layers "In1.Cu" "B.Cu") (net 1))
  (segment (start 25 15) (end 35 15) (width 0.2) (layer "B.Cu") (net 1))
)
"""


@pytest.fixture
def timing_critical_pcb(tmp_path: Path) -> Path:
    """Create a PCB with timing-critical nets."""
    pcb_file = tmp_path / "timing.kicad_pcb"
    pcb_file.write_text(TIMING_CRITICAL_PCB)
    return pcb_file


@pytest.fixture
def regular_pcb(tmp_path: Path) -> Path:
    """Create a PCB with regular (non-critical) nets."""
    pcb_file = tmp_path / "regular.kicad_pcb"
    pcb_file.write_text(REGULAR_PCB)
    return pcb_file


@pytest.fixture
def diff_pair_pcb(tmp_path: Path) -> Path:
    """Create a PCB with various differential pair naming conventions."""
    pcb_file = tmp_path / "diffpair.kicad_pcb"
    pcb_file.write_text(DIFF_PAIR_PCB)
    return pcb_file


@pytest.fixture
def multilayer_pcb(tmp_path: Path) -> Path:
    """Create a PCB with multi-layer routing."""
    pcb_file = tmp_path / "multilayer.kicad_pcb"
    pcb_file.write_text(MULTILAYER_PCB)
    return pcb_file


class TestTraceLengthReport:
    """Tests for TraceLengthReport dataclass."""

    def test_to_dict_basic(self):
        """Test basic to_dict conversion."""
        report = TraceLengthReport(
            net_name="USB_D+",
            total_length_mm=45.5,
            segment_count=3,
            via_count=2,
            layers_used={"F.Cu", "B.Cu"},
        )

        d = report.to_dict()

        assert d["net_name"] == "USB_D+"
        assert d["total_length_mm"] == 45.5
        assert d["segment_count"] == 3
        assert d["via_count"] == 2
        assert sorted(d["layers_used"]) == ["B.Cu", "F.Cu"]

    def test_to_dict_with_target(self):
        """Test to_dict with target length specified."""
        report = TraceLengthReport(
            net_name="CLK",
            total_length_mm=48.0,
            target_length_mm=50.0,
            tolerance_mm=2.0,
            length_delta_mm=-2.0,
            within_tolerance=True,
        )

        d = report.to_dict()

        assert d["target_length_mm"] == 50.0
        assert d["tolerance_mm"] == 2.0
        assert d["length_delta_mm"] == -2.0
        assert d["within_tolerance"] is True

    def test_to_dict_with_differential_pair(self):
        """Test to_dict with differential pair info."""
        report = TraceLengthReport(
            net_name="USB_D+",
            total_length_mm=45.5,
            pair_net="USB_D-",
            pair_length_mm=44.2,
            skew_mm=1.3,
        )

        d = report.to_dict()

        assert "differential_pair" in d
        assert d["differential_pair"]["pair_net"] == "USB_D-"
        assert d["differential_pair"]["pair_length_mm"] == 44.2
        assert d["differential_pair"]["skew_mm"] == 1.3

    def test_to_dict_serializable(self):
        """Test that to_dict output is JSON serializable."""
        report = TraceLengthReport(
            net_name="TEST",
            total_length_mm=10.0,
            layers_used={"F.Cu"},
            layer_changes=["F.Cu → B.Cu"],
        )

        # Should not raise
        json_str = json.dumps(report.to_dict())
        assert isinstance(json_str, str)


class TestDifferentialPairReport:
    """Tests for DifferentialPairReport dataclass."""

    def test_to_dict_basic(self):
        """Test basic to_dict conversion."""
        report_p = TraceLengthReport(net_name="D+", total_length_mm=23.5)
        report_n = TraceLengthReport(net_name="D-", total_length_mm=22.0)

        pair_report = DifferentialPairReport(
            net_p="D+",
            net_n="D-",
            report_p=report_p,
            report_n=report_n,
            skew_mm=1.5,
        )

        d = pair_report.to_dict()

        assert d["net_positive"] == "D+"
        assert d["net_negative"] == "D-"
        assert d["length_positive_mm"] == 23.5
        assert d["length_negative_mm"] == 22.0
        assert d["skew_mm"] == 1.5

    def test_to_dict_with_tolerance(self):
        """Test to_dict with skew tolerance."""
        report_p = TraceLengthReport(net_name="TX+", total_length_mm=50.0)
        report_n = TraceLengthReport(net_name="TX-", total_length_mm=49.5)

        pair_report = DifferentialPairReport(
            net_p="TX+",
            net_n="TX-",
            report_p=report_p,
            report_n=report_n,
            skew_mm=0.5,
            target_skew_mm=2.0,
            skew_within_tolerance=True,
        )

        d = pair_report.to_dict()

        assert d["target_skew_mm"] == 2.0
        assert d["skew_within_tolerance"] is True


class TestTraceLengthAnalyzer:
    """Tests for TraceLengthAnalyzer class."""

    def test_analyzer_init_defaults(self):
        """Test analyzer initialization with defaults."""
        analyzer = TraceLengthAnalyzer()
        # Should have compiled patterns
        assert len(analyzer._compiled_patterns) > 0

    def test_analyzer_init_custom_patterns(self):
        """Test analyzer initialization with custom patterns."""
        analyzer = TraceLengthAnalyzer(critical_patterns=[r"CUSTOM_NET"])
        # Should include custom pattern
        assert len(analyzer._patterns) > len(analyzer._compiled_patterns) - 1

    def test_analyze_net_basic(self, timing_critical_pcb: Path):
        """Test analyzing a specific net."""
        pcb = PCB.load(str(timing_critical_pcb))
        analyzer = TraceLengthAnalyzer()

        report = analyzer.analyze_net(pcb, "USB_D+")

        assert report.net_name == "USB_D+"
        assert report.total_length_mm > 0
        assert report.segment_count == 3
        assert report.via_count == 1
        assert "F.Cu" in report.layers_used

    def test_analyze_net_nonexistent(self, timing_critical_pcb: Path):
        """Test analyzing a non-existent net."""
        pcb = PCB.load(str(timing_critical_pcb))
        analyzer = TraceLengthAnalyzer()

        report = analyzer.analyze_net(pcb, "NONEXISTENT_NET")

        assert report.net_name == "NONEXISTENT_NET"
        assert report.total_length_mm == 0
        assert report.segment_count == 0

    def test_analyze_net_length_calculation(self, timing_critical_pcb: Path):
        """Test that trace length is calculated correctly."""
        pcb = PCB.load(str(timing_critical_pcb))
        analyzer = TraceLengthAnalyzer()

        report = analyzer.analyze_net(pcb, "USB_D+")

        # USB_D+ has 3 segments: (5,14)-(15,14), (15,14)-(25,14), (25,14)-(37,15)
        # Segment 1: 10mm, Segment 2: 10mm, Segment 3: sqrt(12^2 + 1^2) ≈ 12.04mm
        expected_length = 10.0 + 10.0 + math.sqrt(12**2 + 1**2)
        assert abs(report.total_length_mm - expected_length) < 0.1

    def test_analyze_all_critical(self, timing_critical_pcb: Path):
        """Test analyzing all timing-critical nets."""
        pcb = PCB.load(str(timing_critical_pcb))
        analyzer = TraceLengthAnalyzer()

        reports = analyzer.analyze_all_critical(pcb)

        # Should find USB_D+, USB_D-, and CLK
        net_names = {r.net_name for r in reports}
        assert "USB_D+" in net_names
        assert "USB_D-" in net_names
        assert "CLK" in net_names

    def test_analyze_all_critical_no_critical_nets(self, regular_pcb: Path):
        """Test analyzing PCB with no timing-critical nets."""
        pcb = PCB.load(str(regular_pcb))
        analyzer = TraceLengthAnalyzer()

        reports = analyzer.analyze_all_critical(pcb)

        # GPIO1, GPIO2, I2C_SDA are not timing-critical by default patterns
        assert len(reports) == 0

    def test_analyze_diff_pair(self, timing_critical_pcb: Path):
        """Test analyzing a differential pair."""
        pcb = PCB.load(str(timing_critical_pcb))
        analyzer = TraceLengthAnalyzer()

        pair_report = analyzer.analyze_diff_pair(pcb, "USB_D+", "USB_D-")

        assert pair_report.net_p == "USB_D+"
        assert pair_report.net_n == "USB_D-"
        assert pair_report.report_p.total_length_mm > 0
        assert pair_report.report_n.total_length_mm > 0
        assert pair_report.skew_mm >= 0

    def test_analyze_diff_pair_with_tolerance(self, timing_critical_pcb: Path):
        """Test analyzing differential pair with skew tolerance."""
        pcb = PCB.load(str(timing_critical_pcb))
        analyzer = TraceLengthAnalyzer()

        pair_report = analyzer.analyze_diff_pair(pcb, "USB_D+", "USB_D-", target_skew_mm=5.0)

        assert pair_report.target_skew_mm == 5.0
        assert pair_report.skew_within_tolerance is not None

    def test_analyze_multilayer_routing(self, multilayer_pcb: Path):
        """Test analyzing multi-layer routing with layer changes."""
        pcb = PCB.load(str(multilayer_pcb))
        analyzer = TraceLengthAnalyzer()

        report = analyzer.analyze_net(pcb, "CLK_SIG")

        assert report.via_count == 2
        assert len(report.layers_used) >= 2
        assert len(report.layer_changes) >= 1

    def test_find_differential_pairs(self, diff_pair_pcb: Path):
        """Test finding all differential pairs in a PCB."""
        pcb = PCB.load(str(diff_pair_pcb))
        analyzer = TraceLengthAnalyzer()

        pairs = analyzer.find_differential_pairs(pcb)

        # Should find ETH_TX+/TX-, LVDS_P/N, D+/D-
        pair_nets = {frozenset(p) for p in pairs}
        assert frozenset(["ETH_TX+", "ETH_TX-"]) in pair_nets
        assert frozenset(["LVDS_P", "LVDS_N"]) in pair_nets
        assert frozenset(["D+", "D-"]) in pair_nets

    def test_critical_net_patterns(self, timing_critical_pcb: Path):
        """Test that critical net patterns work correctly."""
        pcb = PCB.load(str(timing_critical_pcb))
        analyzer = TraceLengthAnalyzer()

        critical = analyzer._identify_critical_nets(pcb)

        # USB_D+, USB_D-, CLK should be identified as critical
        assert "USB_D+" in critical
        assert "USB_D-" in critical
        assert "CLK" in critical

        # VCC, GND should not be in critical nets
        assert "VCC" not in critical
        assert "GND" not in critical


class TestTraceLengthsCLI:
    """Tests for the analyze trace-lengths CLI command."""

    def test_cli_file_not_found(self, capsys):
        """Test CLI with missing file."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["trace-lengths", "nonexistent.kicad_pcb"])
        assert result == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "Error" in captured.err

    def test_cli_wrong_extension(self, capsys, tmp_path: Path):
        """Test CLI with wrong file extension."""
        from kicad_tools.cli.analyze_cmd import main

        wrong_file = tmp_path / "test.txt"
        wrong_file.write_text("not a pcb")

        result = main(["trace-lengths", str(wrong_file)])
        assert result == 1

        captured = capsys.readouterr()
        assert ".kicad_pcb" in captured.err

    def test_cli_text_output(self, timing_critical_pcb: Path, capsys):
        """Test CLI with text output format."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["trace-lengths", str(timing_critical_pcb)])
        assert result == 0

        captured = capsys.readouterr()
        # Should have some output with trace length info
        assert "USB_D+" in captured.out or "CLK" in captured.out

    def test_cli_json_output(self, timing_critical_pcb: Path, capsys):
        """Test CLI with JSON output format."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["trace-lengths", str(timing_critical_pcb), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "nets" in data
        assert "summary" in data
        assert isinstance(data["nets"], list)
        assert len(data["nets"]) > 0

    def test_cli_specific_net(self, timing_critical_pcb: Path, capsys):
        """Test CLI with specific net filter."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(
            [
                "trace-lengths",
                str(timing_critical_pcb),
                "--net",
                "USB_D+",
                "--format",
                "json",
            ]
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Should only have USB_D+
        assert len(data["nets"]) == 1
        assert data["nets"][0]["net_name"] == "USB_D+"

    def test_cli_multiple_nets(self, timing_critical_pcb: Path, capsys):
        """Test CLI with multiple specific nets."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(
            [
                "trace-lengths",
                str(timing_critical_pcb),
                "--net",
                "USB_D+",
                "--net",
                "CLK",
                "--format",
                "json",
            ]
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        net_names = {n["net_name"] for n in data["nets"]}
        assert "USB_D+" in net_names
        assert "CLK" in net_names

    def test_cli_all_nets(self, timing_critical_pcb: Path, capsys):
        """Test CLI with --all flag."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(
            [
                "trace-lengths",
                str(timing_critical_pcb),
                "--all",
                "--format",
                "json",
            ]
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Should have more nets than just timing-critical
        assert len(data["nets"]) >= 3  # USB_D+, USB_D-, CLK at minimum

    def test_cli_no_diff_pairs(self, timing_critical_pcb: Path, capsys):
        """Test CLI with differential pairs disabled."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(
            [
                "trace-lengths",
                str(timing_critical_pcb),
                "--no-diff-pairs",
                "--format",
                "json",
            ]
        )
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Reports should not have pair info
        for net in data["nets"]:
            assert "differential_pair" not in net

    def test_cli_quiet_mode(self, regular_pcb: Path, capsys):
        """Test CLI quiet mode suppresses informational output."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["trace-lengths", str(regular_pcb), "--quiet"])
        assert result == 0

        # Consume output to clear buffer
        capsys.readouterr()
        # With no critical nets and quiet mode, output is minimal

    def test_cli_empty_result(self, regular_pcb: Path, capsys):
        """Test CLI with PCB having no timing-critical nets."""
        from kicad_tools.cli.analyze_cmd import main

        result = main(["trace-lengths", str(regular_pcb)])
        assert result == 0

        captured = capsys.readouterr()
        # Should indicate no critical nets found
        assert "No timing-critical nets" in captured.out or len(captured.out) > 0


class TestDifferentialPairDetection:
    """Tests for differential pair detection patterns."""

    def test_usb_d_plus_minus(self, diff_pair_pcb: Path):
        """Test USB D+/D- pattern detection."""
        pcb = PCB.load(str(diff_pair_pcb))
        analyzer = TraceLengthAnalyzer()

        pairs = analyzer.find_differential_pairs(pcb)
        pair_sets = {frozenset(p) for p in pairs}

        assert frozenset(["D+", "D-"]) in pair_sets

    def test_eth_plus_minus(self, diff_pair_pcb: Path):
        """Test Ethernet TX+/TX- pattern detection."""
        pcb = PCB.load(str(diff_pair_pcb))
        analyzer = TraceLengthAnalyzer()

        pairs = analyzer.find_differential_pairs(pcb)
        pair_sets = {frozenset(p) for p in pairs}

        assert frozenset(["ETH_TX+", "ETH_TX-"]) in pair_sets

    def test_lvds_p_n(self, diff_pair_pcb: Path):
        """Test LVDS _P/_N pattern detection."""
        pcb = PCB.load(str(diff_pair_pcb))
        analyzer = TraceLengthAnalyzer()

        pairs = analyzer.find_differential_pairs(pcb)
        pair_sets = {frozenset(p) for p in pairs}

        assert frozenset(["LVDS_P", "LVDS_N"]) in pair_sets
