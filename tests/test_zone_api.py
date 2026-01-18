"""Tests for the zone API in PCBEditor.

These tests verify the copper pour zone management APIs including:
- Basic zone creation with thermal settings
- Keepout zones
- Standard GND pour convenience method
- 4-layer stackup setup
- Zone querying
"""

import tempfile
from pathlib import Path

import pytest

from kicad_tools.pcb import Keepout, PCBEditor, Point, Zone
from kicad_tools.sexp.builders import keepout_node, zone_node


class TestZoneDataclass:
    """Tests for the Zone dataclass."""

    def test_zone_basic_attributes(self):
        """Zone has all required attributes."""
        zone = Zone(
            net=1,
            net_name="GND",
            layer="B.Cu",
            points=[Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)],
        )
        assert zone.net == 1
        assert zone.net_name == "GND"
        assert zone.layer == "B.Cu"
        assert len(zone.points) == 4

    def test_zone_default_thermal_settings(self):
        """Zone has sensible defaults for thermal settings."""
        zone = Zone(
            net=1,
            net_name="GND",
            layer="B.Cu",
            points=[Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)],
        )
        assert zone.priority == 0
        assert zone.min_thickness == 0.2
        assert zone.clearance == 0.2
        assert zone.thermal_gap == 0.3
        assert zone.thermal_bridge_width == 0.3

    def test_zone_custom_thermal_settings(self):
        """Zone accepts custom thermal settings."""
        zone = Zone(
            net=1,
            net_name="GND",
            layer="B.Cu",
            points=[Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)],
            priority=2,
            min_thickness=0.25,
            clearance=0.4,
            thermal_gap=0.5,
            thermal_bridge_width=0.6,
        )
        assert zone.priority == 2
        assert zone.min_thickness == 0.25
        assert zone.clearance == 0.4
        assert zone.thermal_gap == 0.5
        assert zone.thermal_bridge_width == 0.6

    def test_zone_to_sexp(self):
        """Zone generates valid S-expression."""
        zone = Zone(
            net=1,
            net_name="GND",
            layer="B.Cu",
            points=[Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)],
            clearance=0.3,
            thermal_gap=0.5,
        )
        sexp = zone.to_sexp_node()
        sexp_str = sexp.to_string()

        assert "(zone" in sexp_str
        assert "(net 1)" in sexp_str
        assert '(net_name "GND")' in sexp_str
        assert '(layer "B.Cu")' in sexp_str
        assert "(clearance 0.3)" in sexp_str
        assert "(thermal_gap 0.5)" in sexp_str


class TestKeeoutDataclass:
    """Tests for the Keepout dataclass."""

    def test_keepout_basic_attributes(self):
        """Keepout has all required attributes."""
        keepout = Keepout(
            points=[Point(50, 50), Point(70, 50), Point(70, 70), Point(50, 70)],
            layers=["F.Cu", "B.Cu"],
        )
        assert len(keepout.points) == 4
        assert keepout.layers == ["F.Cu", "B.Cu"]

    def test_keepout_default_restrictions(self):
        """Keepout has default restriction settings."""
        keepout = Keepout(
            points=[Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)],
        )
        assert keepout.no_tracks is True
        assert keepout.no_vias is True
        assert keepout.no_pour is True

    def test_keepout_custom_restrictions(self):
        """Keepout accepts custom restriction settings."""
        keepout = Keepout(
            points=[Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)],
            no_tracks=False,
            no_vias=True,
            no_pour=False,
        )
        assert keepout.no_tracks is False
        assert keepout.no_vias is True
        assert keepout.no_pour is False

    def test_keepout_to_sexp(self):
        """Keepout generates valid S-expression."""
        keepout = Keepout(
            points=[Point(50, 50), Point(70, 50), Point(70, 70), Point(50, 70)],
            layers=["F.Cu", "B.Cu"],
            no_tracks=True,
            no_vias=False,
            no_pour=True,
        )
        sexp = keepout.to_sexp_node()
        sexp_str = sexp.to_string()

        assert "(zone" in sexp_str
        assert "(keepout" in sexp_str
        # S-expression uses quoted strings for keyword values
        assert 'tracks "not_allowed"' in sexp_str or "(tracks not_allowed)" in sexp_str
        assert 'vias "allowed"' in sexp_str or "(vias allowed)" in sexp_str
        assert 'copperpour "not_allowed"' in sexp_str or "(copperpour not_allowed)" in sexp_str


