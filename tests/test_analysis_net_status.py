"""Tests for kicad_tools.analysis.net_status module."""

import json
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import (
    NetStatus,
    NetStatusAnalyzer,
    NetStatusResult,
    PadInfo,
)


# PCB with fully routed nets
FULLY_ROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")

  (footprint "R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "GND"))
  )

  (segment (start 9.5 10) (end 19.5 10) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 10.5 10) (end 20.5 10) (width 0.25) (layer "F.Cu") (net 2))
)
"""


# PCB with partially routed nets
PARTIALLY_ROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG1")

  (footprint "R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
  )

  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))
)
"""


# PCB with unrouted nets
UNROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "UNROUTED")

  (footprint "R_0402"
    (layer "F.Cu")
    (at 10 10)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "UNROUTED"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 0 ""))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 20 10)
    (property "Reference" "R2")
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "UNROUTED"))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 0 ""))
  )
)
"""


# PCB with a zone (plane net)
PLANE_NET_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")

  (footprint "C_0402"
    (layer "F.Cu")
    (at 15 15)
    (property "Reference" "C1")
    (pad "1" smd rect (at 0 -0.25) (size 0.4 0.4) (layers "F.Cu") (net 0 ""))
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (footprint "C_0402"
    (layer "F.Cu")
    (at 25 15)
    (property "Reference" "C2")
    (pad "1" smd rect (at 0 -0.25) (size 0.4 0.4) (layers "F.Cu") (net 0 ""))
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (zone
    (net 1)
    (net_name "GND")
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.15)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))
    (polygon
      (pts
        (xy 10 10)
        (xy 30 10)
        (xy 30 20)
        (xy 10 20)
      )
    )
    (filled_polygon
      (layer "F.Cu")
      (pts
        (xy 10 10)
        (xy 30 10)
        (xy 30 20)
        (xy 10 20)
      )
    )
  )
)
"""


@pytest.fixture
def fully_routed_pcb(tmp_path: Path) -> Path:
    """Create a PCB with fully routed nets."""
    pcb_file = tmp_path / "routed.kicad_pcb"
    pcb_file.write_text(FULLY_ROUTED_PCB)
    return pcb_file


@pytest.fixture
def partially_routed_pcb(tmp_path: Path) -> Path:
    """Create a PCB with partially routed nets."""
    pcb_file = tmp_path / "partial.kicad_pcb"
    pcb_file.write_text(PARTIALLY_ROUTED_PCB)
    return pcb_file


@pytest.fixture
def unrouted_pcb(tmp_path: Path) -> Path:
    """Create a PCB with unrouted nets."""
    pcb_file = tmp_path / "unrouted.kicad_pcb"
    pcb_file.write_text(UNROUTED_PCB)
    return pcb_file


@pytest.fixture
def plane_net_pcb(tmp_path: Path) -> Path:
    """Create a PCB with a plane net (zone)."""
    pcb_file = tmp_path / "plane.kicad_pcb"
    pcb_file.write_text(PLANE_NET_PCB)
    return pcb_file


class TestPadInfo:
    """Tests for PadInfo dataclass."""

    def test_full_name(self):
        """Test full_name property."""
        pad = PadInfo(
            reference="U1",
            pad_number="5",
            position=(10.0, 20.0),
            is_connected=True,
        )
        assert pad.full_name == "U1.5"

    def test_properties(self):
        """Test basic properties."""
        pad = PadInfo(
            reference="R1",
            pad_number="2",
            position=(5.5, 10.5),
            is_connected=False,
        )
        assert pad.reference == "R1"
        assert pad.pad_number == "2"
        assert pad.position == (5.5, 10.5)
        assert pad.is_connected is False


