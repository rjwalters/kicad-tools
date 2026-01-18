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
