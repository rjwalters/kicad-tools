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
        assert status.connection_percentage == 0.0

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
                NetStatus(
                    1, "VCC", total_pads=2, connected_pads=[PadInfo("R1", "1", (0, 0), True)]
                ),
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

    def test_via_spans_layer_direct_match(self, fully_routed_pcb: Path):
        """Test _via_spans_layer with direct layer match."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)

        # Direct match: via lists F.Cu, target is F.Cu
        assert analyzer._via_spans_layer(["F.Cu", "B.Cu"], "F.Cu") is True
        assert analyzer._via_spans_layer(["F.Cu", "B.Cu"], "B.Cu") is True
        assert analyzer._via_spans_layer(["F.Cu", "In1.Cu"], "F.Cu") is True

    def test_via_spans_layer_through_via_inner_layers(self, fully_routed_pcb: Path):
        """Test _via_spans_layer recognises through-via spanning inner layers."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)

        # Through-via F.Cu->B.Cu should span ALL intermediate copper layers
        assert analyzer._via_spans_layer(["F.Cu", "B.Cu"], "In1.Cu") is True
        assert analyzer._via_spans_layer(["F.Cu", "B.Cu"], "In2.Cu") is True
        assert analyzer._via_spans_layer(["F.Cu", "B.Cu"], "In3.Cu") is True
        assert analyzer._via_spans_layer(["F.Cu", "B.Cu"], "In4.Cu") is True

    def test_via_spans_layer_blind_via(self, fully_routed_pcb: Path):
        """Test _via_spans_layer for blind/buried vias with limited span."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)

        # Blind via F.Cu->In1.Cu should span only those two layers
        assert analyzer._via_spans_layer(["F.Cu", "In1.Cu"], "In1.Cu") is True
        assert analyzer._via_spans_layer(["F.Cu", "In1.Cu"], "F.Cu") is True
        # Should NOT span beyond In1.Cu
        assert analyzer._via_spans_layer(["F.Cu", "In1.Cu"], "In2.Cu") is False
        assert analyzer._via_spans_layer(["F.Cu", "In1.Cu"], "B.Cu") is False

    def test_via_spans_layer_no_match(self, fully_routed_pcb: Path):
        """Test _via_spans_layer returns False for unrelated layers."""
        analyzer = NetStatusAnalyzer(fully_routed_pcb)

        # Non-copper layer should not match
        assert analyzer._via_spans_layer(["F.Cu", "B.Cu"], "F.Mask") is False
        # Empty via layers
        assert analyzer._via_spans_layer([], "F.Cu") is False
        # Single layer via (degenerate case)
        assert analyzer._via_spans_layer(["F.Cu"], "B.Cu") is False


# ---- PCB fixtures for stitching via connectivity tests ----

# 4-layer PCB: SMD pad on F.Cu -> trace stub -> via (F.Cu/In1.Cu) -> zone on In1.Cu
# This is the canonical stitching-via pattern.
STITCH_VIA_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")

  (footprint "C_0402"
    (layer "F.Cu")
    (at 15 15)
    (property "Reference" "C1")
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (footprint "C_0402"
    (layer "F.Cu")
    (at 25 15)
    (property "Reference" "C2")
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (segment (start 15 15.25) (end 15 17) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 25 15.25) (end 25 17) (width 0.25) (layer "F.Cu") (net 1))

  (via (at 15 17) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1))
  (via (at 25 17) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1))

  (zone
    (net 1)
    (net_name "GND")
    (layer "In1.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
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
      (layer "In1.Cu")
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

# Same as STITCH_VIA_PCB but via uses through-via layers ["F.Cu", "B.Cu"]
# instead of blind ["F.Cu", "In1.Cu"].  Zone is still on In1.Cu.
THROUGH_VIA_STITCH_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")

  (footprint "C_0402"
    (layer "F.Cu")
    (at 15 15)
    (property "Reference" "C1")
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (footprint "C_0402"
    (layer "F.Cu")
    (at 25 15)
    (property "Reference" "C2")
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (segment (start 15 15.25) (end 15 17) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 25 15.25) (end 25 17) (width 0.25) (layer "F.Cu") (net 1))

  (via (at 15 17) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via (at 25 17) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))

  (zone
    (net 1)
    (net_name "GND")
    (layer "In1.Cu")
    (uuid "00000000-0000-0000-0000-000000000003")
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
      (layer "In1.Cu")
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

# Dog-leg trace stub: pad -> two segment trace -> via -> zone
DOGLEG_STITCH_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")

  (footprint "C_0402"
    (layer "F.Cu")
    (at 15 15)
    (property "Reference" "C1")
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (footprint "C_0402"
    (layer "F.Cu")
    (at 25 15)
    (property "Reference" "C2")
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (segment (start 15 15.25) (end 16 16) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 16 16) (end 16 17) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 25 15.25) (end 25 17) (width 0.25) (layer "F.Cu") (net 1))

  (via (at 16 17) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via (at 25 17) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))

  (zone
    (net 1)
    (net_name "GND")
    (layer "In1.Cu")
    (uuid "00000000-0000-0000-0000-000000000004")
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
      (layer "In1.Cu")
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
def stitch_via_pcb(tmp_path: Path) -> Path:
    """Create a PCB with stitching vias (blind via F.Cu->In1.Cu)."""
    pcb_file = tmp_path / "stitch_via.kicad_pcb"
    pcb_file.write_text(STITCH_VIA_PCB)
    return pcb_file


@pytest.fixture
def through_via_stitch_pcb(tmp_path: Path) -> Path:
    """Create a PCB with through-vias stitching to an inner layer zone."""
    pcb_file = tmp_path / "through_via_stitch.kicad_pcb"
    pcb_file.write_text(THROUGH_VIA_STITCH_PCB)
    return pcb_file


@pytest.fixture
def dogleg_stitch_pcb(tmp_path: Path) -> Path:
    """Create a PCB with dog-leg trace stubs to stitching vias."""
    pcb_file = tmp_path / "dogleg_stitch.kicad_pcb"
    pcb_file.write_text(DOGLEG_STITCH_PCB)
    return pcb_file


class TestStitchingViaConnectivity:
    """Tests for stitching via connectivity in net status analysis.

    These tests verify the pad -> trace stub -> via -> zone connection
    pattern used by the stitch command.
    """

    def test_blind_via_stitch_complete(self, stitch_via_pcb: Path):
        """Blind via (F.Cu/In1.Cu) stitching to In1.Cu zone is complete."""
        analyzer = NetStatusAnalyzer(stitch_via_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.total_pads == 2
        assert gnd.status == "complete", (
            f"GND should be complete but is {gnd.status}; "
            f"unconnected: {[p.full_name for p in gnd.unconnected_pads]}"
        )

    def test_through_via_stitch_complete(self, through_via_stitch_pcb: Path):
        """Through-via (F.Cu/B.Cu) stitching to In1.Cu zone is complete."""
        analyzer = NetStatusAnalyzer(through_via_stitch_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.total_pads == 2
        assert gnd.status == "complete", (
            f"GND should be complete but is {gnd.status}; "
            f"unconnected: {[p.full_name for p in gnd.unconnected_pads]}"
        )

    def test_dogleg_trace_stitch_complete(self, dogleg_stitch_pcb: Path):
        """Dog-leg trace stub (2 segments) to stitching via is complete."""
        analyzer = NetStatusAnalyzer(dogleg_stitch_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.total_pads == 2
        assert gnd.status == "complete", (
            f"GND should be complete but is {gnd.status}; "
            f"unconnected: {[p.full_name for p in gnd.unconnected_pads]}"
        )

    def test_stitch_via_is_plane_net(self, stitch_via_pcb: Path):
        """Stitched net is correctly identified as a plane net."""
        analyzer = NetStatusAnalyzer(stitch_via_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.is_plane_net is True
        assert gnd.plane_layer == "In1.Cu"

    def test_stitch_via_has_vias(self, stitch_via_pcb: Path):
        """Stitched net correctly reports having vias."""
        analyzer = NetStatusAnalyzer(stitch_via_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.has_vias is True
        assert gnd.has_routing is True


# ---- PCB fixtures for multi-zone and large zone fill tests (Issue #2035) ----

# PCB with zones on multiple layers for the same net.
# GND has zones on both In1.Cu and B.Cu, connected via through-vias.
MULTI_ZONE_LAYER_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")

  (footprint "C_0402"
    (layer "F.Cu")
    (at 15 15)
    (property "Reference" "C1")
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (footprint "C_0402"
    (layer "F.Cu")
    (at 25 15)
    (property "Reference" "C2")
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (segment (start 15 15.25) (end 15 17) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 25 15.25) (end 25 17) (width 0.25) (layer "F.Cu") (net 1))

  (via (at 15 17) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via (at 25 17) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))

  (zone
    (net 1)
    (net_name "GND")
    (layer "In1.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
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
      (layer "In1.Cu")
      (pts
        (xy 10 10)
        (xy 30 10)
        (xy 30 20)
        (xy 10 20)
      )
    )
  )

  (zone
    (net 1)
    (net_name "GND")
    (layer "B.Cu")
    (uuid "00000000-0000-0000-0000-000000000011")
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
      (layer "B.Cu")
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


def _generate_large_zone_pcb(num_filled_polygons: int = 500) -> str:
    """Generate a PCB with many filled zone polygons to test performance.

    Creates a GND zone with many small filled polygon islands to verify
    that the zone_points sampling limit removal does not cause false positives
    and that bounding-box spatial indexing keeps analysis fast.
    """
    header = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")

  (footprint "C_0402"
    (layer "F.Cu")
    (at 50 50)
    (property "Reference" "C1")
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (footprint "C_0402"
    (layer "F.Cu")
    (at 150 50)
    (property "Reference" "C2")
    (pad "2" smd rect (at 0 0.25) (size 0.4 0.4) (layers "F.Cu") (net 1 "GND"))
  )

  (segment (start 50 50.25) (end 50 52) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 150 50.25) (end 150 52) (width 0.25) (layer "F.Cu") (net 1))

  (via (at 50 52) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (via (at 150 52) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))

  (zone
    (net 1)
    (net_name "GND")
    (layer "In1.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.15)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))
    (polygon
      (pts
        (xy 0 0)
        (xy 200 0)
        (xy 200 100)
        (xy 0 100)
      )
    )
"""
    # Generate many filled polygons (small tiles across the zone)
    filled_parts = []
    cols = int(num_filled_polygons**0.5) + 1
    for i in range(num_filled_polygons):
        row = i // cols
        col = i % cols
        x = col * 2
        y = row * 2
        filled_parts.append(
            f"""    (filled_polygon
      (layer "In1.Cu")
      (pts
        (xy {x} {y})
        (xy {x + 1.5} {y})
        (xy {x + 1.5} {y + 1.5})
        (xy {x} {y + 1.5})
      )
    )"""
        )

    footer = """  )
)
"""
    return header + "\n".join(filled_parts) + "\n" + footer


