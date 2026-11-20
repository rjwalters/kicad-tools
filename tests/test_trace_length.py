"""Tests for trace length analysis."""

import json
import math
from pathlib import Path

import pytest

from kicad_tools.analysis import (
    DifferentialPairReport,
    NetPathMeasurement,
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
  (via blind (at 15 15) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1))
  (segment (start 15 15) (end 25 15) (width 0.2) (layer "In1.Cu") (net 1))
  (via blind (at 25 15) (size 0.6) (drill 0.3) (layers "In1.Cu" "B.Cu") (net 1))
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


# Pad-to-pad path fixtures (Issue #9117).  A two-pin net between pads at
# (5,15) and (25,15): the direct run is 20 mm.  Each variant adds copper
# the signal never travels.
PATH_BASE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "LOOP")
  (net 2 "DUP")
  (net 3 "STUB")
  (net 4 "VIAS")
  (net 5 "BRANCH")
  (net 6 "OPEN")

  (gr_rect
    (start 0 0)
    (end 50 60)
    (stroke (width 0.1))
    (layer "Edge.Cuts")
  )

  (footprint "Test:Pads"
    (layer "F.Cu")
    (at 0 0)
    (property "Reference" "J1")
    (pad "1" smd rect (at 5 15) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 1 "LOOP"))
    (pad "2" smd rect (at 25 15) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 1 "LOOP"))
    (pad "3" smd rect (at 5 30) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 2 "DUP"))
    (pad "4" smd rect (at 25 30) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 2 "DUP"))
    (pad "5" smd rect (at 5 45) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 3 "STUB"))
    (pad "6" smd rect (at 25 45) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 3 "STUB"))
    (pad "A" smd rect (at 5 5) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 4 "VIAS"))
    (pad "B" smd rect (at 15 55) (size 1.0 1.0) (layers "B.Cu" "B.Mask") (net 4 "VIAS"))
    (pad "C" smd rect (at 35 5) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 5 "BRANCH"))
    (pad "D" smd rect (at 45 5) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 5 "BRANCH"))
    (pad "E" smd rect (at 40 25) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 5 "BRANCH"))
    (pad "F" smd rect (at 45 55) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 6 "OPEN"))
    (pad "G" smd rect (at 45 58) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 6 "OPEN"))
  )

  # LOOP: direct 20 mm run, then a loop that leaves pad 2 and returns to it.
  (segment (start 5 15) (end 25 15) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 25 15) (end 35 15) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 35 15) (end 25 15) (width 0.2) (layer "F.Cu") (net 1))

  # DUP: direct 20 mm run plus an identical overlapping duplicate.
  (segment (start 5 30) (end 25 30) (width 0.2) (layer "F.Cu") (net 2))
  (segment (start 5 30) (end 25 30) (width 0.2) (layer "F.Cu") (net 2))

  # STUB: direct 20 mm trunk with a 5 mm stub joined mid-segment.
  (segment (start 5 45) (end 25 45) (width 0.2) (layer "F.Cu") (net 3))
  (segment (start 15 45) (end 15 50) (width 0.2) (layer "F.Cu") (net 3))

  # VIAS: layer change through a via at a segment junction.
  (segment (start 5 5) (end 15 5) (width 0.2) (layer "F.Cu") (net 4))
  (via (at 15 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 4))
  (segment (start 15 5) (end 15 55) (width 0.2) (layer "B.Cu") (net 4))

  # BRANCH: three pads joined by a trunk and an arm off its midpoint.
  (segment (start 35 5) (end 45 5) (width 0.2) (layer "F.Cu") (net 5))
  (segment (start 40 5) (end 40 25) (width 0.2) (layer "F.Cu") (net 5))
)
"""


@pytest.fixture
def path_base_pcb(tmp_path: Path) -> Path:
    """PCB with loop / duplicate / stub / via / branch variants of a two-pin net."""
    pcb_file = tmp_path / "paths.kicad_pcb"
    pcb_file.write_text(PATH_BASE_PCB)
    return pcb_file


def stepped_run_pcb(step_mm: float, tmp_path: Path) -> Path:
    """Two-pin net whose middle run is encoded as sub-width segments.

    Pads at (5, 15) and (45, 15) are joined by a 5 mm lead-in, a 30 mm
    middle run split into ``step_mm`` collinear segments, and a 5 mm
    lead-out, all width 0.2.  The copper is the same straight 40 mm run
    for every step; only the segment encoding differs.  Steps below the
    track width make consecutive non-adjacent segments bond through
    capsule contact (Issue #9117 review: board 07's escape copper uses
    0.1414 mm steps at width 0.2).
    """
    count = round(30.0 / step_mm)
    xs = [10.0 + i * (30.0 / count) for i in range(count + 1)]
    segments = ['(segment (start 5 15) (end 10 15) (width 0.2) (layer "F.Cu") (net 1))']
    segments += [
        f"(segment (start {xs[i]:.6f} 15) (end {xs[i + 1]:.6f} 15)"
        f' (width 0.2) (layer "F.Cu") (net 1))'
        for i in range(count)
    ]
    segments.append('(segment (start 40 15) (end 45 15) (width 0.2) (layer "F.Cu") (net 1))')
    pcb_file = tmp_path / f"stepped-{step_mm:.4f}.kicad_pcb"
    pcb_file.write_text(
        "(kicad_pcb\n"
        "  (version 20240108)\n"
        '  (generator "test")\n'
        "  (layers\n"
        '    (0 "F.Cu" signal)\n'
        '    (31 "B.Cu" signal)\n'
        '    (44 "Edge.Cuts" user)\n'
        "  )\n"
        '  (net 0 "")\n'
        '  (net 1 "STEPPED")\n'
        '  (gr_rect (start 0 0) (end 50 60) (stroke (width 0.1)) (layer "Edge.Cuts"))\n'
        '  (footprint "Test:Pads" (layer "F.Cu") (at 0 0)\n'
        '    (property "Reference" "J1")\n'
        '    (pad "1" smd rect (at 5 15) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 1 "STEPPED"))\n'
        '    (pad "2" smd rect (at 45 15) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 1 "STEPPED"))\n'
        "  )\n" + "\n".join(f"  {segment}" for segment in segments) + "\n)\n"
    )
    return pcb_file


class TestMeasureNetPath:
    """Pad-to-pad path measurement through the copper-contact graph (#9117)."""

    @pytest.fixture
    def analyzer(self) -> TraceLengthAnalyzer:
        return TraceLengthAnalyzer()

    def test_loop_through_own_pad_is_off_path(self, path_base_pcb: Path, analyzer):
        """The LOOP net's 20 mm out-and-back loop must not count as length."""
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "LOOP")

        assert measurement.pad_count == 2
        assert measurement.path_length_mm == pytest.approx(20.0, abs=0.05)
        assert measurement.off_path_length_mm == pytest.approx(20.0, abs=0.05)

    def test_duplicate_track_does_not_inflate_path(self, path_base_pcb: Path, analyzer):
        """Two identical overlapping runs measure one path, not two."""
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "DUP")

        assert measurement.pad_count == 2
        assert measurement.path_length_mm == pytest.approx(20.0, abs=0.05)
        assert measurement.off_path_length_mm == pytest.approx(20.0, abs=0.05)

    def test_stub_joined_mid_trunk_is_off_path(self, path_base_pcb: Path, analyzer):
        """A stub joined mid-segment splits the trunk at the junction."""
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "STUB")

        assert measurement.pad_count == 2
        assert measurement.path_length_mm == pytest.approx(20.0, abs=0.05)
        assert measurement.off_path_length_mm == pytest.approx(5.0, abs=0.05)

    def test_layer_change_through_via_stays_on_path(self, path_base_pcb: Path, analyzer):
        """A via joining two layers is a zero-cost path node, not off-path."""
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "VIAS")

        assert measurement.pad_count == 2
        assert measurement.path_length_mm == pytest.approx(60.0, abs=0.05)
        assert measurement.off_path_length_mm == pytest.approx(0.0, abs=0.05)
        # The fixture's single via is board.vias[0]; its contact-graph item
        # index is 2 (two pads precede it in the net-scoped items list).
        # Pinning the value keeps path_via_indices in board.vias index space,
        # the documented contract the board-07 validator indexes with.
        assert len(pcb.vias) == 1
        assert measurement.path_via_indices == (0,)

    def test_branched_net_reports_longest_pairwise_path(self, path_base_pcb: Path, analyzer):
        """Multi-pin nets measure the longest pairwise pad-to-pad path.

        Documented behaviour: pads C (35,5) and D (45,5) join through a
        10 mm trunk, and a 20 mm arm leaves the trunk midpoint for pad E
        (40,25).  The longest pair C-E runs 5 mm of trunk plus the arm
        (25 mm); the other 5 mm of trunk to D is off that path.
        """
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "BRANCH")

        assert measurement.pad_count == 3
        assert measurement.path_length_mm == pytest.approx(25.0, abs=0.05)
        assert measurement.off_path_length_mm == pytest.approx(5.0, abs=0.05)

    def test_open_net_has_no_path(self, path_base_pcb: Path, analyzer):
        """An unrouted two-pad net measures no path at all."""
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "OPEN")

        assert measurement.pad_count == 2
        assert measurement.path_length_mm is None
        assert measurement.off_path_length_mm is None

    def test_unknown_net_measures_nothing(self, path_base_pcb: Path, analyzer):
        pcb = PCB.load(str(path_base_pcb))

        assert analyzer.measure_net_path(pcb, "NOPE") == NetPathMeasurement()

    def test_analyze_net_path_fields_opt_in(self, path_base_pcb: Path, analyzer):
        """Path fields stay None unless requested; total keeps its meaning."""
        pcb = PCB.load(str(path_base_pcb))

        plain = analyzer.analyze_net(pcb, "LOOP")
        assert plain.total_length_mm == pytest.approx(40.0, abs=0.05)
        assert plain.path_length_mm is None
        assert plain.off_path_length_mm is None
        assert plain.to_dict().get("path_length_mm") is None

        with_path = analyzer.analyze_net(pcb, "LOOP", with_path_length=True)
        assert with_path.total_length_mm == pytest.approx(40.0, abs=0.05)
        assert with_path.path_length_mm == pytest.approx(20.0, abs=0.05)
        assert with_path.off_path_length_mm == pytest.approx(20.0, abs=0.05)
        assert with_path.to_dict()["path_length_mm"] == pytest.approx(20.0, abs=0.05)
        assert with_path.to_dict()["off_path_length_mm"] == pytest.approx(20.0, abs=0.05)

    @pytest.mark.parametrize(
        "step_mm",
        [2.0, 0.5, 0.2, 30.0 / 212, 0.1],
        ids=["step2.0", "step0.5", "step0.2", "step0.1415", "step0.1"],
    )
    def test_sub_width_segment_encoding_measures_full_run(self, tmp_path: Path, analyzer, step_mm):
        """Segment encoding must not change the measured path (#9117 review).

        Sub-width steps make non-adjacent segments bond through capsule
        contact; the path must still pay the run's real length instead of
        hopping the bonds for free.  With contact links weighted 0.0 the
        0.1415 and 0.1 encodings measured ~0 mm of a 40 mm run.
        """
        pcb = PCB.load(str(stepped_run_pcb(step_mm, tmp_path)))

        measurement = analyzer.measure_net_path(pcb, "STEPPED")

        assert measurement.pad_count == 2
        assert measurement.path_length_mm == pytest.approx(40.0, abs=0.05), step_mm
        assert measurement.off_path_length_mm == pytest.approx(0.0, abs=0.05), step_mm


