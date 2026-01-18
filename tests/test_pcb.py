"""Tests for PCB parsing and editing."""

from pathlib import Path

import pytest

from kicad_tools import load_pcb
from kicad_tools.schema import PCB


def test_load_pcb(minimal_pcb: Path):
    """Load a PCB file."""
    doc = load_pcb(str(minimal_pcb))
    assert doc is not None
    assert doc.tag == "kicad_pcb"


def test_parse_pcb(minimal_pcb: Path):
    """Parse PCB into structured data."""
    doc = load_pcb(str(minimal_pcb))
    pcb = PCB(doc)

    # Check layers (dict of layer_num -> Layer)
    assert len(pcb.layers) > 0
    layer_names = [l.name for l in pcb.layers.values()]
    assert "F.Cu" in layer_names
    assert "B.Cu" in layer_names


def test_pcb_nets(minimal_pcb: Path):
    """Parse PCB nets."""
    doc = load_pcb(str(minimal_pcb))
    pcb = PCB(doc)

    # nets is a dict of net_num -> Net
    assert len(pcb.nets) >= 2
    net_names = [n.name for n in pcb.nets.values()]
    assert "GND" in net_names
    assert "+3.3V" in net_names


def test_pcb_footprints(minimal_pcb: Path):
    """Parse PCB footprints."""
    doc = load_pcb(str(minimal_pcb))
    pcb = PCB(doc)

    assert len(pcb.footprints) == 1
    fp = pcb.footprints[0]
    assert fp.name == "Resistor_SMD:R_0402_1005Metric"
    assert len(fp.pads) == 2


def test_pcb_pad_nets(minimal_pcb: Path):
    """Parse pad net assignments.

    Issue #938: PCB loader must populate pad.net attribute for routing.
    """
    doc = load_pcb(str(minimal_pcb))
    pcb = PCB(doc)

    fp = pcb.footprints[0]
    assert len(fp.pads) == 2

    # Pad 1 should have net 1 (GND)
    pad1 = fp.pads[0]
    assert pad1.number == "1"
    assert pad1.net == 1  # The .net property (alias for net_number)
    assert pad1.net_number == 1
    assert pad1.net_name == "GND"

    # Pad 2 should have net 2 (+3.3V)
    pad2 = fp.pads[1]
    assert pad2.number == "2"
    assert pad2.net == 2
    assert pad2.net_number == 2
    assert pad2.net_name == "+3.3V"


def test_pcb_traces(minimal_pcb: Path):
    """Parse PCB traces."""
    doc = load_pcb(str(minimal_pcb))
    pcb = PCB(doc)

    assert len(pcb.segments) == 1
    seg = pcb.segments[0]
    assert seg.net_number == 1
    assert seg.layer == "F.Cu"


class TestPCBEditor:
    """Tests for PCB editor module."""

    def test_point_creation(self):
        """Test Point dataclass."""
        from kicad_tools.pcb.editor import Point

        p = Point(10.0, 20.0)
        assert p.x == 10.0
        assert p.y == 20.0

        # Test iteration
        coords = list(p)
        assert coords == [10.0, 20.0]

    def test_track_creation(self):
        """Test Track dataclass."""
        from kicad_tools.pcb.editor import Point, Track

        track = Track(
            net=1,
            start=Point(0.0, 0.0),
            end=Point(10.0, 0.0),
            width=0.25,
            layer="F.Cu",
        )
        assert track.net == 1
        assert track.start.x == 0.0
        assert track.end.x == 10.0
        assert track.width == 0.25
        assert track.layer == "F.Cu"

    def test_track_to_sexp(self):
        """Test Track S-expression generation."""
        from kicad_tools.pcb.editor import Point, Track

        track = Track(
            net=1,
            start=Point(0.0, 0.0),
            end=Point(10.0, 0.0),
            width=0.25,
            layer="F.Cu",
            uuid_str="test-uuid",
        )
        sexp = track.to_sexp_node()
        assert sexp.name == "segment"
        assert sexp.find("net") is not None

    def test_via_creation(self):
        """Test Via dataclass."""
        from kicad_tools.pcb.editor import Point, Via

        via = Via(
            net=1,
            position=Point(5.0, 5.0),
            size=0.6,
            drill=0.3,
        )
        assert via.net == 1
        assert via.position.x == 5.0
        assert via.size == 0.6
        assert via.drill == 0.3
        assert via.layers == ("F.Cu", "B.Cu")

    def test_via_to_sexp(self):
        """Test Via S-expression generation."""
        from kicad_tools.pcb.editor import Point, Via

        via = Via(
            net=1,
            position=Point(5.0, 5.0),
            size=0.6,
            drill=0.3,
            uuid_str="test-via-uuid",
        )
        sexp = via.to_sexp_node()
        assert sexp.name == "via"

    def test_zone_creation(self):
        """Test Zone dataclass."""
        from kicad_tools.pcb.editor import Point, Zone

        zone = Zone(
            net=1,
            net_name="GND",
            layer="F.Cu",
            points=[Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)],
        )
        assert zone.net == 1
        assert zone.net_name == "GND"
        assert len(zone.points) == 4

    def test_zone_to_sexp(self):
        """Test Zone S-expression generation."""
        from kicad_tools.pcb.editor import Point, Zone

        zone = Zone(
            net=1,
            net_name="GND",
            layer="F.Cu",
            points=[Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)],
            uuid_str="test-zone-uuid",
        )
        sexp = zone.to_sexp_node()
        assert sexp.name == "zone"

    def test_editor_init_nonexistent(self, tmp_path):
        """Test PCBEditor with nonexistent file."""
        from kicad_tools.pcb.editor import PCBEditor

        editor = PCBEditor(str(tmp_path / "nonexistent.kicad_pcb"))
        assert editor.doc is None
        assert editor.nets == {}
        assert editor.footprints == {}

    def test_editor_init_with_pcb(self, minimal_pcb):
        """Test PCBEditor with existing PCB file."""
        from kicad_tools.pcb.editor import PCBEditor

        editor = PCBEditor(str(minimal_pcb))
        assert editor.doc is not None
        assert "GND" in editor.nets
        assert "+3.3V" in editor.nets