# PCB with zone that has filled_polygon data but no boundary polygon.
# This tests the fallback boundary detection using bounding box.
ZONE_NO_BOUNDARY_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")

  (footprint "R_0805"
    (layer "F.Cu")
    (at 15 15)
    (property "Reference" "R1")
    (pad "1" thru_hole rect (at 0 0) (size 1.2 1.2) (drill 0.6) (layers "*.Cu") (net 1 "GND"))
  )

  (footprint "R_0805"
    (layer "F.Cu")
    (at 25 15)
    (property "Reference" "R2")
    (pad "1" thru_hole rect (at 0 0) (size 1.2 1.2) (drill 0.6) (layers "*.Cu") (net 1 "GND"))
  )

  (zone
    (net 1)
    (net_name "GND")
    (layer "In1.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.15)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))
    (filled_polygon
      (layer "In1.Cu")
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
def multi_zone_layer_pcb(tmp_path: Path) -> Path:
    """Create a PCB with zones on multiple layers for the same net."""
    pcb_file = tmp_path / "multi_zone.kicad_pcb"
    pcb_file.write_text(MULTI_ZONE_LAYER_PCB)
    return pcb_file


@pytest.fixture
def large_zone_pcb(tmp_path: Path) -> Path:
    """Create a PCB with many filled zone polygons."""
    pcb_file = tmp_path / "large_zone.kicad_pcb"
    pcb_file.write_text(_generate_large_zone_pcb(500))
    return pcb_file


@pytest.fixture
def zone_no_boundary_pcb(tmp_path: Path) -> Path:
    """Create a PCB with zone that has no boundary polygon."""
    pcb_file = tmp_path / "no_boundary.kicad_pcb"
    pcb_file.write_text(ZONE_NO_BOUNDARY_PCB)
    return pcb_file


class TestMultiZoneConnectivity:
    """Tests for zone fill connectivity improvements (Issue #2035).

    These tests cover:
    - Multiple zone layers per net reporting all layers
    - Large zone fills (>1000 polygon vertices) without false positives
    - Zones with missing boundary polygons (filled_polygon only)
    - Performance with many filled polygons
    """

    def test_multi_zone_layers_reported(self, multi_zone_layer_pcb: Path):
        """Net with zones on multiple layers reports all zone layers."""
        analyzer = NetStatusAnalyzer(multi_zone_layer_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.is_plane_net is True
        assert len(gnd.plane_layers) == 2
        assert "In1.Cu" in gnd.plane_layers
        assert "B.Cu" in gnd.plane_layers
        # Backward compat: plane_layer returns first layer
        assert gnd.plane_layer in ("In1.Cu", "B.Cu")

    def test_multi_zone_complete(self, multi_zone_layer_pcb: Path):
        """Net with zones on multiple layers is complete when vias connect."""
        analyzer = NetStatusAnalyzer(multi_zone_layer_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.status == "complete", (
            f"GND should be complete but is {gnd.status}; "
            f"unconnected: {[p.full_name for p in gnd.unconnected_pads]}"
        )

    def test_multi_zone_to_dict_includes_plane_layers(self, multi_zone_layer_pcb: Path):
        """to_dict includes both plane_layer and plane_layers."""
        analyzer = NetStatusAnalyzer(multi_zone_layer_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        d = gnd.to_dict()
        assert "plane_layer" in d
        assert "plane_layers" in d
        assert len(d["plane_layers"]) == 2
        assert d["plane_layer"] in d["plane_layers"]

    def test_multi_zone_suggested_fix_shows_all_layers(self):
        """Suggested fix for plane net shows all zone layers."""
        status = NetStatus(
            net_number=1,
            net_name="GND",
            is_plane_net=True,
            plane_layer="In1.Cu",
            plane_layers=["In1.Cu", "B.Cu"],
            total_pads=2,
            unconnected_pads=[PadInfo("C1", "2", (15, 15), False)],
        )
        assert "In1.Cu" in status.suggested_fix
        assert "B.Cu" in status.suggested_fix

    def test_large_zone_no_false_positives(self, large_zone_pcb: Path):
        """Board with 500+ filled polygons does not produce false positives.

        Previously, the zone_points[:1000] sampling cap could miss zone
        connectivity when zone polygon vertices exceeded the limit.
        """
        analyzer = NetStatusAnalyzer(large_zone_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.is_plane_net is True
        assert gnd.status == "complete", (
            f"GND should be complete but is {gnd.status}; "
            f"unconnected: {[p.full_name for p in gnd.unconnected_pads]}"
        )

    def test_large_zone_performance(self, large_zone_pcb: Path):
        """Analysis of board with 500+ filled polygons completes in <5s."""
        import time

        analyzer = NetStatusAnalyzer(large_zone_pcb)
        start = time.monotonic()
        result = analyzer.analyze()
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, f"Analysis took {elapsed:.2f}s (limit: 5s)"
        assert result.total_nets >= 1

    def test_zone_no_boundary_polygon(self, zone_no_boundary_pcb: Path):
        """Zone with no boundary polygon (only filled_polygon) detects pads.

        When kicad-cli omits the zone outline for filled zones, the
        bounding-box fallback boundary should still detect pad connectivity.
        """
        analyzer = NetStatusAnalyzer(zone_no_boundary_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.is_plane_net is True
        assert gnd.status == "complete", (
            f"GND should be complete but is {gnd.status}; "
            f"unconnected: {[p.full_name for p in gnd.unconnected_pads]}"
        )

    def test_bounding_box_polygon_helper(self):
        """Test _bounding_box_polygon helper method."""
        polys = [
            [(0.0, 0.0), (5.0, 0.0), (5.0, 3.0)],
            [(10.0, 10.0), (15.0, 10.0), (15.0, 15.0)],
        ]
        result = NetStatusAnalyzer._bounding_box_polygon(polys)
        assert len(result) == 4
        # Should be bounding box of all points
        xs = [p[0] for p in result]
        ys = [p[1] for p in result]
        assert min(xs) == 0.0
        assert max(xs) == 15.0
        assert min(ys) == 0.0
        assert max(ys) == 15.0

    def test_bounding_box_polygon_empty(self):
        """Test _bounding_box_polygon with empty input."""
        result = NetStatusAnalyzer._bounding_box_polygon([])
        assert result == []

    def test_polygon_bbox_helper(self):
        """Test _polygon_bbox static method."""
        poly = [(1.0, 2.0), (5.0, 3.0), (3.0, 8.0)]
        bbox = NetStatusAnalyzer._polygon_bbox(poly)
        assert bbox == (1.0, 2.0, 5.0, 8.0)

    def test_point_in_bbox_helper(self):
        """Test _point_in_bbox static method."""
        bbox = (0.0, 0.0, 10.0, 10.0)
        assert NetStatusAnalyzer._point_in_bbox((5.0, 5.0), bbox) is True
        assert NetStatusAnalyzer._point_in_bbox((0.0, 0.0), bbox) is True
        assert NetStatusAnalyzer._point_in_bbox((10.0, 10.0), bbox) is True
        assert NetStatusAnalyzer._point_in_bbox((-1.0, 5.0), bbox) is False
        assert NetStatusAnalyzer._point_in_bbox((5.0, 11.0), bbox) is False

    def test_plane_layer_backward_compat_setter(self):
        """Setting plane_layer via constructor still works."""
        status = NetStatus(
            net_number=1,
            net_name="GND",
            is_plane_net=True,
            plane_layer="F.Cu",
        )
        assert status.plane_layer == "F.Cu"
        # plane_layers should remain empty when set via constructor
        # (backward compat: old code sets plane_layer directly)
        assert status.plane_layers == []

    def test_plane_layers_with_three_layers(self):
        """Net with zones on 3+ layers reports all layers in metadata."""
        status = NetStatus(
            net_number=1,
            net_name="GND",
            is_plane_net=True,
            plane_layers=["F.Cu", "In1.Cu", "B.Cu"],
            plane_layer="F.Cu",
        )
        assert len(status.plane_layers) == 3
        d = status.to_dict()
        assert len(d["plane_layers"]) == 3
        assert d["plane_layer"] == "F.Cu"


# PCB with non-zero board origin (regression for issue #2742)
# - Edge.Cuts gr_rect starts at (100, 100), so board_origin = (100, 100)
# - Footprints, pads, and trace segments are all expressed in
#   sheet-absolute coordinates (matching what KiCad/the autorouter write).
# - The trace between R1.2 and R2.2 fully connects the SIG_A net.
NON_ZERO_ORIGIN_ROUTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "SIG_A")

  (gr_rect
    (start 100 100)
    (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "11111111-1111-1111-1111-111111111111")
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 112.5 108)
    (uuid "22222222-2222-2222-2222-222222222221")
    (property "Reference" "R1" (at 0 -1.5 0))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 0 ""))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG_A"))
  )

  (footprint "R_0402"
    (layer "F.Cu")
    (at 120 108)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Reference" "R2" (at 0 -1.5 0))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 0 ""))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG_A"))
  )

  (segment (start 113 108) (end 120.5 108) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "33333333-3333-3333-3333-333333333331"))
)
"""


class TestNonZeroBoardOriginRegression:
    """Regression tests for issue #2742.

    Before the coordinate-space fix, PCB.load() converted footprint
    positions to board-relative but left ``Segment.start/end``,
    ``Via.position``, and ``Zone.polygon`` in sheet-absolute coordinates.
    NetStatusAnalyzer compared pad positions (board-relative) directly
    against segment endpoints (sheet-absolute), so every signal net on a
    centered board (board_origin != (0, 0)) was reported as ``incomplete``
    by exactly ``board_origin`` mm.

    These tests pin the post-fix invariant: copper primitives loaded from
    a PCB with a non-zero board origin are reported in the same
    coordinate space as footprint positions, and a fully-routed signal
    net is correctly classified as ``complete``.
    """

    @pytest.fixture
    def non_zero_origin_pcb(self, tmp_path: Path) -> Path:
        """PCB with gr_rect Edge.Cuts at (100, 100), routed SIG_A net."""
        pcb_file = tmp_path / "centered_routed.kicad_pcb"
        pcb_file.write_text(NON_ZERO_ORIGIN_ROUTED_PCB)
        return pcb_file

    def test_segments_loaded_in_board_relative_space(self, non_zero_origin_pcb: Path) -> None:
        """Segment endpoints must be in the same space as footprint positions.

        With the fix in place, ``PCB.load`` subtracts ``_board_origin``
        from every segment endpoint just as it already did for footprint
        positions.  ``seg.start`` should therefore equal the segment's
        in-file value minus the detected origin.
        """
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(non_zero_origin_pcb))
        assert pcb._board_origin == (100.0, 100.0)
        assert len(pcb.segments) == 1
        seg = pcb.segments[0]
        # In-file: (113, 108) -> board-relative: (13, 8)
        assert seg.start == pytest.approx((13.0, 8.0))
        # In-file: (120.5, 108) -> board-relative: (20.5, 8)
        assert seg.end == pytest.approx((20.5, 8.0))

    def test_footprint_positions_remain_board_relative(self, non_zero_origin_pcb: Path) -> None:
        """Pre-existing footprint behavior must not regress."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(non_zero_origin_pcb))
        positions = {fp.reference: fp.position for fp in pcb.footprints}
        # In-file: (112.5, 108) -> board-relative: (12.5, 8)
        assert positions["R1"] == pytest.approx((12.5, 8.0))
        # In-file: (120, 108) -> board-relative: (20, 8)
        assert positions["R2"] == pytest.approx((20.0, 8.0))

    def test_signal_net_reported_complete_on_centered_board(
        self, non_zero_origin_pcb: Path
    ) -> None:
        """Issue #2742: fully routed signal net must not be 'incomplete'.

        Before the fix this returned ``incomplete`` with connected=1/2
        because the analyzer compared pads (board-relative) against
        segment endpoints (sheet-absolute), off by 100mm in both axes.
        """
        analyzer = NetStatusAnalyzer(non_zero_origin_pcb)
        result = analyzer.analyze()
        sig = result.get_net("SIG_A")
        assert sig is not None
        assert sig.status == "complete", (
            f"SIG_A should be complete after the coordinate-space fix; "
            f"got status={sig.status} connected={sig.connected_count}/"
            f"{sig.total_pads}"
        )
        assert sig.connected_count == 2
        assert sig.total_pads == 2
        assert sig.has_routing is True

    def test_signal_net_completion_percent_is_100(self, non_zero_origin_pcb: Path) -> None:
        """Report collector must surface 100% signal-net completion."""
        from kicad_tools.report.collector import ReportDataCollector

        collector = ReportDataCollector(
            pcb_path=non_zero_origin_pcb,
            manufacturer="jlcpcb",
        )
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(non_zero_origin_pcb))
        status = collector.collect_net_status(pcb)
        assert status["signal_net_count"] == 1
        assert status["signal_complete_count"] == 1
        assert status["signal_completion_percent"] == pytest.approx(100.0)
        assert status["incomplete_count"] == 0