class TestZoneNodeBuilder:
    """Tests for the zone_node S-expression builder."""

    def test_zone_node_basic(self):
        """zone_node creates valid zone S-expression."""
        sexp = zone_node(
            net=1,
            net_name="GND",
            layer="B.Cu",
            points=[(0, 0), (10, 0), (10, 10), (0, 10)],
            uuid_str="test-uuid",
        )
        sexp_str = sexp.to_string()

        assert "(zone" in sexp_str
        assert "(net 1)" in sexp_str
        assert '(net_name "GND")' in sexp_str
        assert '(layer "B.Cu")' in sexp_str
        assert "(polygon" in sexp_str
        assert "(xy 0 0)" in sexp_str

    def test_zone_node_with_priority(self):
        """zone_node respects priority setting."""
        sexp = zone_node(
            net=1,
            net_name="GND",
            layer="B.Cu",
            points=[(0, 0), (10, 0), (10, 10), (0, 10)],
            uuid_str="test-uuid",
            priority=2,
        )
        sexp_str = sexp.to_string()
        assert "(priority 2)" in sexp_str

    def test_zone_node_thermal_settings(self):
        """zone_node includes thermal relief settings."""
        sexp = zone_node(
            net=1,
            net_name="GND",
            layer="B.Cu",
            points=[(0, 0), (10, 0), (10, 10), (0, 10)],
            uuid_str="test-uuid",
            thermal_gap=0.5,
            thermal_bridge_width=0.6,
        )
        sexp_str = sexp.to_string()
        assert "(thermal_gap 0.5)" in sexp_str
        assert "(thermal_bridge_width 0.6)" in sexp_str


class TestKeepoutNodeBuilder:
    """Tests for the keepout_node S-expression builder."""

    def test_keepout_node_basic(self):
        """keepout_node creates valid keepout zone S-expression."""
        sexp = keepout_node(
            points=[(50, 50), (70, 50), (70, 70), (50, 70)],
            layers=["F.Cu", "B.Cu"],
            uuid_str="test-uuid",
        )
        sexp_str = sexp.to_string()

        assert "(zone" in sexp_str
        assert "(keepout" in sexp_str
        assert '(layers "F.Cu" "B.Cu")' in sexp_str

    def test_keepout_node_restrictions(self):
        """keepout_node respects restriction settings."""
        sexp = keepout_node(
            points=[(0, 0), (10, 0), (10, 10), (0, 10)],
            layers=["F.Cu"],
            no_tracks=True,
            no_vias=False,
            no_pour=True,
            uuid_str="test-uuid",
        )
        sexp_str = sexp.to_string()

        # S-expression uses quoted strings for keyword values
        assert 'tracks "not_allowed"' in sexp_str or "(tracks not_allowed)" in sexp_str
        assert 'vias "allowed"' in sexp_str or "(vias allowed)" in sexp_str
        assert 'copperpour "not_allowed"' in sexp_str or "(copperpour not_allowed)" in sexp_str