class TestFootprintLibrary:
    """Tests for footprint library module."""

    def test_pad_info_creation(self):
        """Test PadInfo dataclass."""
        from kicad_tools.pcb.footprints import PadInfo

        pad = PadInfo(
            name="1",
            x=-0.775,
            y=0,
            width=0.8,
            height=0.9,
        )
        assert pad.name == "1"
        assert pad.x == -0.775
        assert pad.y == 0
        assert pad.shape == "roundrect"

    def test_footprint_library_known_footprints(self):
        """Test listing known footprints."""
        from kicad_tools.pcb.footprints import FootprintLibrary

        lib = FootprintLibrary()
        known = lib.list_known_footprints()

        assert "Capacitor_SMD:C_0603_1608Metric" in known
        assert "Resistor_SMD:R_0603_1608Metric" in known
        assert "Package_TO_SOT_SMD:SOT-23-5" in known

    def test_get_pads_builtin(self):
        """Test getting pads for built-in footprint."""
        from kicad_tools.pcb.footprints import FootprintLibrary

        lib = FootprintLibrary()
        pads = lib.get_pads("Capacitor_SMD:C_0603_1608Metric")

        assert "1" in pads
        assert "2" in pads
        assert pads["1"] == pytest.approx((-0.775, 0), abs=0.01)
        assert pads["2"] == pytest.approx((0.775, 0), abs=0.01)

    def test_get_pads_alias(self):
        """Test getting pads using alias."""
        from kicad_tools.pcb.footprints import FootprintLibrary

        lib = FootprintLibrary()
        pads = lib.get_pads("C_0603_1608Metric")

        # Should resolve alias to full name
        assert "1" in pads
        assert "2" in pads

    def test_get_pads_unknown(self, capsys):
        """Test getting pads for unknown footprint returns fallback."""
        from kicad_tools.pcb.footprints import FootprintLibrary

        lib = FootprintLibrary()
        pads = lib.get_pads("Unknown:Some_Footprint")

        # Should return default 2-pad layout
        assert "1" in pads
        assert "2" in pads
        assert pads["1"] == (-0.5, 0)
        assert pads["2"] == (0.5, 0)

        # Should print warning
        captured = capsys.readouterr()
        assert "Warning" in captured.out or "Unknown" in captured.out

    def test_get_pads_caching(self):
        """Test that pads are cached."""
        from kicad_tools.pcb.footprints import FootprintLibrary

        lib = FootprintLibrary()

        # First call
        pads1 = lib.get_pads("Capacitor_SMD:C_0603_1608Metric")

        # Second call should return cached value
        pads2 = lib.get_pads("Capacitor_SMD:C_0603_1608Metric")

        assert pads1 is pads2

    def test_get_footprint_pads_convenience(self):
        """Test convenience function."""
        from kicad_tools.pcb.footprints import get_footprint_pads

        pads = get_footprint_pads("R_0603_1608Metric")
        assert "1" in pads
        assert "2" in pads

    def test_sot23_5_pinout(self):
        """Test SOT-23-5 pinout."""
        from kicad_tools.pcb.footprints import get_footprint_pads

        pads = get_footprint_pads("SOT-23-5")

        # Should have 5 pins
        assert len(pads) == 5
        assert "1" in pads
        assert "5" in pads

    def test_tssop_20_pinout(self):
        """Test TSSOP-20 pinout."""
        from kicad_tools.pcb.footprints import get_footprint_pads

        pads = get_footprint_pads("TSSOP-20_4.4x6.5mm_P0.65mm")

        # Should have 20 pins
        assert len(pads) == 20
        assert "1" in pads
        assert "20" in pads

    def test_oscillator_pinout(self):
        """Test oscillator footprint."""
        from kicad_tools.pcb.footprints import get_footprint_pads

        pads = get_footprint_pads("Oscillator_SMD_3.2x2.5mm")

        # Should have 4 pins
        assert len(pads) == 4
        assert "1" in pads
        assert "4" in pads

    def test_common_footprints_data(self):
        """Test COMMON_FOOTPRINTS data is valid."""
        from kicad_tools.pcb.footprints import COMMON_FOOTPRINTS

        for _name, pads in COMMON_FOOTPRINTS.items():
            assert isinstance(pads, dict)
            for pad_name, pos in pads.items():
                assert isinstance(pad_name, str)
                assert len(pos) == 2
                assert isinstance(pos[0], (int, float))
                assert isinstance(pos[1], (int, float))

    def test_footprint_aliases_valid(self):
        """Test FOOTPRINT_ALIASES resolve to valid footprints."""
        from kicad_tools.pcb.footprints import COMMON_FOOTPRINTS, FOOTPRINT_ALIASES

        for alias, full_name in FOOTPRINT_ALIASES.items():
            assert full_name in COMMON_FOOTPRINTS, f"Alias {alias} -> {full_name} not found"