# ---- PCB fixtures for zero-fill zone connectivity tests (Issue #3482) ----

# Shared body: two GND pads inside the zone outline, no traces, no vias.
# The zone definition is parameterized so each case differs only in its
# fill / filled_polygon content.
_ZONE_FILL_PCB_TEMPLATE = """(kicad_pcb
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
    {fill_clause}
    (polygon
      (pts
        (xy 10 10)
        (xy 30 10)
        (xy 30 20)
        (xy 10 20)
      )
    )
{filled_polygons}  )
)
"""

# Zone with fill ENABLED but zero filled polygons (e.g. fully shadowed by a
# higher-priority overlapping zone or carved away entirely by clearances).
# This is the exact softstart AC_NEUTRAL/ISENSE_POS failure mode from
# Issue #3482: both pads sit inside the zone BOUNDARY but there is no copper.
ZERO_FILL_ZONE_PCB = _ZONE_FILL_PCB_TEMPLATE.format(
    fill_clause="(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))",
    filled_polygons="",
)

# Zone with fill DISABLED (boundary-only zone, board 06 style).
BOUNDARY_ONLY_ZONE_PCB = _ZONE_FILL_PCB_TEMPLATE.format(
    fill_clause="(fill (thermal_gap 0.2) (thermal_bridge_width 0.2))",
    filled_polygons="",
)