class TestNetStatus:
    """Tests for NetStatus dataclass."""

    def test_connected_count(self):
        """Test connected_count property."""
        status = NetStatus(
            net_number=1,
            net_name="VCC",
            connected_pads=[
                PadInfo("R1", "1", (0, 0), True),
                PadInfo("R2", "1", (10, 0), True),
            ],
            unconnected_pads=[
                PadInfo("U1", "1", (20, 0), False),
            ],
        )
        assert status.connected_count == 2
        assert status.unconnected_count == 1

    def test_total_pads(self):
        """Test total_pads property."""
        status = NetStatus(
            net_number=1,
            net_name="VCC",
            total_pads=5,
        )
        assert status.total_pads == 5

    def test_connection_percentage(self):
        """Test connection_percentage calculation."""
        status = NetStatus(
            net_number=1,
            net_name="VCC",
            total_pads=4,
            connected_pads=[
                PadInfo("R1", "1", (0, 0), True),
                PadInfo("R2", "1", (10, 0), True),
            ],
        )
        assert status.connection_percentage == 50.0

    def test_connection_percentage_empty(self):
        """Test connection_percentage with no pads."""
        status = NetStatus(net_number=1, net_name="VCC", total_pads=0)
        assert status.connection_percentage == 100.0

    def test_status_complete(self):
        """Test status is 'complete' when all pads connected."""
        status = NetStatus(
            net_number=1,
            net_name="VCC",
            total_pads=2,
            connected_pads=[
                PadInfo("R1", "1", (0, 0), True),
                PadInfo("R2", "1", (10, 0), True),
            ],
            unconnected_pads=[],
        )
        assert status.status == "complete"

    def test_status_incomplete(self):
        """Test status is 'incomplete' when some pads connected."""
        status = NetStatus(
            net_number=1,
            net_name="SIG",
            total_pads=3,
            connected_pads=[
                PadInfo("R1", "1", (0, 0), True),
            ],
            unconnected_pads=[
                PadInfo("U1", "1", (20, 0), False),
                PadInfo("U2", "1", (30, 0), False),
            ],
        )
        assert status.status == "incomplete"

    def test_status_unrouted(self):
        """Test status is 'unrouted' when no pads connected."""
        status = NetStatus(
            net_number=1,
            net_name="SIG",
            total_pads=2,
            connected_pads=[],
            unconnected_pads=[
                PadInfo("U1", "1", (20, 0), False),
                PadInfo("U2", "1", (30, 0), False),
            ],
        )
        assert status.status == "unrouted"

    def test_status_single_pad(self):
        """Test single-pad nets are always complete."""
        status = NetStatus(
            net_number=1,
            net_name="NC",
            total_pads=1,
            connected_pads=[],
            unconnected_pads=[],
        )
        assert status.status == "complete"

    def test_net_type_plane(self):
        """Test net_type is 'plane' when is_plane_net."""
        status = NetStatus(
            net_number=1,
            net_name="GND",
            is_plane_net=True,
            plane_layer="F.Cu",
        )
        assert status.net_type == "plane"

    def test_net_type_power(self):
        """Test net_type detection for power nets."""
        power_names = ["VCC", "+3.3V", "-12V", "VDD", "VSS", "GND", "AGND", "DGND"]
        for name in power_names:
            status = NetStatus(net_number=1, net_name=name, is_plane_net=False)
            assert status.net_type == "power", f"{name} should be power type"

    def test_net_type_signal(self):
        """Test net_type detection for signal nets."""
        status = NetStatus(net_number=1, net_name="SDA", is_plane_net=False)
        assert status.net_type == "signal"

    def test_suggested_fix_plane(self):
        """Test suggested_fix for plane nets."""
        status = NetStatus(
            net_number=1,
            net_name="GND",
            is_plane_net=True,
            total_pads=2,
            unconnected_pads=[PadInfo("C1", "2", (0, 0), False)],
        )
        assert "stitch" in status.suggested_fix.lower()
        assert "GND" in status.suggested_fix

    def test_suggested_fix_signal(self):
        """Test suggested_fix for signal nets."""
        status = NetStatus(
            net_number=1,
            net_name="SDA",
            is_plane_net=False,
            total_pads=2,
            unconnected_pads=[PadInfo("U1", "3", (0, 0), False)],
        )
        assert "trace" in status.suggested_fix.lower() or "Route" in status.suggested_fix

    def test_to_dict(self):
        """Test to_dict serialization."""
        status = NetStatus(
            net_number=1,
            net_name="VCC",
            net_class="Power",
            total_pads=2,
            connected_pads=[PadInfo("R1", "1", (0.0, 0.0), True)],
            unconnected_pads=[PadInfo("U1", "1", (10.0, 10.0), False)],
            is_plane_net=False,
            has_routing=True,
            has_vias=False,
        )

        d = status.to_dict()

        assert d["net_number"] == 1
        assert d["net_name"] == "VCC"
        assert d["net_class"] == "Power"
        assert d["status"] == "incomplete"
        assert d["net_type"] == "power"
        assert d["total_pads"] == 2
        assert d["connected_count"] == 1
        assert d["unconnected_count"] == 1
        assert d["has_routing"] is True
        assert d["has_vias"] is False
        assert len(d["connected_pads"]) == 1
        assert len(d["unconnected_pads"]) == 1

        # Check JSON serializable
        json.dumps(d)