class TestZoneParsing:
    """Tests for zone parsing with full polygon and thermal relief support."""

    def test_zone_count(self, zone_test_pcb):
        """Test that all zones are parsed."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        assert len(pcb.zones) == 3

    def test_zone_basic_properties(self, zone_test_pcb):
        """Test basic zone properties are parsed correctly."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        # First zone - GND on F.Cu
        zone = pcb.zones[0]
        assert zone.net_number == 1
        assert zone.net_name == "GND"
        assert zone.layer == "F.Cu"
        assert zone.uuid == "zone-uuid-1"
        assert zone.name == "GND_Zone"

    def test_zone_priority(self, zone_test_pcb):
        """Test zone priority parsing."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        assert pcb.zones[0].priority == 1
        assert pcb.zones[1].priority == 0
        assert pcb.zones[2].priority == 0  # Default

    def test_zone_min_thickness(self, zone_test_pcb):
        """Test minimum thickness parsing."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        assert pcb.zones[0].min_thickness == 0.15
        assert pcb.zones[1].min_thickness == 0.2

    def test_zone_clearance(self, zone_test_pcb):
        """Test clearance parsing from connect_pads."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        assert pcb.zones[0].clearance == 0.25
        assert pcb.zones[1].clearance == 0.2

    def test_zone_thermal_parameters(self, zone_test_pcb):
        """Test thermal gap and bridge width parsing."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        # First zone with custom thermal settings
        assert pcb.zones[0].thermal_gap == 0.4
        assert pcb.zones[0].thermal_bridge_width == 0.35

        # Second zone with different settings
        assert pcb.zones[1].thermal_gap == 0.3
        assert pcb.zones[1].thermal_bridge_width == 0.3

    def test_zone_connect_pads_types(self, zone_test_pcb):
        """Test different connect_pads settings."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        # First zone - default thermal (only clearance specified)
        assert pcb.zones[0].connect_pads == "thermal_reliefs"

        # Second zone - solid connection (yes)
        assert pcb.zones[1].connect_pads == "solid"

        # Third zone - no connection (no)
        assert pcb.zones[2].connect_pads == "none"

    def test_zone_is_filled(self, zone_test_pcb):
        """Test fill status parsing."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        assert pcb.zones[0].is_filled is True
        assert pcb.zones[1].is_filled is False
        assert pcb.zones[2].is_filled is True

    def test_zone_polygon_parsing(self, zone_test_pcb):
        """Test boundary polygon parsing."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        # First zone - rectangular
        polygon = pcb.zones[0].polygon
        assert len(polygon) == 4
        assert polygon[0] == (100.0, 100.0)
        assert polygon[1] == (130.0, 100.0)
        assert polygon[2] == (130.0, 120.0)
        assert polygon[3] == (100.0, 120.0)

        # Third zone - hexagonal (6 points)
        polygon = pcb.zones[2].polygon
        assert len(polygon) == 6
        assert polygon[0] == (140.0, 100.0)
        assert polygon[5] == (140.0, 120.0)

    def test_zone_filled_polygon_parsing(self, zone_test_pcb):
        """Test filled polygon parsing."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        # First zone has a filled polygon
        filled = pcb.zones[0].filled_polygons
        assert len(filled) == 1
        assert len(filled[0]) == 4
        assert filled[0][0] == (100.1, 100.1)

        # Second zone has no filled polygons (fill no)
        assert len(pcb.zones[1].filled_polygons) == 0

    def test_zone_defaults(self, zone_test_pcb):
        """Test default values for optional zone properties."""
        doc = load_pcb(str(zone_test_pcb))
        pcb = PCB(doc)

        # Third zone uses many defaults
        zone = pcb.zones[2]
        assert zone.fill_type == "solid"  # Default