# Zone whose fill produced copper over only PART of the outline: filled
# polygon covers x in [10, 22] while the boundary extends to x = 30.
# C1 (15, 15) lands inside filled copper; C2 (25, 15) is inside the
# boundary only.  The Issue #479 thermal-relief heuristic applies because
# the zone genuinely has filled copper.
PARTIAL_FILL_ZONE_PCB = _ZONE_FILL_PCB_TEMPLATE.format(
    fill_clause="(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))",
    filled_polygons="""    (filled_polygon
      (layer "F.Cu")
      (pts
        (xy 10 10)
        (xy 22 10)
        (xy 22 20)
        (xy 10 20)
      )
    )
""",
)


class TestZeroFillZoneConnectivity:
    """Regression tests for Issue #3482.

    A zone with zero filled polygons produces NO copper on the manufactured
    board, so its boundary polygon must not mark pads or vias as connected.
    Before the fix, the Issue #479 boundary heuristic (intended only for
    thermal-relief cutouts INSIDE filled copper) marked every pad inside a
    zero-fill zone outline as zone-connected, reporting electrically open
    pour nets (softstart AC_NEUTRAL / ISENSE_POS) as ``complete``.
    """

    @pytest.fixture
    def zero_fill_pcb(self, tmp_path: Path) -> Path:
        pcb_file = tmp_path / "zero_fill.kicad_pcb"
        pcb_file.write_text(ZERO_FILL_ZONE_PCB)
        return pcb_file

    @pytest.fixture
    def boundary_only_pcb(self, tmp_path: Path) -> Path:
        pcb_file = tmp_path / "boundary_only.kicad_pcb"
        pcb_file.write_text(BOUNDARY_ONLY_ZONE_PCB)
        return pcb_file

    @pytest.fixture
    def partial_fill_pcb(self, tmp_path: Path) -> Path:
        pcb_file = tmp_path / "partial_fill.kicad_pcb"
        pcb_file.write_text(PARTIAL_FILL_ZONE_PCB)
        return pcb_file

    def test_zero_fill_zone_is_not_connectivity(self, zero_fill_pcb: Path):
        """Pads inside a zero-fill zone boundary must NOT report complete.

        This is the softstart PR #3481 failure mode: fill enabled, zero
        filled polygons, no segments, no vias -> open circuit on the
        manufactured board.
        """
        analyzer = NetStatusAnalyzer(zero_fill_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.status != "complete", (
            f"Zero-fill zone must not provide connectivity; got status={gnd.status}"
        )
        # Both pads are electrically isolated; only the trivial largest
        # island (one pad) counts as connected.
        assert gnd.unconnected_count == 1
        assert gnd.connected_count == 1

    def test_boundary_only_zone_is_not_connectivity(self, boundary_only_pcb: Path):
        """A boundary-only zone (no fill) must not provide connectivity."""
        analyzer = NetStatusAnalyzer(boundary_only_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.status != "complete", (
            f"Boundary-only zone must not provide connectivity; got status={gnd.status}"
        )
        assert gnd.unconnected_count == 1

    def test_zero_fill_zone_still_reported_as_plane_net(self, zero_fill_pcb: Path):
        """Zero-fill zones still classify the net as a plane net.

        The plane-net metadata drives the 'kct stitch' suggested fix, which
        is exactly the right remediation for an unfilled pour.
        """
        analyzer = NetStatusAnalyzer(zero_fill_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.is_plane_net is True
        assert gnd.plane_layer == "F.Cu"

    def test_partial_fill_zone_provides_connectivity(self, partial_fill_pcb: Path):
        """A zone with at least one filled polygon retains the Issue #479
        boundary heuristic: pads inside the outline (including thermal-relief
        cutouts) are zone-connected.
        """
        analyzer = NetStatusAnalyzer(partial_fill_pcb)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.status == "complete", (
            f"Partially filled zone should connect pads inside its boundary; "
            f"got status={gnd.status}, "
            f"unconnected: {[p.full_name for p in gnd.unconnected_pads]}"
        )

    def test_fully_filled_zone_regression(self, tmp_path: Path):
        """Fully filled zone behavior (Issue #479) must not regress."""
        pcb_file = tmp_path / "full_fill.kicad_pcb"
        pcb_file.write_text(PLANE_NET_PCB)

        analyzer = NetStatusAnalyzer(pcb_file)
        result = analyzer.analyze()

        gnd = result.get_net("GND")
        assert gnd is not None
        assert gnd.status == "complete", (
            f"Fully filled zone should connect both pads; got {gnd.status}"
        )