class TestPCBEditorZoneAPI:
    """Tests for PCBEditor zone management methods."""

    @pytest.fixture
    def simple_pcb(self, tmp_path):
        """Create a simple PCB file for testing."""
        pcb_content = '''(kicad_pcb
  (version 20240108)
  (generator "kicad_tools")
  (general
    (thickness 1.6)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (gr_line (start 0 0) (end 100 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 100 0) (end 100 80) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 100 80) (end 0 80) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 80) (end 0 0) (layer "Edge.Cuts") (width 0.1))
)'''
        pcb_path = tmp_path / "test_board.kicad_pcb"
        pcb_path.write_text(pcb_content)
        return pcb_path

    def test_add_zone_basic(self, simple_pcb):
        """add_zone creates a zone with basic parameters."""
        pcb = PCBEditor(str(simple_pcb))
        zone = pcb.add_zone(
            net_name="GND",
            layer="B.Cu",
            boundary=[(10, 10), (90, 10), (90, 70), (10, 70)],
        )

        assert zone.net == 1
        assert zone.net_name == "GND"
        assert zone.layer == "B.Cu"
        assert len(zone.points) == 4

    def test_add_zone_thermal_settings(self, simple_pcb):
        """add_zone accepts thermal relief settings."""
        pcb = PCBEditor(str(simple_pcb))
        zone = pcb.add_zone(
            net_name="GND",
            layer="B.Cu",
            boundary=[(10, 10), (90, 10), (90, 70), (10, 70)],
            clearance=0.4,
            thermal_gap=0.6,
            thermal_spoke_width=0.5,
        )

        assert zone.clearance == 0.4
        assert zone.thermal_gap == 0.6
        assert zone.thermal_bridge_width == 0.5

    def test_add_zone_with_board_outline(self, simple_pcb):
        """add_zone can use board outline as boundary."""
        pcb = PCBEditor(str(simple_pcb))
        zone = pcb.add_zone(
            net_name="GND",
            layer="B.Cu",
            boundary="board_outline",
        )

        # Should have extracted the board outline (100x80)
        assert len(zone.points) >= 3

    def test_add_zone_priority(self, simple_pcb):
        """add_zone respects priority setting."""
        pcb = PCBEditor(str(simple_pcb))
        zone = pcb.add_zone(
            net_name="GND",
            layer="B.Cu",
            boundary=[(0, 0), (100, 0), (100, 80), (0, 80)],
            priority=3,
        )

        assert zone.priority == 3

    def test_add_keepout_basic(self, simple_pcb):
        """add_keepout creates a keepout zone."""
        pcb = PCBEditor(str(simple_pcb))
        keepout = pcb.add_keepout(
            boundary=[(40, 30), (60, 30), (60, 50), (40, 50)],
        )

        assert len(keepout.points) == 4
        assert keepout.no_tracks is True
        assert keepout.no_vias is True
        assert keepout.no_pour is True

    def test_add_keepout_custom_restrictions(self, simple_pcb):
        """add_keepout accepts custom restriction settings."""
        pcb = PCBEditor(str(simple_pcb))
        keepout = pcb.add_keepout(
            boundary=[(40, 30), (60, 30), (60, 50), (40, 50)],
            layers=["F.Cu"],
            no_tracks=False,
            no_vias=True,
            no_pour=False,
        )

        assert keepout.layers == ["F.Cu"]
        assert keepout.no_tracks is False
        assert keepout.no_vias is True
        assert keepout.no_pour is False

    def test_get_zones_empty(self, simple_pcb):
        """get_zones returns empty list when no zones exist."""
        pcb = PCBEditor(str(simple_pcb))
        zones = pcb.get_zones()
        assert zones == []

    def test_get_zones_after_add(self, simple_pcb):
        """get_zones returns added zones."""
        pcb = PCBEditor(str(simple_pcb))
        pcb.add_zone(
            net_name="GND",
            layer="B.Cu",
            boundary=[(0, 0), (100, 0), (100, 80), (0, 80)],
            priority=1,
        )

        zones = pcb.get_zones()
        assert len(zones) == 1
        assert zones[0]["net"] == "GND"
        assert zones[0]["layer"] == "B.Cu"
        assert zones[0]["priority"] == 1

    def test_add_standard_gnd_pour(self, simple_pcb):
        """add_standard_gnd_pour creates GND zone with defaults."""
        pcb = PCBEditor(str(simple_pcb))
        zone = pcb.add_standard_gnd_pour()

        assert zone.net_name == "GND"
        assert zone.layer == "B.Cu"
        assert zone.clearance == 0.3
        assert zone.thermal_gap == 0.5

    def test_add_standard_gnd_pour_custom_layer(self, simple_pcb):
        """add_standard_gnd_pour accepts custom layer."""
        pcb = PCBEditor(str(simple_pcb))
        zone = pcb.add_standard_gnd_pour(layer="F.Cu")

        assert zone.layer == "F.Cu"

    def test_setup_4layer_stackup(self, simple_pcb):
        """setup_4layer_stackup creates GND and VCC zones."""
        pcb = PCBEditor(str(simple_pcb))
        zones = pcb.setup_4layer_stackup()

        assert len(zones) == 2

        # First zone should be GND
        gnd_zone = zones[0]
        assert gnd_zone.net_name == "GND"
        assert gnd_zone.layer == "In1.Cu"
        assert gnd_zone.priority == 1

        # Second zone should be VCC
        vcc_zone = zones[1]
        assert vcc_zone.net_name == "+3.3V"
        assert vcc_zone.layer == "In2.Cu"
        assert vcc_zone.priority == 0

    def test_setup_4layer_stackup_custom(self, simple_pcb):
        """setup_4layer_stackup accepts custom parameters."""
        pcb = PCBEditor(str(simple_pcb))
        zones = pcb.setup_4layer_stackup(
            gnd_layer="In2.Cu",
            vcc_layer="In1.Cu",
            vcc_net="+3.3V",
        )

        assert zones[0].layer == "In2.Cu"  # GND
        assert zones[1].layer == "In1.Cu"  # VCC

    def test_zone_roundtrip(self, simple_pcb, tmp_path):
        """Zone survives save/load roundtrip."""
        pcb = PCBEditor(str(simple_pcb))
        pcb.add_zone(
            net_name="GND",
            layer="B.Cu",
            boundary=[(10, 10), (90, 10), (90, 70), (10, 70)],
            priority=2,
            clearance=0.35,
        )

        # Save
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(str(output_path))

        # Reload and verify
        pcb2 = PCBEditor(str(output_path))
        zones = pcb2.get_zones()

        assert len(zones) == 1
        assert zones[0]["net"] == "GND"
        assert zones[0]["layer"] == "B.Cu"
        assert zones[0]["priority"] == 2