class TestPCBQueryMethods:
    """Tests for PCB query methods."""

    def test_get_footprint_by_reference(self, routing_test_pcb):
        """Test finding footprint by reference.

        Note: Uses routing_test_pcb which has fp_text tags that the parser handles.
        The minimal_pcb uses property tags which are not yet parsed.
        """
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        # Should find R1
        fp = pcb.get_footprint("R1")
        assert fp is not None
        assert fp.reference == "R1"

    def test_get_footprint_not_found(self, routing_test_pcb):
        """Test footprint not found returns None."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        fp = pcb.get_footprint("U99")
        assert fp is None

    def test_get_net_by_name(self, minimal_pcb):
        """Test finding net by name."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        net = pcb.get_net_by_name("GND")
        assert net is not None
        assert net.name == "GND"

    def test_get_net_not_found(self, minimal_pcb):
        """Test net not found returns None."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        net = pcb.get_net_by_name("NONEXISTENT_NET")
        assert net is None


class TestUpdateFootprintPosition:
    """Tests for PCB.update_footprint_position method."""

    def test_update_position_basic(self, routing_test_pcb, tmp_path):
        """Test updating footprint position persists after save/reload."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        # Verify footprint exists
        fp = pcb.get_footprint("R1")
        assert fp is not None

        # Update position
        new_x, new_y = 140.0, 120.0
        result = pcb.update_footprint_position("R1", new_x, new_y)
        assert result is True

        # Verify in memory
        fp = pcb.get_footprint("R1")
        assert fp.position == pytest.approx((new_x, new_y))

        # Save and reload
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(output_path)

        doc2 = load_pcb(str(output_path))
        pcb2 = PCB(doc2)

        # Verify position persisted
        fp2 = pcb2.get_footprint("R1")
        assert fp2 is not None
        assert fp2.position == pytest.approx((new_x, new_y))

    def test_update_position_with_rotation_existing(self, routing_test_pcb, tmp_path):
        """Test updating position with rotation when at node already has rotation."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        # First set a rotation, then update with different rotation
        pcb.update_footprint_position("R1", 135.0, 115.0, rotation=45.0)

        # Save and reload
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(output_path)

        doc2 = load_pcb(str(output_path))
        pcb2 = PCB(doc2)

        # Now update again with new rotation
        pcb2.update_footprint_position("R1", 140.0, 120.0, rotation=90.0)

        # Save and reload again
        output_path2 = tmp_path / "output2.kicad_pcb"
        pcb2.save(output_path2)

        doc3 = load_pcb(str(output_path2))
        pcb3 = PCB(doc3)

        # Verify both position and rotation persisted
        fp3 = pcb3.get_footprint("R1")
        assert fp3 is not None
        assert fp3.position == pytest.approx((140.0, 120.0))
        assert fp3.rotation == pytest.approx(90.0)

    def test_update_position_with_rotation_new(self, routing_test_pcb, tmp_path):
        """Test updating position with rotation when at node has no rotation.

        This is a regression test for issue #915 where rotation was not
        persisted when the footprint's at node didn't already have a rotation
        value.
        """
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        # The R1 footprint starts without rotation (at 135 115)
        fp = pcb.get_footprint("R1")
        assert fp is not None
        # Initial rotation should be 0 or unset
        assert fp.rotation == pytest.approx(0.0, abs=0.1)

        # Update with non-zero rotation
        new_x, new_y, new_rot = 140.0, 120.0, 45.0
        result = pcb.update_footprint_position("R1", new_x, new_y, rotation=new_rot)
        assert result is True

        # Verify in memory
        fp = pcb.get_footprint("R1")
        assert fp.position == pytest.approx((new_x, new_y))
        assert fp.rotation == pytest.approx(new_rot)

        # Save and reload
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(output_path)

        doc2 = load_pcb(str(output_path))
        pcb2 = PCB(doc2)

        # Verify position AND rotation persisted
        fp2 = pcb2.get_footprint("R1")
        assert fp2 is not None
        assert fp2.position == pytest.approx((new_x, new_y))
        assert fp2.rotation == pytest.approx(new_rot)

    def test_update_position_nonexistent_footprint(self, routing_test_pcb):
        """Test that updating nonexistent footprint returns False."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        result = pcb.update_footprint_position("NONEXISTENT", 100.0, 100.0)
        assert result is False