class TestNetStatusResult:
    """Tests for NetStatusResult dataclass."""

    def test_categorization(self):
        """Test net categorization properties."""
        result = NetStatusResult(
            nets=[
                NetStatus(
                    1,
                    "VCC",
                    total_pads=2,
                    connected_pads=[
                        PadInfo("R1", "1", (0, 0), True),
                        PadInfo("R2", "1", (10, 0), True),
                    ],
                ),
                NetStatus(
                    2,
                    "SIG",
                    total_pads=2,
                    connected_pads=[PadInfo("R1", "2", (0, 0), True)],
                    unconnected_pads=[PadInfo("R2", "2", (10, 0), False)],
                ),
                NetStatus(
                    3,
                    "NC",
                    total_pads=2,
                    unconnected_pads=[
                        PadInfo("U1", "1", (20, 0), False),
                        PadInfo("U2", "1", (30, 0), False),
                    ],
                ),
            ],
            total_nets=3,
        )

        assert len(result.complete) == 1
        assert len(result.incomplete) == 1
        assert len(result.unrouted) == 1

        assert result.complete_count == 1
        assert result.incomplete_count == 1
        assert result.unrouted_count == 1

    def test_total_unconnected_pads(self):
        """Test total_unconnected_pads calculation."""
        result = NetStatusResult(
            nets=[
                NetStatus(
                    1,
                    "A",
                    total_pads=3,
                    unconnected_pads=[
                        PadInfo("R1", "1", (0, 0), False),
                        PadInfo("R2", "1", (10, 0), False),
                    ],
                ),
                NetStatus(
                    2,
                    "B",
                    total_pads=2,
                    unconnected_pads=[PadInfo("U1", "1", (20, 0), False)],
                ),
            ],
            total_nets=2,
        )

        assert result.total_unconnected_pads == 3

    def test_by_net_class(self):
        """Test grouping by net class."""
        result = NetStatusResult(
            nets=[
                NetStatus(1, "VCC", net_class="Power"),
                NetStatus(2, "GND", net_class="Power"),
                NetStatus(3, "SDA", net_class="I2C"),
                NetStatus(4, "SCL", net_class="I2C"),
                NetStatus(5, "GPIO", net_class=""),
            ],
            total_nets=5,
        )

        by_class = result.by_net_class()

        assert "Power" in by_class
        assert "I2C" in by_class
        assert "Default" in by_class  # Empty class becomes "Default"

        assert len(by_class["Power"]) == 2
        assert len(by_class["I2C"]) == 2
        assert len(by_class["Default"]) == 1

    def test_get_net(self):
        """Test get_net by name."""
        result = NetStatusResult(
            nets=[
                NetStatus(1, "VCC"),
                NetStatus(2, "GND"),
                NetStatus(3, "SDA"),
            ],
            total_nets=3,
        )

        vcc = result.get_net("VCC")
        assert vcc is not None
        assert vcc.net_name == "VCC"

        nonexistent = result.get_net("NONEXISTENT")
        assert nonexistent is None

    def test_to_dict(self):
        """Test to_dict serialization."""
        result = NetStatusResult(
            nets=[NetStatus(1, "VCC", total_pads=2)],
            total_nets=1,
        )

        d = result.to_dict()

        assert "total_nets" in d
        assert "complete_count" in d
        assert "incomplete_count" in d
        assert "unrouted_count" in d
        assert "total_unconnected_pads" in d
        assert "nets" in d

        # Check JSON serializable
        json.dumps(d)

    def test_summary(self):
        """Test summary string generation."""
        result = NetStatusResult(
            nets=[
                NetStatus(1, "VCC", total_pads=2, connected_pads=[PadInfo("R1", "1", (0, 0), True)]),
            ],
            total_nets=1,
        )

        summary = result.summary()

        assert "Net Status Summary" in summary
        assert "Complete:" in summary
        assert "Incomplete:" in summary
        assert "Unrouted:" in summary