class TestBoardOutlineExtraction:
    """Tests for board outline extraction."""

    @pytest.fixture
    def pcb_with_outline(self, tmp_path):
        """Create a PCB with Edge.Cuts outline."""
        pcb_content = '''(kicad_pcb
  (version 20240108)
  (generator "kicad_tools")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 40) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 40) (end 0 40) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 40) (end 0 0) (layer "Edge.Cuts") (width 0.1))
)'''
        pcb_path = tmp_path / "outline_board.kicad_pcb"
        pcb_path.write_text(pcb_content)
        return pcb_path

    def test_extract_board_outline(self, pcb_with_outline):
        """_get_board_outline extracts Edge.Cuts polygon."""
        pcb = PCBEditor(str(pcb_with_outline))
        outline = pcb._get_board_outline()

        # Should have 4+ points forming the rectangle
        assert len(outline) >= 4

        # Check that bounds are approximately correct
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        assert min(xs) == pytest.approx(0.0, abs=0.1)
        assert max(xs) == pytest.approx(50.0, abs=0.1)
        assert min(ys) == pytest.approx(0.0, abs=0.1)
        assert max(ys) == pytest.approx(40.0, abs=0.1)

    def test_fallback_when_no_outline(self, tmp_path):
        """_get_board_outline falls back to default when no Edge.Cuts."""
        pcb_content = '''(kicad_pcb
  (version 20240108)
  (generator "kicad_tools")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "GND")
)'''
        pcb_path = tmp_path / "no_outline.kicad_pcb"
        pcb_path.write_text(pcb_content)

        pcb = PCBEditor(str(pcb_path))
        outline = pcb._get_board_outline()

        # Should return a default rectangle
        assert len(outline) == 4
