"""Tests for net connectivity status analysis and CLI command."""

import json
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import (
    NetStatus,
    NetStatusAnalyzer,
    NetStatusResult,
    PadInfo,
)
from kicad_tools.cli.net_status_cmd import main as net_status_main

# PCB with fully routed nets
FULLY_ROUTED_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp2")
    (at 110 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (segment (start 99.49 100) (end 109.49 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg1"))
  (segment (start 100.51 100) (end 110.51 100) (width 0.25) (layer "F.Cu") (net 2) (uuid "seg2"))
)
"""


# PCB with incomplete routing (GND net only has one segment)
INCOMPLETE_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "UNROUTED")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp2")
    (at 110 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp3")
    (at 120 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref3"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "UNROUTED"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "UNROUTED"))
  )
  (segment (start 99.49 100) (end 109.49 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg1"))
)
"""


# PCB with via-to-zone connectivity (Issue #419)
# Tests that pads connected to zones via vias are recognized as connected
VIA_ZONE_CONNECTIVITY_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp1")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (pad "1" smd roundrect (at 0 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp2")
    (at 150 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (pad "1" smd roundrect (at 0 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (segment (start 100 100) (end 105 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg1"))
  (segment (start 150 100) (end 145 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg2"))
  (via (at 105 100) (size 0.8) (drill 0.4) (layers "F.Cu" "In1.Cu") (net 1) (uuid "via1"))
  (via (at 145 100) (size 0.8) (drill 0.4) (layers "F.Cu" "In1.Cu") (net 1) (uuid "via2"))
  (zone (net 1) (net_name "GND") (layer "In1.Cu")
    (uuid "zone1")
    (connect_pads (clearance 0.3))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 90 90) (xy 160 90) (xy 160 110) (xy 90 110)))
    (filled_polygon (layer "In1.Cu") (pts (xy 90 90) (xy 160 90) (xy 160 110) (xy 90 110)))
  )
)
"""


# PCB with zone (plane net)
ZONE_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp1")
    (at 110 110)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp2")
    (at 120 110)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref2"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (zone (net 1) (net_name "GND") (layer "B.Cu")
    (uuid "zone1")
    (connect_pads (clearance 0.3))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 140 100) (xy 140 130) (xy 100 130)))
    (filled_polygon (layer "B.Cu") (pts (xy 100 100) (xy 140 100) (xy 140 130) (xy 100 130)))
  )
)
"""


@pytest.fixture
def fully_routed_pcb(tmp_path: Path) -> Path:
    """Create a fully routed PCB file for testing."""
    pcb_file = tmp_path / "fully_routed.kicad_pcb"
    pcb_file.write_text(FULLY_ROUTED_PCB)
    return pcb_file


@pytest.fixture
def incomplete_pcb(tmp_path: Path) -> Path:
    """Create an incomplete PCB file for testing."""
    pcb_file = tmp_path / "incomplete.kicad_pcb"
    pcb_file.write_text(INCOMPLETE_PCB)
    return pcb_file


@pytest.fixture
def zone_pcb(tmp_path: Path) -> Path:
    """Create a PCB with zones for testing."""
    pcb_file = tmp_path / "zone.kicad_pcb"
    pcb_file.write_text(ZONE_PCB)
    return pcb_file


@pytest.fixture
def via_zone_connectivity_pcb(tmp_path: Path) -> Path:
    """Create a PCB with via-to-zone connectivity for testing (Issue #419)."""
    pcb_file = tmp_path / "via_zone.kicad_pcb"
    pcb_file.write_text(VIA_ZONE_CONNECTIVITY_PCB)
    return pcb_file


class TestPadInfo:
    """Tests for PadInfo dataclass."""

    def test_full_name(self):
        """Test full_name property."""
        pad = PadInfo(
            reference="R1",
            pad_number="2",
            position=(100.0, 100.0),
            is_connected=True,
        )
        assert pad.full_name == "R1.2"

    def test_position(self):
        """Test position storage."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            position=(50.5, 75.25),
            is_connected=False,
        )
        assert pad.position == (50.5, 75.25)


class TestNetStatus:
    """Tests for NetStatus dataclass."""

    def test_complete_net(self):
        """Test a fully connected net."""
        net = NetStatus(
            net_number=1,
            net_name="GND",
            total_pads=4,
            connected_pads=[
                PadInfo("R1", "1", (100, 100), True),
                PadInfo("R2", "1", (110, 100), True),
                PadInfo("C1", "1", (120, 100), True),
                PadInfo("C2", "1", (130, 100), True),
            ],
            unconnected_pads=[],
        )
        assert net.status == "complete"
        assert net.connected_count == 4
        assert net.unconnected_count == 0
        assert net.connection_percentage == 100.0

    def test_incomplete_net(self):
        """Test a partially connected net."""
        net = NetStatus(
            net_number=2,
            net_name="+3.3V",
            total_pads=3,
            connected_pads=[
                PadInfo("R1", "2", (100.51, 100), True),
                PadInfo("R2", "2", (110.51, 100), True),
            ],
            unconnected_pads=[
                PadInfo("C1", "1", (120.51, 100), False),
            ],
        )
        assert net.status == "incomplete"
        assert net.connected_count == 2
        assert net.unconnected_count == 1
        assert net.connection_percentage == pytest.approx(66.67, rel=0.01)

    def test_unrouted_net(self):
        """Test a net with no routing."""
        net = NetStatus(
            net_number=3,
            net_name="SIGNAL",
            total_pads=2,
            connected_pads=[],
            unconnected_pads=[
                PadInfo("U1", "5", (100, 100), False),
                PadInfo("U2", "3", (150, 100), False),
            ],
        )
        assert net.status == "unrouted"
        assert net.connected_count == 0
        assert net.unconnected_count == 2

    def test_single_pad_net_complete(self):
        """Test a single-pad net is always complete."""
        net = NetStatus(
            net_number=4,
            net_name="NC",
            total_pads=1,
            connected_pads=[
                PadInfo("U1", "NC", (100, 100), True),
            ],
            unconnected_pads=[],
        )
        assert net.status == "complete"

    def test_plane_net(self):
        """Test plane net identification."""
        net = NetStatus(
            net_number=1,
            net_name="GND",
            is_plane_net=True,
            plane_layer="B.Cu",
        )
        assert net.net_type == "plane"
        assert net.plane_layer == "B.Cu"

    def test_power_net_identification(self):
        """Test power net identification from name patterns."""
        for name in ["+3.3V", "-5V", "VCC", "VDD", "GND", "AGND"]:
            net = NetStatus(net_number=1, net_name=name)
            assert net.net_type == "power", f"{name} should be identified as power"

    def test_signal_net_identification(self):
        """Test signal net identification."""
        net = NetStatus(net_number=1, net_name="SPI_CLK")
        assert net.net_type == "signal"

    def test_to_dict(self):
        """Test serialization to dictionary."""
        net = NetStatus(
            net_number=1,
            net_name="GND",
            total_pads=2,
            connected_pads=[PadInfo("R1", "1", (100, 100), True)],
            unconnected_pads=[PadInfo("R2", "1", (110, 100), False)],
            is_plane_net=True,
            plane_layer="B.Cu",
        )
        d = net.to_dict()
        assert d["net_name"] == "GND"
        assert d["status"] == "incomplete"
        assert d["is_plane_net"] is True
        assert d["plane_layer"] == "B.Cu"
        assert len(d["connected_pads"]) == 1
        assert len(d["unconnected_pads"]) == 1


class TestNetStatusResult:
    """Tests for NetStatusResult dataclass."""

    def test_counts(self):
        """Test result count properties."""
        result = NetStatusResult(
            nets=[
                NetStatus(
                    1,
                    "GND",
                    total_pads=2,
                    connected_pads=[
                        PadInfo("R1", "1", (100, 100), True),
                        PadInfo("R2", "1", (110, 100), True),
                    ],
                    unconnected_pads=[],
                ),
                NetStatus(
                    2,
                    "+3.3V",
                    total_pads=2,
                    connected_pads=[
                        PadInfo("R1", "2", (100.51, 100), True),
                    ],
                    unconnected_pads=[
                        PadInfo("R2", "2", (110.51, 100), False),
                    ],
                ),
                NetStatus(
                    3,
                    "SIG",
                    total_pads=2,
                    connected_pads=[],
                    unconnected_pads=[
                        PadInfo("U1", "1", (100, 100), False),
                        PadInfo("U2", "1", (110, 100), False),
                    ],
                ),
            ],
            total_nets=3,
        )
        assert result.complete_count == 1
        assert result.incomplete_count == 1
        assert result.unrouted_count == 1
        assert result.total_unconnected_pads == 3

    def test_get_net(self):
        """Test getting a specific net by name."""
        result = NetStatusResult(
            nets=[
                NetStatus(1, "GND"),
                NetStatus(2, "+3.3V"),
            ],
            total_nets=2,
        )
        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.net_name == "GND"
        assert result.get_net("NONEXISTENT") is None

    def test_summary(self):
        """Test summary generation."""
        result = NetStatusResult(total_nets=5)
        summary = result.summary()
        assert "5 nets" in summary


class TestNetStatusAnalyzer:
    """Tests for NetStatusAnalyzer class."""

    def test_analyze_fully_routed(self, fully_routed_pcb: Path):
        """Test analysis of fully routed PCB."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)
        result = analyzer.analyze()

        assert result.total_nets == 2  # GND and +3.3V
        assert result.incomplete_count == 0
        assert result.unrouted_count == 0
        assert result.complete_count == 2

    def test_analyze_incomplete(self, incomplete_pcb: Path):
        """Test analysis of PCB with incomplete routing."""
        analyzer = NetStatusAnalyzer(incomplete_pcb)
        result = analyzer.analyze()

        assert result.total_nets == 3  # GND, +3.3V, UNROUTED

        # GND is connected via segment
        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.status == "complete"

        # +3.3V has no routing, should be unrouted or incomplete
        v33 = result.get_net("+3.3V")
        assert v33 is not None
        assert v33.status in ("unrouted", "incomplete")  # No segments for this net

        # UNROUTED net has no routing, may be classified as unrouted or incomplete
        unrouted = result.get_net("UNROUTED")
        assert unrouted is not None
        assert unrouted.status in ("unrouted", "incomplete")  # No segments for this net

    def test_analyze_zone_pcb(self, zone_pcb: Path):
        """Test analysis identifies plane nets."""
        analyzer = NetStatusAnalyzer(zone_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.is_plane_net is True
        assert gnd.plane_layer == "B.Cu"

    def test_unconnected_pads_have_positions(self, incomplete_pcb: Path):
        """Test that unconnected pads include position information."""
        analyzer = NetStatusAnalyzer(incomplete_pcb)
        result = analyzer.analyze()

        v33 = result.get_net("+3.3V")
        assert v33 is not None
        assert len(v33.unconnected_pads) > 0

        for pad in v33.unconnected_pads:
            assert pad.position is not None
            assert isinstance(pad.position, tuple)
            assert len(pad.position) == 2

    def test_via_zone_connectivity(self, via_zone_connectivity_pcb: Path):
        """Test that pads connected to zones via vias are recognized as connected.

        Issue #419: net-status doesn't detect via connectivity to planes.

        This test creates a PCB with:
        - Two pads on F.Cu (R1.1 at 100,100 and R2.1 at 150,100)
        - Traces from each pad to vias
        - Vias spanning F.Cu to In1.Cu
        - A GND zone on In1.Cu covering both via positions

        Both pads should be recognized as connected because they both connect
        to the zone via their respective vias (stitching vias pattern).
        """
        analyzer = NetStatusAnalyzer(via_zone_connectivity_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None, "GND net should exist"
        assert gnd.is_plane_net is True, "GND should be identified as plane net"
        assert gnd.status == "complete", (
            f"GND should be complete (pads connected via zone), "
            f"but found {gnd.unconnected_count} unconnected pads: "
            f"{[p.full_name for p in gnd.unconnected_pads]}"
        )
        assert gnd.total_pads == 2, "GND should have 2 pads"
        assert gnd.connected_count == 2, "Both pads should be connected via zone"
        assert gnd.unconnected_count == 0, "No pads should be unconnected"


class TestNetStatusCLI:
    """Tests for net-status CLI command."""

    def test_basic_usage(self, fully_routed_pcb: Path):
        """Test basic command execution."""
        exit_code = net_status_main([str(fully_routed_pcb)])
        assert exit_code == 0  # All complete

    def test_exit_code_incomplete(self, incomplete_pcb: Path):
        """Test exit code when incomplete nets exist."""
        exit_code = net_status_main([str(incomplete_pcb)])
        assert exit_code == 2  # Incomplete nets found

    def test_json_format(self, fully_routed_pcb: Path, capsys):
        """Test JSON output format."""
        exit_code = net_status_main([str(fully_routed_pcb), "--format", "json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "summary" in data
        assert "nets" in data
        assert data["summary"]["total_nets"] == 2

    def test_incomplete_filter(self, incomplete_pcb: Path, capsys):
        """Test --incomplete filter."""
        exit_code = net_status_main([str(incomplete_pcb), "--incomplete"])
        assert exit_code == 2

        captured = capsys.readouterr()
        # Should only show incomplete/unrouted nets
        assert "+3.3V" in captured.out or "UNROUTED" in captured.out

    def test_specific_net(self, incomplete_pcb: Path, capsys):
        """Test --net filter for specific net."""
        _exit_code = net_status_main([str(incomplete_pcb), "--net", "GND"])

        captured = capsys.readouterr()
        assert "GND" in captured.out

    def test_nonexistent_net(self, fully_routed_pcb: Path):
        """Test error when net doesn't exist."""
        exit_code = net_status_main([str(fully_routed_pcb), "--net", "DOES_NOT_EXIST"])
        assert exit_code == 1  # Error

    def test_nonexistent_file(self, tmp_path: Path):
        """Test error when file doesn't exist."""
        exit_code = net_status_main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert exit_code == 1

    def test_verbose_mode(self, incomplete_pcb: Path, capsys):
        """Test verbose output shows all pads."""
        _exit_code = net_status_main([str(incomplete_pcb), "--verbose"])

        captured = capsys.readouterr()
        # Verbose should show coordinate details
        assert "@" in captured.out or "(" in captured.out

    def test_by_class_grouping(self, fully_routed_pcb: Path, capsys):
        """Test --by-class grouping option."""
        exit_code = net_status_main([str(fully_routed_pcb), "--by-class"])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "Net Class:" in captured.out


class TestNetStatusIntegration:
    """Integration tests for net status functionality."""

    def test_unified_cli_access(self, fully_routed_pcb: Path):
        """Test access via unified kicad-tools CLI."""
        from kicad_tools.cli import main

        exit_code = main(["net-status", str(fully_routed_pcb)])
        assert exit_code == 0

    def test_json_serialization_roundtrip(self, incomplete_pcb: Path):
        """Test that JSON output can be parsed back."""
        analyzer = NetStatusAnalyzer(incomplete_pcb)
        result = analyzer.analyze()

        # Serialize to JSON
        data = result.to_dict()
        json_str = json.dumps(data)

        # Parse back
        parsed = json.loads(json_str)
        assert parsed["total_nets"] == result.total_nets
        assert len(parsed["nets"]) == len(result.nets)