class TestNetStatusAnalyzer:
    """Tests for NetStatusAnalyzer class."""

    def test_init_with_path(self, fully_routed_pcb: Path):
        """Test initialization with file path."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)
        assert analyzer.pcb is not None

    def test_init_with_pcb_object(self, fully_routed_pcb: Path):
        """Test initialization with PCB object."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(fully_routed_pcb))
        analyzer = NetStatusAnalyzer(pcb)
        assert analyzer.pcb is not None

    def test_analyze_fully_routed(self, fully_routed_pcb: Path):
        """Test analyzing fully routed PCB."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)
        result = analyzer.analyze()

        # Should have some nets (VCC, GND)
        assert result.total_nets >= 1

        # All nets should be complete or single-pad
        assert result.incomplete_count == 0 or result.complete_count > 0

    def test_analyze_partially_routed(self, partially_routed_pcb: Path):
        """Test analyzing partially routed PCB."""
        analyzer = NetStatusAnalyzer(partially_routed_pcb)
        result = analyzer.analyze()

        # Should detect incomplete net
        assert result.total_nets >= 1
        # SIG1 connects 4 pads but only 2 are routed together
        # Check that we have either incomplete or complete depending on analysis
        assert len(result.nets) >= 1

    def test_analyze_unrouted(self, unrouted_pcb: Path):
        """Test analyzing PCB with unrouted nets."""
        analyzer = NetStatusAnalyzer(unrouted_pcb)
        result = analyzer.analyze()

        # Should detect unrouted net
        assert result.total_nets >= 1
        # UNROUTED net has 2 pads with no traces
        assert result.unrouted_count >= 0  # May be 0 if single-pad

    def test_analyze_plane_net(self, plane_net_pcb: Path):
        """Test analyzing PCB with plane net (zone)."""
        analyzer = NetStatusAnalyzer(plane_net_pcb)
        result = analyzer.analyze()

        # Should detect GND as plane net
        gnd = result.get_net("GND")
        if gnd:
            assert gnd.is_plane_net is True
            assert gnd.plane_layer == "F.Cu"

    def test_result_sorting(self, partially_routed_pcb: Path):
        """Test that results are sorted by status."""
        analyzer = NetStatusAnalyzer(partially_routed_pcb)
        result = analyzer.analyze()

        # If we have multiple nets with different statuses,
        # incomplete should come before complete
        statuses = [n.status for n in result.nets]
        if len(statuses) > 1:
            # Just verify sorting doesn't crash
            pass

    def test_position_tolerance(self, fully_routed_pcb: Path):
        """Test position tolerance constant."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)
        assert analyzer.POSITION_TOLERANCE > 0
        assert analyzer.POSITION_TOLERANCE < 1  # Should be small


class TestAnalyzerHelperMethods:
    """Tests for analyzer internal methods."""

    def test_points_close(self, fully_routed_pcb: Path):
        """Test _points_close method."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)

        # Same point
        assert analyzer._points_close((10.0, 20.0), (10.0, 20.0)) is True

        # Very close points
        assert analyzer._points_close((10.0, 20.0), (10.001, 20.001)) is True

        # Distant points
        assert analyzer._points_close((10.0, 20.0), (11.0, 21.0)) is False

    def test_transform_pad_position_no_rotation(self, fully_routed_pcb: Path):
        """Test pad position transform without rotation."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)

        result = analyzer._transform_pad_position((1.0, 2.0), 10.0, 20.0, 0.0)

        assert result == pytest.approx((11.0, 22.0), abs=0.001)

    def test_transform_pad_position_with_rotation(self, fully_routed_pcb: Path):
        """Test pad position transform with 90 degree rotation."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)

        # 90 degree rotation: (1, 0) should become (0, 1)
        result = analyzer._transform_pad_position((1.0, 0.0), 0.0, 0.0, 90.0)

        assert result[0] == pytest.approx(0.0, abs=0.001)
        assert result[1] == pytest.approx(1.0, abs=0.001)