class TestAddFootprint:
    """Tests for PCB.add_footprint and add_footprint_from_file methods."""

    # Path to test fixtures
    FIXTURES_DIR = Path(__file__).parent / "fixtures"
    TEST_PRETTY_DIR = FIXTURES_DIR / "Test_Library.pretty"

    def test_add_footprint_from_file_basic(self, minimal_pcb):
        """Test adding a footprint from a .kicad_mod file."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)
        initial_count = len(pcb.footprints)

        # Add footprint
        fp = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
        )

        # Verify footprint was added
        assert fp is not None
        assert fp.reference == "C1"
        assert len(pcb.footprints) == initial_count + 1

    def test_add_footprint_from_file_with_position(self, minimal_pcb):
        """Test that footprint position is set correctly."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        fp = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
            rotation=90.0,
        )

        assert fp.position == pytest.approx((50.0, 30.0))
        assert fp.rotation == pytest.approx(90.0)

    def test_add_footprint_from_file_with_value(self, minimal_pcb):
        """Test that footprint value is set correctly."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        fp = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
            value="100nF",
        )

        assert fp.value == "100nF"

    def test_add_footprint_from_file_with_layer(self, minimal_pcb):
        """Test that footprint layer is set correctly."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        fp = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
            layer="B.Cu",
        )

        assert fp.layer == "B.Cu"

    def test_add_footprint_from_file_has_pads(self, minimal_pcb):
        """Test that added footprint has pads."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        fp = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
        )

        # The C_0402_1005Metric footprint has 2 pads
        assert len(fp.pads) == 2

    def test_add_footprint_from_file_unique_uuid(self, minimal_pcb):
        """Test that each added footprint gets a unique UUID."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        fp1 = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
        )
        fp2 = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C2",
            x=60.0,
            y=30.0,
        )

        assert fp1.uuid != fp2.uuid
        assert len(fp1.uuid) > 0
        assert len(fp2.uuid) > 0

    def test_add_footprint_from_file_get_by_reference(self, minimal_pcb):
        """Test that added footprint can be found by reference."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
        )

        found = pcb.get_footprint("C1")
        assert found is not None
        assert found.reference == "C1"

    def test_add_footprint_from_file_save_roundtrip(self, minimal_pcb, tmp_path):
        """Test that PCB with added footprint can be saved and reloaded."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
            value="100nF",
        )

        # Save to new file
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(output_path)

        # Reload and verify
        doc2 = load_pcb(str(output_path))
        pcb2 = PCB(doc2)

        fp = pcb2.get_footprint("C1")
        assert fp is not None
        assert fp.reference == "C1"
        assert fp.value == "100nF"
        assert fp.position == pytest.approx((50.0, 30.0))

    def test_add_footprint_from_file_multipad_footprint(self, minimal_pcb):
        """Test adding a footprint with multiple pads."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        fp = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "SOT-23-5.kicad_mod",
            reference="U1",
            x=80.0,
            y=25.0,
            value="LM317",
        )

        # SOT-23-5 has 5 pads
        assert len(fp.pads) == 5
        assert fp.reference == "U1"
        assert fp.value == "LM317"

    def test_add_footprint_from_file_nonexistent_raises(self, minimal_pcb):
        """Test that adding a nonexistent file raises an error."""
        from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError

        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        with pytest.raises(KiCadFileNotFoundError):
            pcb.add_footprint_from_file(
                kicad_mod_path="/nonexistent/footprint.kicad_mod",
                reference="C1",
                x=50.0,
                y=30.0,
            )

    def test_add_multiple_footprints(self, minimal_pcb):
        """Test adding multiple footprints to a PCB."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)
        initial_count = len(pcb.footprints)

        # Add multiple footprints
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
            value="100nF",
        )
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "R_0603_1608Metric.kicad_mod",
            reference="R1",
            x=60.0,
            y=30.0,
            value="10k",
        )
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "SOT-23-5.kicad_mod",
            reference="U1",
            x=80.0,
            y=25.0,
            value="LM317",
        )

        assert len(pcb.footprints) == initial_count + 3

        # Verify each can be found
        assert pcb.get_footprint("C1") is not None
        assert pcb.get_footprint("R1") is not None
        assert pcb.get_footprint("U1") is not None

    def test_add_footprint_at_node_position_in_sexp(self, minimal_pcb, tmp_path):
        """Test that (at) node is positioned correctly after (layer) in S-expression.

        This tests the fix for issue #910 where add_footprint() was appending
        the (at) node at the end of the footprint S-expression instead of
        immediately after the (layer) node. KiCad expects this specific ordering.
        """
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        # Add footprint with specific position
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
            rotation=45.0,
        )

        # Save to file
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(output_path)

        # Read raw content and verify structure
        content = output_path.read_text()

        # Find the footprint section
        fp_start = content.find('(footprint "C_0402_1005Metric"')
        assert fp_start != -1, "Footprint not found in saved file"

        # Extract the footprint section (find matching closing paren)
        fp_content = content[fp_start : fp_start + 2000]  # Enough for the footprint

        # Verify (at) appears after (layer) and before other elements
        layer_pos = fp_content.find('(layer "F.Cu")')
        at_pos = fp_content.find("(at 50 30 45)")
        assert layer_pos != -1, "(layer) node not found"
        assert at_pos != -1, "(at) node with correct values not found"
        assert at_pos > layer_pos, "(at) node should appear after (layer) node"

        # Verify (at) is not at the very end (before closing paren of footprint)
        # This checks that it wasn't just appended to the end
        descr_pos = fp_content.find("(descr")
        tags_pos = fp_content.find("(tags")
        if descr_pos != -1:
            assert at_pos < descr_pos, "(at) should appear before (descr)"
        if tags_pos != -1:
            assert at_pos < tags_pos, "(at) should appear before (tags)"