class TestDefectTrackCopper:
    """Defect-copper classification on the path fixtures (Issue #9304).

    The classifier is one uniform rule per member: dead-end branches and
    terminal-rooted loops whose connected structure exceeds the epsilon
    are defect; rejoined cycle copper (bumps, parallel duplicates) and
    the sub-epsilon escape residual are not.
    """

    @pytest.fixture
    def analyzer(self) -> TraceLengthAnalyzer:
        return TraceLengthAnalyzer()

    def test_defect_classification_is_opt_in(self, path_base_pcb: Path, analyzer):
        """Without ``defect_epsilon_mm`` the field stays ``None``."""
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "LOOP")

        assert measurement.defect_track_mm is None
        assert measurement.defect_structures == 0

    def test_loop_through_own_pad_is_defect(self, path_base_pcb: Path, analyzer):
        """The LOOP net's 20 mm out-and-back is shed as one structure."""
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "LOOP", defect_epsilon_mm=4.0)

        assert measurement.defect_track_mm == pytest.approx(20.0, abs=0.05)
        assert measurement.defect_structures == 1

    def test_dead_end_stub_is_defect(self, path_base_pcb: Path, analyzer):
        """The STUB net's 5 mm mid-trunk dead end is shed."""
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "STUB", defect_epsilon_mm=4.0)

        assert measurement.defect_track_mm == pytest.approx(5.0, abs=0.05)
        assert measurement.defect_structures == 1

    def test_small_stub_below_epsilon_is_kept(self, path_base_pcb: Path, analyzer):
        """A structure under the epsilon stays counted (no new noise).

        The same STUB net measured with a 6 mm epsilon keeps its 5 mm
        dead end in the figure: the correction fires only where the
        off-path warning's epsilon already would.
        """
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "STUB", defect_epsilon_mm=6.0)

        assert measurement.defect_track_mm == pytest.approx(0.0)
        assert measurement.defect_structures == 0

    def test_coincident_duplicate_overlay_is_defect(self, path_base_pcb: Path, analyzer):
        """The DUP net's identical overlapping copy is shed, once.

        Two pieces spanning the same node pair with the same length are
        overlays of one physical connection (the #9138 review's
        collinear stubs): one copy stays counted, the extras are defect.
        A parallel piece with a DIFFERENT length would be a detour and
        stay kept.
        """
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "DUP", defect_epsilon_mm=4.0)

        assert measurement.defect_track_mm == pytest.approx(20.0, abs=0.05)
        assert measurement.defect_structures == 1

    def test_unequal_parallel_detour_is_not_defect(self, tmp_path: Path, analyzer):
        """A longer parallel detour between the same attach points stays.

        Unlike a coincident overlay, a detour spanning the same node
        pair with different length is rejoined cycle copper (a chord
        bump), not a duplicate of the direct run.
        """
        pcb_text = (
            PATH_BASE_PCB.replace(
                '  (net 6 "OPEN")',
                '  (net 6 "OPEN")\n  (net 7 "CHORD")',
            )
            .replace(
                '    (pad "F" smd rect (at 45 55)',
                '    (pad "H" smd rect (at 5 58) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 7 "CHORD"))\n'
                '    (pad "I" smd rect (at 25 58) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 7 "CHORD"))\n'
                '    (pad "F" smd rect (at 45 55)',
            )
            .replace(
                "  # BRANCH:",
                "  # CHORD: 20 mm direct run plus a 30 mm rectangular detour between\n"
                "  # the same attach points (different length: a detour, not an overlay).\n"
                '  (segment (start 5 58) (end 25 58) (width 0.2) (layer "F.Cu") (net 7))\n'
                '  (segment (start 5 58) (end 5 70) (width 0.2) (layer "F.Cu") (net 7))\n'
                '  (segment (start 5 70) (end 25 70) (width 0.2) (layer "F.Cu") (net 7))\n'
                '  (segment (start 25 70) (end 25 58) (width 0.2) (layer "F.Cu") (net 7))\n'
                "\n  # BRANCH:",
            )
        )
        pcb_file = tmp_path / "chord.kicad_pcb"
        pcb_file.write_text(pcb_text)
        pcb = PCB.load(str(pcb_file))

        measurement = analyzer.measure_net_path(pcb, "CHORD", defect_epsilon_mm=4.0)

        assert measurement.defect_track_mm == pytest.approx(0.0)
        assert measurement.defect_structures == 0

    def test_via_layer_change_and_third_pad_branch_are_not_defect(
        self, path_base_pcb: Path, analyzer
    ):
        """Via joints and branches serving another pad carry signal."""
        pcb = PCB.load(str(path_base_pcb))

        for name in ("VIAS", "BRANCH"):
            measurement = analyzer.measure_net_path(pcb, name, defect_epsilon_mm=4.0)
            assert measurement.defect_track_mm == pytest.approx(0.0), name
            assert measurement.defect_structures == 0, name

    def test_open_net_defect_stays_none(self, path_base_pcb: Path, analyzer):
        """An open net has no path model, so no correction either."""
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "OPEN", defect_epsilon_mm=4.0)

        assert measurement.path_length_mm is None
        assert measurement.defect_track_mm is None

    def test_by_layer_split_and_cross_layer_structure(self, tmp_path: Path, analyzer):
        """The shed copper is reported per layer, epsilon once per structure.

        One dead-end branch leaves the trunk on F.Cu, drops through a via
        and keeps going on B.Cu: 3 mm + 3 mm = 6 mm as ONE structure --
        each half alone sits under the 4 mm epsilon, so only the whole
        crosses it -- shed as a unit and split ``{"F.Cu": 3, "B.Cu": 3}``
        so a per-layer-delay consumer charges each piece where it sits
        (Issue #9767).
        """
        pcb_text = (
            PATH_BASE_PCB.replace(
                '  (net 6 "OPEN")',
                '  (net 6 "OPEN")\n  (net 8 "XCROSS")',
            )
            .replace(
                '    (pad "F" smd rect (at 45 55)',
                '    (pad "H" smd rect (at 5 55) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 8 "XCROSS"))\n'
                '    (pad "I" smd rect (at 25 55) (size 1.0 1.0) (layers "F.Cu" "F.Mask") (net 8 "XCROSS"))\n'
                '    (pad "F" smd rect (at 45 55)',
            )
            .replace(
                "  # BRANCH:",
                "  # XCROSS: 20 mm trunk with a 3 mm F.Cu stub through a via onto a\n"
                "  # 3 mm B.Cu dead end -- one cross-layer structure of 6 mm total.\n"
                '  (segment (start 5 55) (end 25 55) (width 0.2) (layer "F.Cu") (net 8))\n'
                '  (segment (start 15 55) (end 15 52) (width 0.2) (layer "F.Cu") (net 8))\n'
                '  (via (at 15 52) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 8))\n'
                '  (segment (start 15 52) (end 15 49) (width 0.2) (layer "B.Cu") (net 8))\n'
                "\n  # BRANCH:",
            )
        )
        pcb_file = tmp_path / "xcross.kicad_pcb"
        pcb_file.write_text(pcb_text)
        pcb = PCB.load(str(pcb_file))

        measurement = analyzer.measure_net_path(pcb, "XCROSS", defect_epsilon_mm=4.0)

        assert measurement.defect_track_mm == pytest.approx(6.0, abs=0.1)
        assert measurement.defect_structures == 1
        assert set(measurement.defect_track_by_layer or {}) == {"F.Cu", "B.Cu"}
        assert (measurement.defect_track_by_layer or {})["F.Cu"] == pytest.approx(3.0, abs=0.1)
        assert (measurement.defect_track_by_layer or {})["B.Cu"] == pytest.approx(3.0, abs=0.1)

    def test_by_layer_is_none_without_classification(self, path_base_pcb: Path, analyzer):
        """``defect_track_by_layer`` follows ``defect_track_mm``'s None."""
        pcb = PCB.load(str(path_base_pcb))

        measurement = analyzer.measure_net_path(pcb, "STUB")

        assert measurement.defect_track_mm is None
        assert measurement.defect_track_by_layer is None


class TestContactGraphBonds:
    """``bonds`` under ``with_bonds=True`` is the full edge set (#9117)."""

    def test_with_bonds_records_cycle_closing_edges(self, path_base_pcb: Path):
        """Every bonded pair is recorded, not only a spanning forest.

        The DUP net's four items (two pads, two identical overlapping
        tracks) are pairwise bonded everywhere except pad-pad: 5 real
        edges over 4 items.  A union-find shortcut would keep only
        items - 1 = 3 of them, silently dropping exactly the
        cycle-closing edges the pad-to-pad path measurement needs.
        """
        from kicad_tools.validate.connectivity import ConnectivityValidator
        from kicad_tools.validate.copper_contact import build_copper_contact_graph

        pcb = PCB.load(str(path_base_pcb))
        cv = ConnectivityValidator(pcb)
        net = pcb.get_net_by_name("DUP")
        assert net is not None

        graph = build_copper_contact_graph(cv, net.number, with_bonds=True)

        assert len(graph.items) == 4
        assert len(graph.components()) == 1
        assert len(graph.bonds) == 5

        plain = build_copper_contact_graph(cv, net.number)
        assert plain.components() == graph.components()
        assert plain.bonds == []