class TestSilkscreenManagement:
    """Tests for silkscreen management APIs (issue #924)."""

    # Multi-footprint PCB fixture for silkscreen tests
    PCB_WITH_REFS = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-r1") (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "val-r1"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 110 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-c1") (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "val-c1"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000003")
    (at 120 100)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-c2") (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "10uF" (at 0 1.5 0) (layer "F.Fab") (uuid "val-c2"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000004")
    (at 100 120)
    (property "Reference" "U1" (at 0 -4 0) (layer "F.SilkS") (uuid "ref-u1") (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "LM358" (at 0 4 0) (layer "F.Fab") (uuid "val-u1"))
    (pad "1" smd rect (at -2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
)"""

    @pytest.fixture
    def pcb_with_refs(self, tmp_path: Path) -> Path:
        """Create a PCB with multiple footprints for silkscreen testing."""
        pcb_file = tmp_path / "silkscreen_test.kicad_pcb"
        pcb_file.write_text(self.PCB_WITH_REFS)
        return pcb_file

    def test_set_reference_visibility_all_hide(self, pcb_with_refs):
        """Test hiding all reference designators."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        count = pcb.set_reference_visibility(visible=False)

        # Should update all 4 footprints
        assert count == 4

        # Verify parsed objects are updated
        for fp in pcb.footprints:
            for text in fp.texts:
                if text.text_type == "reference":
                    assert text.hidden is True

    def test_set_reference_visibility_specific(self, pcb_with_refs):
        """Test hiding a specific reference designator."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        count = pcb.set_reference_visibility("R1", visible=False)

        assert count == 1

        # Verify only R1 is hidden
        for fp in pcb.footprints:
            for text in fp.texts:
                if text.text_type == "reference":
                    if fp.reference == "R1":
                        assert text.hidden is True
                    else:
                        assert text.hidden is False

    def test_set_reference_visibility_pattern(self, pcb_with_refs):
        """Test hiding references matching a pattern."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        count = pcb.set_reference_visibility(pattern="C*", visible=False)

        # Should update C1 and C2
        assert count == 2

        for fp in pcb.footprints:
            for text in fp.texts:
                if text.text_type == "reference":
                    if fp.reference.startswith("C"):
                        assert text.hidden is True
                    else:
                        assert text.hidden is False

    def test_set_reference_visibility_show_after_hide(self, pcb_with_refs):
        """Test showing references after hiding them."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        # Hide all
        pcb.set_reference_visibility(visible=False)
        # Show one
        count = pcb.set_reference_visibility("U1", visible=True)

        assert count == 1

        for fp in pcb.footprints:
            for text in fp.texts:
                if text.text_type == "reference":
                    if fp.reference == "U1":
                        assert text.hidden is False
                    else:
                        assert text.hidden is True

    def test_move_reference_offset(self, pcb_with_refs):
        """Test moving reference with offset."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        # Get original position
        original_pos = None
        for fp in pcb.footprints:
            if fp.reference == "R1":
                for text in fp.texts:
                    if text.text_type == "reference":
                        original_pos = text.position
                        break
                break

        result = pcb.move_reference("R1", offset=(2.0, -3.0))

        assert result is True

        # Verify position changed
        for fp in pcb.footprints:
            if fp.reference == "R1":
                for text in fp.texts:
                    if text.text_type == "reference":
                        assert text.position[0] == pytest.approx(original_pos[0] + 2.0)
                        assert text.position[1] == pytest.approx(original_pos[1] - 3.0)
                        break
                break

    def test_move_reference_absolute(self, pcb_with_refs):
        """Test moving reference to absolute position."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        result = pcb.move_reference("C1", absolute=(5.0, -4.0))

        assert result is True

        for fp in pcb.footprints:
            if fp.reference == "C1":
                for text in fp.texts:
                    if text.text_type == "reference":
                        assert text.position == pytest.approx((5.0, -4.0))
                        break
                break

    def test_move_reference_with_layer(self, pcb_with_refs):
        """Test moving reference with layer change."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        result = pcb.move_reference("U1", offset=(0.0, 0.0), layer="F.Fab")

        assert result is True

        for fp in pcb.footprints:
            if fp.reference == "U1":
                for text in fp.texts:
                    if text.text_type == "reference":
                        assert text.layer == "F.Fab"
                        break
                break

    def test_move_reference_not_found(self, pcb_with_refs):
        """Test moving non-existent reference returns False."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        result = pcb.move_reference("NONEXISTENT", offset=(1.0, 1.0))

        assert result is False

    def test_set_silkscreen_font_all(self, pcb_with_refs):
        """Test setting font for all references."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        count = pcb.set_silkscreen_font(size=0.8, thickness=0.12)

        assert count == 4

        for fp in pcb.footprints:
            for text in fp.texts:
                if text.text_type == "reference":
                    assert text.font_size == pytest.approx((0.8, 0.8))
                    assert text.font_thickness == pytest.approx(0.12)

    def test_set_silkscreen_font_pattern(self, pcb_with_refs):
        """Test setting font for pattern-matched references."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        count = pcb.set_silkscreen_font(size=0.6, thickness=0.1, pattern="C*")

        assert count == 2

        for fp in pcb.footprints:
            for text in fp.texts:
                if text.text_type == "reference":
                    if fp.reference.startswith("C"):
                        assert text.font_size == pytest.approx((0.6, 0.6))
                    else:
                        # Original size
                        assert text.font_size == pytest.approx((1.0, 1.0))

    def test_set_silkscreen_font_tuple_size(self, pcb_with_refs):
        """Test setting font with different width and height."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        count = pcb.set_silkscreen_font(size=(0.7, 0.9), thickness=0.15)

        assert count == 4

        for fp in pcb.footprints:
            for text in fp.texts:
                if text.text_type == "reference":
                    assert text.font_size == pytest.approx((0.7, 0.9))

    def test_move_references_to_layer_all(self, pcb_with_refs):
        """Test moving all references to a different layer."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        count = pcb.move_references_to_layer("F.Fab")

        assert count == 4

        for fp in pcb.footprints:
            for text in fp.texts:
                if text.text_type == "reference":
                    assert text.layer == "F.Fab"

    def test_move_references_to_layer_pattern(self, pcb_with_refs):
        """Test moving pattern-matched references to different layer."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        count = pcb.move_references_to_layer("F.Fab", pattern="R*")

        assert count == 1

        for fp in pcb.footprints:
            for text in fp.texts:
                if text.text_type == "reference":
                    if fp.reference.startswith("R"):
                        assert text.layer == "F.Fab"
                    else:
                        assert text.layer == "F.SilkS"

    def test_validate_silkscreen_returns_list(self, pcb_with_refs):
        """Test that validate_silkscreen returns a list of issues."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        issues = pcb.validate_silkscreen()

        # Returns a list (may be empty for valid PCB)
        assert isinstance(issues, list)

    def test_validate_silkscreen_small_font(self, pcb_with_refs):
        """Test that validate_silkscreen detects small font."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        # Set very small font (below minimum)
        pcb.set_silkscreen_font(size=0.5, thickness=0.1)

        from kicad_tools.manufacturers import DesignRules

        rules = DesignRules(
            min_trace_width_mm=0.127,
            min_clearance_mm=0.127,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.5,
            min_annular_ring_mm=0.127,
            min_silkscreen_height_mm=0.8,
            min_silkscreen_width_mm=0.15,
        )
        issues = pcb.validate_silkscreen(design_rules=rules)

        # Should detect font height issues
        text_height_issues = [i for i in issues if i["type"] == "text_height"]
        assert len(text_height_issues) > 0

    def test_silkscreen_changes_persist_on_save(self, pcb_with_refs, tmp_path):
        """Test that silkscreen changes persist after save/reload."""
        doc = load_pcb(str(pcb_with_refs))
        pcb = PCB(doc)

        # Make changes
        pcb.set_reference_visibility("R1", visible=False)
        pcb.move_reference("C1", offset=(1.0, -2.0))
        pcb.set_silkscreen_font(size=0.9, thickness=0.12, pattern="U*")

        # Save
        output_path = tmp_path / "modified.kicad_pcb"
        pcb.save(str(output_path))

        # Reload
        doc2 = load_pcb(str(output_path))
        pcb2 = PCB(doc2)

        # Verify changes persisted
        for fp in pcb2.footprints:
            for text in fp.texts:
                if text.text_type == "reference":
                    if fp.reference == "R1":
                        assert text.hidden is True
                    if fp.reference == "U1":
                        assert text.font_size == pytest.approx((0.9, 0.9))


class TestTraceRoutingAPI:
    """Tests for the trace routing API (add_trace, add_via, routing_status)."""

    def test_add_trace_between_coordinates(self, tmp_path):
        """Test adding a trace between two coordinate positions."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_net("TestNet")

        segments = pcb.add_trace(
            start=(10.0, 10.0),
            end=(50.0, 10.0),
            width=0.3,
            layer="F.Cu",
            net="TestNet",
        )

        assert len(segments) == 1
        seg = segments[0]
        assert seg.start == (10.0, 10.0)
        assert seg.end == (50.0, 10.0)
        assert seg.width == 0.3
        assert seg.layer == "F.Cu"
        assert seg.net_number > 0

        # Verify the segment is in the PCB
        assert len(pcb.segments) == 1

    def test_add_trace_with_waypoints(self, tmp_path):
        """Test adding a trace with intermediate waypoints."""
        pcb = PCB.create(width=100, height=100)

        segments = pcb.add_trace(
            start=(10.0, 10.0),
            end=(50.0, 50.0),
            width=0.25,
            layer="F.Cu",
            net="Signal1",
            waypoints=[(30.0, 10.0), (30.0, 50.0)],
        )

        # Should create 3 segments: start->wp1, wp1->wp2, wp2->end
        assert len(segments) == 3
        assert segments[0].start == (10.0, 10.0)
        assert segments[0].end == (30.0, 10.0)
        assert segments[1].start == (30.0, 10.0)
        assert segments[1].end == (30.0, 50.0)
        assert segments[2].start == (30.0, 50.0)
        assert segments[2].end == (50.0, 50.0)

    def test_add_trace_persists_on_save(self, tmp_path):
        """Test that added traces persist after save/reload."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(
            start=(10.0, 10.0),
            end=(50.0, 10.0),
            width=0.25,
            layer="F.Cu",
            net="TestNet",
        )

        # Save and reload
        output_path = tmp_path / "with_trace.kicad_pcb"
        pcb.save(str(output_path))

        pcb2 = PCB.load(str(output_path))
        assert len(pcb2.segments) == 1
        seg = pcb2.segments[0]
        assert seg.start == pytest.approx((10.0, 10.0))
        assert seg.end == pytest.approx((50.0, 10.0))
        assert seg.width == pytest.approx(0.25)
        assert seg.layer == "F.Cu"

    def test_add_via_basic(self, tmp_path):
        """Test adding a via at a position."""
        pcb = PCB.create(width=100, height=100)

        via = pcb.add_via(
            x=50.0,
            y=30.0,
            size=0.6,
            drill=0.3,
            layers=("F.Cu", "B.Cu"),
            net="VCC",
        )

        assert via.position == (50.0, 30.0)
        assert via.size == 0.6
        assert via.drill == 0.3
        assert via.layers == ["F.Cu", "B.Cu"]
        assert via.net_number > 0

        # Verify the via is in the PCB
        assert len(pcb.vias) == 1

    def test_add_via_persists_on_save(self, tmp_path):
        """Test that added vias persist after save/reload."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_via(x=25.0, y=25.0, size=0.8, drill=0.4, net="GND")

        # Save and reload
        output_path = tmp_path / "with_via.kicad_pcb"
        pcb.save(str(output_path))

        pcb2 = PCB.load(str(output_path))
        assert len(pcb2.vias) == 1
        via = pcb2.vias[0]
        assert via.position == pytest.approx((25.0, 25.0))
        assert via.size == pytest.approx(0.8)
        assert via.drill == pytest.approx(0.4)

    def test_routing_status_empty_pcb(self, tmp_path):
        """Test routing_status on a PCB with no traces."""
        pcb = PCB.create(width=100, height=100)

        status = pcb.routing_status()

        assert status["segments"] == 0
        assert status["vias"] == 0
        assert status["trace_length_mm"] == 0.0
        assert len(status["nets_with_traces"]) == 0

    def test_routing_status_with_traces(self, tmp_path):
        """Test routing_status with added traces."""
        pcb = PCB.create(width=100, height=100)

        # Add a 40mm horizontal trace
        pcb.add_trace(
            start=(10.0, 10.0),
            end=(50.0, 10.0),
            width=0.25,
            layer="F.Cu",
            net="TestNet",
        )

        # Add a via
        pcb.add_via(x=30.0, y=20.0, net="TestNet")

        status = pcb.routing_status()

        assert status["segments"] == 1
        assert status["vias"] == 1
        assert status["trace_length_mm"] == pytest.approx(40.0)
        assert len(status["nets_with_traces"]) >= 1

    def test_get_pad_position_no_rotation(self, minimal_pcb):
        """Test get_pad_position for a footprint with no rotation."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        # Get the first footprint's pad position
        fp = pcb.footprints[0]
        if fp.pads:
            pos = pcb.get_pad_position(fp.reference, fp.pads[0].number)
            assert pos is not None
            # Position should be footprint position + pad offset
            expected_x = fp.position[0] + fp.pads[0].position[0]
            expected_y = fp.position[1] + fp.pads[0].position[1]
            assert pos[0] == pytest.approx(expected_x)
            assert pos[1] == pytest.approx(expected_y)

    def test_get_pad_position_not_found(self, minimal_pcb):
        """Test get_pad_position returns None for missing pad."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        # Non-existent footprint
        assert pcb.get_pad_position("NONEXISTENT", "1") is None

        # Non-existent pad on existing footprint
        fp = pcb.footprints[0]
        assert pcb.get_pad_position(fp.reference, "999") is None

    def test_get_ratsnest_empty(self, tmp_path):
        """Test get_ratsnest on a PCB with no multi-pad nets."""
        pcb = PCB.create(width=100, height=100)

        ratsnest = pcb.get_ratsnest()

        # No footprints means no ratsnest
        assert ratsnest == []

    def test_segment_to_sexp(self):
        """Test Segment.to_sexp() produces valid S-expression."""
        from kicad_tools.schema.pcb import Segment

        seg = Segment(
            start=(10.0, 20.0),
            end=(30.0, 40.0),
            width=0.25,
            layer="F.Cu",
            net_number=5,
        )

        sexp = seg.to_sexp()

        assert sexp.name == "segment"
        start = sexp.find("start")
        assert start is not None
        assert start.get_float(0) == 10.0
        assert start.get_float(1) == 20.0

        end = sexp.find("end")
        assert end is not None
        assert end.get_float(0) == 30.0
        assert end.get_float(1) == 40.0

        width = sexp.find("width")
        assert width is not None
        assert width.get_float(0) == 0.25

        layer = sexp.find("layer")
        assert layer is not None
        assert layer.get_string(0) == "F.Cu"

        net = sexp.find("net")
        assert net is not None
        assert net.get_int(0) == 5

        # UUID should be auto-generated
        uuid_node = sexp.find("uuid")
        assert uuid_node is not None
        assert len(uuid_node.get_string(0)) > 0

    def test_via_to_sexp(self):
        """Test Via.to_sexp() produces valid S-expression."""
        from kicad_tools.schema.pcb import Via

        via = Via(
            position=(50.0, 60.0),
            size=0.6,
            drill=0.3,
            layers=["F.Cu", "B.Cu"],
            net_number=3,
        )

        sexp = via.to_sexp()

        assert sexp.name == "via"

        at = sexp.find("at")
        assert at is not None
        assert at.get_float(0) == 50.0
        assert at.get_float(1) == 60.0

        size = sexp.find("size")
        assert size is not None
        assert size.get_float(0) == 0.6

        drill = sexp.find("drill")
        assert drill is not None
        assert drill.get_float(0) == 0.3

        layers = sexp.find("layers")
        assert layers is not None
        assert layers.get_string(0) == "F.Cu"
        assert layers.get_string(1) == "B.Cu"

        net = sexp.find("net")
        assert net is not None
        assert net.get_int(0) == 3

        # UUID should be auto-generated
        uuid_node = sexp.find("uuid")
        assert uuid_node is not None
        assert len(uuid_node.get_string(0)) > 0
