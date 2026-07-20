"""Tests for PCB parsing and editing."""

from pathlib import Path

import pytest

from kicad_tools import load_pcb
from kicad_tools.schema import PCB

try:
    from kicad_tools.footprints.library_path import detect_kicad_library_path

    _KICAD_FOOTPRINT_LIBS = detect_kicad_library_path().footprints_path is not None
except Exception:
    _KICAD_FOOTPRINT_LIBS = False

requires_kicad_footprint_libs = pytest.mark.skipif(
    not _KICAD_FOOTPRINT_LIBS,
    reason="KiCad footprint libraries not installed",
)


def _edge_cuts_lines(pcb: PCB):
    """Return all (start, end) gr_line segments on Edge.Cuts from the tree."""
    segments = []
    for child in pcb._sexp.iter_children():
        if child.tag != "gr_line":
            continue
        layer = child.find("layer")
        if layer is None or layer.get_string(0) != "Edge.Cuts":
            continue
        start = child.find("start")
        end = child.find("end")
        segments.append(
            (
                (start.get_float(0), start.get_float(1)),
                (end.get_float(0), end.get_float(1)),
            )
        )
    return segments


def _outline_bbox(pcb: PCB):
    """Return ((min_x, min_y), (max_x, max_y)) of the Edge.Cuts gr_line outline."""
    segments = _edge_cuts_lines(pcb)
    xs = [c for seg in segments for c in (seg[0][0], seg[1][0])]
    ys = [c for seg in segments for c in (seg[0][1], seg[1][1])]
    return ((min(xs), min(ys)), (max(xs), max(ys)))


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


class TestFootprintDirectSetPersistence:
    """Regression tests for direct fp.position/rotation/layer assignment.

    Setting ``fp.position`` directly must persist through ``PCB.save()``
    without requiring ``update_footprint_position()``.  This was broken
    before the ``_sexp_node`` back-reference was added to ``Footprint``.

    See https://github.com/rjwalters/kicad-tools/issues/1990
    """

    def test_position_setter_persists(self, routing_test_pcb, tmp_path):
        """Direct position assignment must round-trip through save/load."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        fp = pcb.get_footprint("R1")
        assert fp is not None
        original_pos = fp.position

        # Assign a new position directly (no update_footprint_position).
        new_pos = (original_pos[0] + 5.0, original_pos[1] + 3.0)
        fp.position = new_pos
        assert fp.position == pytest.approx(new_pos)

        # Save and reload.
        out = tmp_path / "pos_direct.kicad_pcb"
        pcb.save(out)

        doc2 = load_pcb(str(out))
        pcb2 = PCB(doc2)
        fp2 = pcb2.get_footprint("R1")
        assert fp2 is not None
        assert fp2.position == pytest.approx(new_pos)

    def test_rotation_setter_persists(self, routing_test_pcb, tmp_path):
        """Direct rotation assignment must round-trip through save/load."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        fp = pcb.get_footprint("R1")
        assert fp is not None
        assert fp.rotation == pytest.approx(0.0, abs=0.1)

        fp.rotation = 90.0
        assert fp.rotation == pytest.approx(90.0)

        out = tmp_path / "rot_direct.kicad_pcb"
        pcb.save(out)

        doc2 = load_pcb(str(out))
        pcb2 = PCB(doc2)
        fp2 = pcb2.get_footprint("R1")
        assert fp2 is not None
        assert fp2.rotation == pytest.approx(90.0)

    def test_layer_setter_persists(self, routing_test_pcb, tmp_path):
        """Direct layer assignment must round-trip through save/load."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        fp = pcb.get_footprint("R1")
        assert fp is not None
        assert fp.layer == "F.Cu"

        fp.layer = "B.Cu"
        assert fp.layer == "B.Cu"

        out = tmp_path / "layer_direct.kicad_pcb"
        pcb.save(out)

        doc2 = load_pcb(str(out))
        pcb2 = PCB(doc2)
        fp2 = pcb2.get_footprint("R1")
        assert fp2 is not None
        assert fp2.layer == "B.Cu"

    def test_position_and_rotation_together(self, routing_test_pcb, tmp_path):
        """Setting both position and rotation directly must persist."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        fp = pcb.get_footprint("U1")
        assert fp is not None

        fp.position = (120.0, 120.0)
        fp.rotation = 45.0

        out = tmp_path / "pos_rot_direct.kicad_pcb"
        pcb.save(out)

        doc2 = load_pcb(str(out))
        pcb2 = PCB(doc2)
        fp2 = pcb2.get_footprint("U1")
        assert fp2 is not None
        assert fp2.position == pytest.approx((120.0, 120.0))
        assert fp2.rotation == pytest.approx(45.0)

    def test_footprint_without_sexp_node(self):
        """Footprint constructed without from_sexp must still work normally."""
        from kicad_tools.schema.pcb import Footprint

        fp = Footprint(
            name="Test",
            layer="F.Cu",
            position=(10.0, 20.0),
            rotation=0.0,
            reference="R1",
            value="10k",
        )
        # No _sexp_node -- setters should not raise.
        fp.position = (30.0, 40.0)
        fp.rotation = 90.0
        fp.layer = "B.Cu"
        assert fp.position == (30.0, 40.0)
        assert fp.rotation == 90.0
        assert fp.layer == "B.Cu"

    def test_update_footprint_position_still_works(self, routing_test_pcb, tmp_path):
        """Existing update_footprint_position must still persist correctly."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        result = pcb.update_footprint_position("R1", 140.0, 120.0, rotation=45.0)
        assert result is True

        out = tmp_path / "update_method.kicad_pcb"
        pcb.save(out)

        doc2 = load_pcb(str(out))
        pcb2 = PCB(doc2)
        fp2 = pcb2.get_footprint("R1")
        assert fp2 is not None
        assert fp2.position == pytest.approx((140.0, 120.0))
        assert fp2.rotation == pytest.approx(45.0)


class TestUpdateFootprintReference:
    """Tests for PCB.update_footprint_reference method."""

    def test_rename_reference_kicad8(self, minimal_pcb, tmp_path):
        """Test renaming a reference designator in KiCad 8+ format (property)."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        # Verify footprint exists
        fp = pcb.get_footprint("R1")
        assert fp is not None

        # Rename R1 -> R100
        result = pcb.update_footprint_reference("R1", "R100")
        assert result is True

        # Verify in memory
        assert pcb.get_footprint("R1") is None
        fp = pcb.get_footprint("R100")
        assert fp is not None
        assert fp.reference == "R100"

        # Verify FootprintText also updated
        ref_texts = [t for t in fp.texts if t.text_type == "reference"]
        assert len(ref_texts) > 0
        assert ref_texts[0].text == "R100"

        # Save and reload
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(output_path)

        doc2 = load_pcb(str(output_path))
        pcb2 = PCB(doc2)

        # Verify persisted
        assert pcb2.get_footprint("R1") is None
        fp2 = pcb2.get_footprint("R100")
        assert fp2 is not None
        assert fp2.reference == "R100"

    def test_rename_reference_kicad7(self, routing_test_pcb, tmp_path):
        """Test renaming a reference designator in KiCad 7 format (fp_text)."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        # Rename R1 -> R50
        result = pcb.update_footprint_reference("R1", "R50")
        assert result is True

        # Verify in memory
        assert pcb.get_footprint("R1") is None
        fp = pcb.get_footprint("R50")
        assert fp is not None
        assert fp.reference == "R50"

        # Save and reload
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(output_path)

        doc2 = load_pcb(str(output_path))
        pcb2 = PCB(doc2)

        assert pcb2.get_footprint("R1") is None
        fp2 = pcb2.get_footprint("R50")
        assert fp2 is not None
        assert fp2.reference == "R50"

    def test_rename_reference_collision(self, routing_test_pcb):
        """Test that renaming to an existing reference returns False."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        # U1 and R1 both exist -- renaming R1 to U1 should fail
        result = pcb.update_footprint_reference("R1", "U1")
        assert result is False

        # Original should be unchanged
        assert pcb.get_footprint("R1") is not None
        assert pcb.get_footprint("R1").reference == "R1"

    def test_rename_reference_nonexistent(self, routing_test_pcb):
        """Test that renaming a nonexistent reference returns False."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        result = pcb.update_footprint_reference("NONEXISTENT", "R99")
        assert result is False

    def test_rename_reference_same_name(self, routing_test_pcb):
        """Test renaming to the same name succeeds (no-op)."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        result = pcb.update_footprint_reference("R1", "R1")
        assert result is True
        assert pcb.get_footprint("R1") is not None


class TestUpdateFootprintValue:
    """Tests for PCB.update_footprint_value method."""

    def test_update_value_kicad8(self, minimal_pcb, tmp_path):
        """Test updating value in KiCad 8+ format (property)."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        # The minimal_pcb has R1 with value "10k"
        fp = pcb.get_footprint("R1")
        assert fp is not None
        assert fp.value == "10k"

        # Update value
        result = pcb.update_footprint_value("R1", "4.7k")
        assert result is True

        # Verify in memory
        fp = pcb.get_footprint("R1")
        assert fp.value == "4.7k"

        # Verify FootprintText also updated
        val_texts = [t for t in fp.texts if t.text_type == "value"]
        assert len(val_texts) > 0
        assert val_texts[0].text == "4.7k"

        # Save and reload
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(output_path)

        doc2 = load_pcb(str(output_path))
        pcb2 = PCB(doc2)

        fp2 = pcb2.get_footprint("R1")
        assert fp2 is not None
        assert fp2.value == "4.7k"

    def test_update_value_kicad7(self, tmp_path):
        """Test updating value in KiCad 7 format (fp_text)."""
        # Create a KiCad 7-style PCB with fp_text value entries
        pcb_content = """(kicad_pcb
  (version 20171130)
  (generator "test")
  (general
    (thickness 1.6)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_rect (start 100 100) (end 150 140)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 120 120)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS"))
    (fp_text value "10k" (at 0 1.5) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 0 ""))
  )
)
"""
        pcb_file = tmp_path / "kicad7.kicad_pcb"
        pcb_file.write_text(pcb_content)

        doc = load_pcb(str(pcb_file))
        pcb = PCB(doc)

        fp = pcb.get_footprint("R1")
        assert fp is not None
        assert fp.value == "10k"

        # Update value
        result = pcb.update_footprint_value("R1", "4.7k")
        assert result is True

        # Verify in memory
        fp = pcb.get_footprint("R1")
        assert fp.value == "4.7k"

        # Save and reload
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(output_path)

        doc2 = load_pcb(str(output_path))
        pcb2 = PCB(doc2)

        fp2 = pcb2.get_footprint("R1")
        assert fp2 is not None
        assert fp2.value == "4.7k"

    def test_update_value_nonexistent(self, routing_test_pcb):
        """Test that updating value of nonexistent reference returns False."""
        doc = load_pcb(str(routing_test_pcb))
        pcb = PCB(doc)

        result = pcb.update_footprint_value("NONEXISTENT", "4.7k")
        assert result is False

    def test_update_value_round_trip(self, minimal_pcb, tmp_path):
        """Test that updating value does not corrupt other footprint fields."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        # Record original state
        fp = pcb.get_footprint("R1")
        original_pos = fp.position
        original_ref = fp.reference

        # Update value
        pcb.update_footprint_value("R1", "100k")

        # Save and reload
        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(output_path)

        doc2 = load_pcb(str(output_path))
        pcb2 = PCB(doc2)

        fp2 = pcb2.get_footprint("R1")
        assert fp2.value == "100k"
        assert fp2.position == pytest.approx(original_pos)
        assert fp2.reference == original_ref


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


class TestAddFootprintPadAngleAbsolute:
    """Writer must emit pad angles as ABSOLUTE (fp_rotation + local) (issue #3902).

    KiCad stores a pad's ``(at x y ANGLE)`` in the ABSOLUTE board frame -- the
    angle already includes the parent footprint rotation. A library
    ``.kicad_mod`` is authored at rotation 0, so when we place it at a non-zero
    rotation the writer must fold that rotation into every pad angle. Emitting
    the LOCAL angle instead renders elongated pads unrotated in KiCad and
    produces phantom shorting / solder-mask-bridge DRC violations plus wrong
    gerber apertures.
    """

    FIXTURES_DIR = Path(__file__).parent / "fixtures"
    TEST_PRETTY_DIR = FIXTURES_DIR / "RotatedPad_Test.pretty"

    def _pad_angles(self, fp) -> dict[str, float]:
        return {pad.number: pad.rotation for pad in fp.pads}

    def test_zero_rotation_leaves_pad_angles_unchanged(self, minimal_pcb):
        """A footprint placed at rotation 0 keeps each pad's authored angle."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        fp = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "RotatedPad_Test.kicad_mod",
            reference="U1",
            x=50.0,
            y=30.0,
            rotation=0.0,
        )

        angles = self._pad_angles(fp)
        # Pad 1 authored at 0, pad 2 authored at 30 -> unchanged at rotation 0.
        assert angles["1"] == pytest.approx(0.0)
        assert angles["2"] == pytest.approx(30.0)

    def test_positive_90_folds_into_pad_angles(self, minimal_pcb):
        """Placing at +90 emits absolute angles (local + 90) % 360."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        fp = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "RotatedPad_Test.kicad_mod",
            reference="U1",
            x=50.0,
            y=30.0,
            rotation=90.0,
        )

        angles = self._pad_angles(fp)
        assert angles["1"] == pytest.approx(90.0)  # 0 + 90
        assert angles["2"] == pytest.approx(120.0)  # 30 + 90

    def test_negative_90_folds_and_wraps(self, minimal_pcb):
        """Placing at -90 wraps into [0, 360): local 0 -> 270, local 30 -> 300."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        fp = pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "RotatedPad_Test.kicad_mod",
            reference="U1",
            x=50.0,
            y=30.0,
            rotation=-90.0,
        )

        angles = self._pad_angles(fp)
        # (0 + -90) % 360 == 270 ; (30 + -90) % 360 == 300
        assert angles["1"] == pytest.approx(270.0)
        assert angles["2"] == pytest.approx(300.0)

    def test_emitted_text_contains_absolute_pad_angle(self, minimal_pcb, tmp_path):
        """The serialized board text carries the absolute pad angle."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "RotatedPad_Test.kicad_mod",
            reference="U1",
            x=50.0,
            y=30.0,
            rotation=90.0,
        )
        out = tmp_path / "abs_angle.kicad_pcb"
        pcb.save(out)
        text = out.read_text()

        # Pad 2 (authored local 30) must serialize with absolute angle 120.
        assert "(at 1 0 120)" in text or "(at 1.0 0 120)" in text, (
            f"Pad 2's absolute angle (local 30 + fp 90 = 120) not found in emitted board.\n{text}"
        )
        # Pad 1 (authored local 0) must serialize with absolute angle 90.
        assert "(at -1 0 90)" in text or "(at -1.0 0 90)" in text, (
            f"Pad 1's absolute angle (local 0 + fp 90 = 90) not found in emitted board.\n{text}"
        )

    def test_round_trip_reader_reports_absolute_angle(self, minimal_pcb, tmp_path):
        """Save then reload: the reader reports the same ABSOLUTE pad angle.

        Confirms writer/reader agree on the convention (no double counting).
        """
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "RotatedPad_Test.kicad_mod",
            reference="U1",
            x=50.0,
            y=30.0,
            rotation=90.0,
        )
        out = tmp_path / "roundtrip.kicad_pcb"
        pcb.save(out)

        reloaded = PCB(load_pcb(str(out)))
        fp = next(f for f in reloaded.footprints if f.reference == "U1")
        angles = {pad.number: pad.rotation for pad in fp.pads}
        assert angles["1"] == pytest.approx(90.0)
        assert angles["2"] == pytest.approx(120.0)


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


class TestPCBCreate:
    """Tests for PCB.create() method."""

    def test_create_centers_board_by_default(self):
        """Test that PCB.create() centers the board on the drawing sheet by default."""
        # A4 paper is 297 x 210mm
        # Board is 200 x 120mm
        # Offset X: (297 - 200) / 2 = 48.5mm
        # Offset Y: (210 - 120) / 2 = 45mm
        pcb = PCB.create(width=200, height=120)

        # Board outline is four gr_line segments on Edge.Cuts (issue #3805).
        assert pcb._sexp.find("gr_rect") is None
        assert len(_edge_cuts_lines(pcb)) == 4

        (min_x, min_y), (max_x, max_y) = _outline_bbox(pcb)

        # Check origin is centered
        assert min_x == pytest.approx(48.5)
        assert min_y == pytest.approx(45.0)

        # Check far corner is offset by board dimensions
        assert max_x == pytest.approx(248.5)  # 48.5 + 200
        assert max_y == pytest.approx(165.0)  # 45 + 120

    def test_create_no_centering(self):
        """Test that center=False places board at origin."""
        pcb = PCB.create(width=100, height=80, center=False)

        assert pcb._sexp.find("gr_rect") is None
        assert len(_edge_cuts_lines(pcb)) == 4

        (min_x, min_y), (max_x, max_y) = _outline_bbox(pcb)

        # Check origin is at (0, 0)
        assert min_x == pytest.approx(0.0)
        assert min_y == pytest.approx(0.0)

        # Check far corner is at (width, height)
        assert max_x == pytest.approx(100.0)
        assert max_y == pytest.approx(80.0)

    def test_create_with_a3_paper(self):
        """Test centering on A3 paper."""
        # A3 paper is 420 x 297mm
        # Board is 200 x 150mm
        # Offset X: (420 - 200) / 2 = 110mm
        # Offset Y: (297 - 150) / 2 = 73.5mm
        pcb = PCB.create(width=200, height=150, paper="A3")

        # Check paper size was set correctly
        paper = pcb._sexp.find("paper")
        assert paper is not None
        assert paper.get_string(0) == "A3"

        (min_x, min_y), (max_x, max_y) = _outline_bbox(pcb)

        assert min_x == pytest.approx(110.0)
        assert min_y == pytest.approx(73.5)
        assert max_x == pytest.approx(310.0)  # 110 + 200
        assert max_y == pytest.approx(223.5)  # 73.5 + 150

    def test_create_with_us_letter_paper(self):
        """Test centering on US Letter (A) paper."""
        # US Letter is 279.4 x 215.9mm
        # Board is 100 x 100mm
        # Offset X: (279.4 - 100) / 2 = 89.7mm
        # Offset Y: (215.9 - 100) / 2 = 57.95mm
        pcb = PCB.create(width=100, height=100, paper="A")

        paper = pcb._sexp.find("paper")
        assert paper.get_string(0) == "A"

        (min_x, min_y), _ = _outline_bbox(pcb)

        assert min_x == pytest.approx(89.7)
        assert min_y == pytest.approx(57.95)

    def test_create_invalid_paper_raises_error(self):
        """Test that invalid paper size raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            PCB.create(width=100, height=100, paper="InvalidSize")

        assert "Unknown paper size 'InvalidSize'" in str(exc_info.value)
        assert "A4" in str(exc_info.value)  # Should list supported sizes

    def test_create_large_board_negative_offset(self):
        """Test that boards larger than paper result in negative offsets (still valid)."""
        # A4 paper is 297 x 210mm
        # Board is 400 x 300mm (larger than paper)
        # Offset X: (297 - 400) / 2 = -51.5mm
        # Offset Y: (210 - 300) / 2 = -45mm
        pcb = PCB.create(width=400, height=300, paper="A4")

        (min_x, min_y), _ = _outline_bbox(pcb)

        assert min_x == pytest.approx(-51.5)
        assert min_y == pytest.approx(-45.0)

    def test_create_preserves_all_parameters(self):
        """Test that all create() parameters still work with centering."""
        pcb = PCB.create(
            width=160,
            height=100,
            layers=4,
            title="Test Board",
            revision="2.0",
            company="Test Co",
            paper="A3",
            center=True,
        )

        # Verify title block
        title_block = pcb._sexp.find("title_block")
        assert title_block is not None
        assert title_block.find("title").get_string(0) == "Test Board"
        assert title_block.find("rev").get_string(0) == "2.0"
        assert title_block.find("company").get_string(0) == "Test Co"

        # Verify 4 copper layers
        layers = pcb._sexp.find("layers")
        layer_names = [child.get_string(0) for child in layers.iter_children()]
        assert "F.Cu" in layer_names
        assert "In1.Cu" in layer_names
        assert "In2.Cu" in layer_names
        assert "B.Cu" in layer_names

    def test_board_origin_property_centered(self):
        """Test that board_origin returns the correct offset for centered boards."""
        # A4 paper is 297 x 210mm
        # Board is 200 x 120mm
        # Offset X: (297 - 200) / 2 = 48.5mm
        # Offset Y: (210 - 120) / 2 = 45mm
        pcb = PCB.create(width=200, height=120)

        assert pcb.board_origin[0] == pytest.approx(48.5)
        assert pcb.board_origin[1] == pytest.approx(45.0)

    def test_board_origin_property_not_centered(self):
        """Test that board_origin returns (0, 0) for non-centered boards."""
        pcb = PCB.create(width=200, height=120, center=False)

        assert pcb.board_origin[0] == pytest.approx(0.0)
        assert pcb.board_origin[1] == pytest.approx(0.0)

    @requires_kicad_footprint_libs
    def test_footprint_position_offset_centered_board(self, tmp_path: Path):
        """Test that footprint positions are offset by board origin for centered boards.

        This is a regression test for issue #943: Board centering doesn't offset
        footprint positions. When a board is centered on the drawing sheet,
        footprint positions specified via update_footprint_position() should be
        relative to the board origin, not the sheet origin.
        """
        # A4 paper is 297 x 210mm
        # Board is 200 x 120mm
        # Board origin: (48.5, 45) - centered on A4
        pcb = PCB.create(width=200, height=120)

        # Add a footprint at board-relative position (50, 30)
        pcb.add_footprint(
            library_id="Resistor_SMD:R_0603_1608Metric",
            reference="R1",
            x=50.0,
            y=30.0,
        )

        # Save and check the file
        output_path = tmp_path / "centered_board.kicad_pcb"
        pcb.save(output_path)

        # Read back the file and check the absolute position in the sexp
        reloaded = PCB.load(str(output_path))

        # Find the footprint in the S-expression tree
        for child in reloaded._sexp.iter_children():
            if child.tag != "footprint":
                continue

            # Check if this is R1
            ref_value = None
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    ref_value = prop.get_string(1)
                    break
            if not ref_value:
                for fp_text in child.find_all("fp_text"):
                    if fp_text.get_string(0) == "reference":
                        ref_value = fp_text.get_string(1)
                        break

            if ref_value == "R1":
                # Check the (at x y) node - should be sheet-absolute position
                at_node = child.find("at")
                assert at_node is not None

                # Expected absolute position: board-relative + board origin
                # (50, 30) + (48.5, 45) = (98.5, 75)
                assert at_node.get_float(0) == pytest.approx(98.5)
                assert at_node.get_float(1) == pytest.approx(75.0)
                break
        else:
            pytest.fail("Footprint R1 not found")

    @requires_kicad_footprint_libs
    def test_footprint_position_no_offset_non_centered_board(self, tmp_path: Path):
        """Test that footprint positions match exactly for non-centered boards."""
        pcb = PCB.create(width=200, height=120, center=False)

        # Add a footprint at position (50, 30)
        pcb.add_footprint(
            library_id="Resistor_SMD:R_0603_1608Metric",
            reference="R1",
            x=50.0,
            y=30.0,
        )

        # Save and check the file
        output_path = tmp_path / "non_centered_board.kicad_pcb"
        pcb.save(output_path)

        # Read back the file and check the position
        reloaded = PCB.load(str(output_path))

        # Find R1 footprint
        for child in reloaded._sexp.iter_children():
            if child.tag != "footprint":
                continue

            ref_value = None
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    ref_value = prop.get_string(1)
                    break
            if not ref_value:
                for fp_text in child.find_all("fp_text"):
                    if fp_text.get_string(0) == "reference":
                        ref_value = fp_text.get_string(1)
                        break

            if ref_value == "R1":
                at_node = child.find("at")
                # For non-centered board, position should be exactly as specified
                assert at_node.get_float(0) == pytest.approx(50.0)
                assert at_node.get_float(1) == pytest.approx(30.0)
                break
        else:
            pytest.fail("Footprint R1 not found")

    @requires_kicad_footprint_libs
    def test_update_footprint_position_applies_offset(self, tmp_path: Path):
        """Test that update_footprint_position applies board origin offset."""
        # Create centered board
        pcb = PCB.create(width=200, height=120)

        # Add footprint at initial position
        pcb.add_footprint(
            library_id="Resistor_SMD:R_0603_1608Metric",
            reference="R1",
            x=10.0,
            y=10.0,
        )

        # Update to new board-relative position
        result = pcb.update_footprint_position("R1", x=100.0, y=60.0)
        assert result is True

        # Save and reload
        output_path = tmp_path / "updated_position.kicad_pcb"
        pcb.save(output_path)
        reloaded = PCB.load(str(output_path))

        # Find R1 and check position
        for child in reloaded._sexp.iter_children():
            if child.tag != "footprint":
                continue

            ref_value = None
            for prop in child.find_all("property"):
                if prop.get_string(0) == "Reference":
                    ref_value = prop.get_string(1)
                    break
            if not ref_value:
                for fp_text in child.find_all("fp_text"):
                    if fp_text.get_string(0) == "reference":
                        ref_value = fp_text.get_string(1)
                        break

            if ref_value == "R1":
                at_node = child.find("at")
                # Board origin is (48.5, 45)
                # Board-relative (100, 60) -> sheet-absolute (148.5, 105)
                assert at_node.get_float(0) == pytest.approx(148.5)
                assert at_node.get_float(1) == pytest.approx(105.0)
                break
        else:
            pytest.fail("Footprint R1 not found")

    def test_create_outline_is_four_gr_lines(self):
        """Issue #3805: board outline must be four gr_line Edge.Cuts segments."""
        pcb = PCB.create(width=65, height=56, center=False)

        # No gr_rect outline anymore.
        assert pcb._sexp.find("gr_rect") is None

        segments = _edge_cuts_lines(pcb)
        assert len(segments) == 4

        # Segments form a closed rectangle: each corner appears as both a
        # start and an end exactly once.
        corners = {(0.0, 0.0), (65.0, 0.0), (65.0, 56.0), (0.0, 56.0)}
        starts = {(round(s[0], 6), round(s[1], 6)) for s, _e in segments}
        ends = {(round(e[0], 6), round(e[1], 6)) for _s, e in segments}
        assert starts == corners
        assert ends == corners

    def test_create_board_size_from_gr_line_outline(self):
        """board_size resolves correctly from the gr_line outline (#3805)."""
        pcb = PCB.create(width=65, height=56)
        w, h = pcb.board_size
        assert w == pytest.approx(65.0)
        assert h == pytest.approx(56.0)

    def test_create_board_origin_from_gr_line_outline(self):
        """Board-origin detection works with the gr_line outline (#3805)."""
        # Centered on A4: origin = ((297-65)/2, (210-56)/2) = (116, 77).
        pcb = PCB.create(width=65, height=56)
        ox, oy = pcb.board_origin
        assert ox == pytest.approx(116.0)
        assert oy == pytest.approx(77.0)

    def test_create_stamps_kicad10_format_version(self):
        """Generated board stamps the KiCad-10 format version 20241229 (#3805).

        Also pins the sibling per-stream constants and the shared
        ``generator_version`` so a stale/inconsistent literal can never creep
        back into the writers (#4378).  20241229 is the conservative floor that
        loads across the whole 10.0.x line; 20260206 is 10.0.4-only and is
        rejected by 10.0.3, so the board constant must NOT be bumped past it.
        """
        from kicad_tools.core.version import (
            KICAD_BOARD_FORMAT_VERSION,
            KICAD_GENERATOR_VERSION,
            KICAD_SCH_FORMAT_VERSION,
            KICAD_SYM_FORMAT_VERSION,
        )

        assert KICAD_BOARD_FORMAT_VERSION == 20241229
        assert KICAD_SCH_FORMAT_VERSION == 20231120
        assert KICAD_SYM_FORMAT_VERSION == 20231120
        assert KICAD_GENERATOR_VERSION == "10.0"

        # Regression guard: the board constant must stay at a code that every
        # 10.0.x release accepts. 20260206 is 10.0.4-authored but rejected by
        # 10.0.3 (a *future* format); bumping to it would regress those users.
        assert KICAD_BOARD_FORMAT_VERSION < 20260206

        pcb = PCB.create(width=65, height=56)
        version = pcb._sexp.find("version")
        assert version is not None
        assert version.get_int(0) == 20241229

    def test_create_4layer_outline_is_gr_line(self):
        """4-layer boards also emit a gr_line outline (#3805)."""
        pcb = PCB.create(width=65, height=56, layers=4, center=False)
        assert pcb._sexp.find("gr_rect") is None
        assert len(_edge_cuts_lines(pcb)) == 4
        assert pcb.board_size == pytest.approx((65.0, 56.0))


class TestBoardOriginCoordinateConversion:
    """Tests for board origin coordinate conversion.

    Issue #1088: Placement optimizer translates components outside board outline.
    When a PCB is loaded with a non-zero board origin (centered boards), footprint
    positions should be converted from sheet-absolute to board-relative coordinates.
    This ensures that the optimizer works with board-relative positions, and
    update_footprint_position() correctly adds the origin back when saving.
    """

    @requires_kicad_footprint_libs
    def test_loaded_footprint_positions_are_board_relative(self, tmp_path: Path):
        """Test that footprint positions are converted to board-relative on load.

        This is a regression test for issue #1088: When a centered board is loaded,
        footprint positions should be board-relative, not sheet-absolute.
        """
        # Create a centered board
        pcb = PCB.create(width=65, height=56)  # Similar to issue example

        # Board origin will be at approximately ((297-65)/2, (210-56)/2) = (116, 77)
        origin_x, origin_y = pcb.board_origin

        # Add a footprint at board-relative position (12, 43)
        pcb.add_footprint(
            library_id="Resistor_SMD:R_0603_1608Metric",
            reference="R1",
            x=12.0,
            y=43.0,
        )

        # Verify in-memory position is board-relative
        fp = pcb.get_footprint("R1")
        assert fp.position[0] == pytest.approx(12.0)
        assert fp.position[1] == pytest.approx(43.0)

        # Save and reload
        output_path = tmp_path / "centered_board.kicad_pcb"
        pcb.save(output_path)
        reloaded = PCB.load(str(output_path))

        # After loading, position should still be board-relative
        fp_reloaded = reloaded.get_footprint("R1")
        assert fp_reloaded.position[0] == pytest.approx(12.0)
        assert fp_reloaded.position[1] == pytest.approx(43.0)

    @requires_kicad_footprint_libs
    def test_update_position_roundtrip_preserves_coordinates(self, tmp_path: Path):
        """Test that updating position and reloading preserves coordinates.

        This is the core regression test for issue #1088: Without the fix,
        each save/reload cycle would ADD the board origin to positions,
        causing positions to grow unboundedly.
        """
        # Create a centered board
        pcb = PCB.create(width=65, height=56)

        # Add a footprint
        pcb.add_footprint(
            library_id="Resistor_SMD:R_0603_1608Metric",
            reference="R1",
            x=12.0,
            y=43.0,
        )

        # Save, reload, update position, save, reload
        output_path = tmp_path / "roundtrip.kicad_pcb"
        pcb.save(output_path)

        # First reload
        pcb2 = PCB.load(str(output_path))
        fp = pcb2.get_footprint("R1")
        assert fp.position[0] == pytest.approx(12.0)
        assert fp.position[1] == pytest.approx(43.0)

        # Update to new position
        pcb2.update_footprint_position("R1", x=30.0, y=20.0)

        # Save and reload again
        output_path2 = tmp_path / "roundtrip2.kicad_pcb"
        pcb2.save(output_path2)
        pcb3 = PCB.load(str(output_path2))

        # Position should be exactly (30, 20), not doubled
        fp3 = pcb3.get_footprint("R1")
        assert fp3.position[0] == pytest.approx(30.0)
        assert fp3.position[1] == pytest.approx(20.0)

        # Multiple roundtrips should not accumulate offset
        pcb3.update_footprint_position("R1", x=40.0, y=25.0)
        output_path3 = tmp_path / "roundtrip3.kicad_pcb"
        pcb3.save(output_path3)
        pcb4 = PCB.load(str(output_path3))

        fp4 = pcb4.get_footprint("R1")
        assert fp4.position[0] == pytest.approx(40.0)
        assert fp4.position[1] == pytest.approx(25.0)

    @requires_kicad_footprint_libs
    def test_optimizer_workflow_preserves_board_bounds(self, tmp_path: Path):
        """Test that optimizer workflow keeps components within board bounds.

        This simulates the workflow from issue #1088 where optimization
        caused components to be placed outside the board outline.
        """
        from kicad_tools.optim import PlacementConfig, PlacementOptimizer

        # Create a centered board similar to the issue
        pcb = PCB.create(width=65, height=56)
        origin_x, origin_y = pcb.board_origin

        # Add components at board-relative positions
        pcb.add_footprint(
            library_id="Resistor_SMD:R_0603_1608Metric",
            reference="R1",
            x=12.0,
            y=43.0,
        )
        pcb.add_footprint(
            library_id="Capacitor_SMD:C_0603_1608Metric",
            reference="C1",
            x=45.0,
            y=20.0,
        )

        # Save and reload to simulate real workflow
        pcb_path = tmp_path / "optimizer_test.kicad_pcb"
        pcb.save(pcb_path)
        loaded_pcb = PCB.load(str(pcb_path))

        # Create optimizer from loaded PCB
        config = PlacementConfig()
        optimizer = PlacementOptimizer.from_pcb(loaded_pcb, config=config)

        # Get components - they should be at board-relative positions
        r1 = optimizer.get_component("R1")
        c1 = optimizer.get_component("C1")

        assert r1 is not None
        assert c1 is not None

        # Positions should be board-relative, within board bounds
        assert 0 <= r1.x <= 65, f"R1.x={r1.x} outside board bounds [0, 65]"
        assert 0 <= r1.y <= 56, f"R1.y={r1.y} outside board bounds [0, 56]"
        assert 0 <= c1.x <= 65, f"C1.x={c1.x} outside board bounds [0, 65]"
        assert 0 <= c1.y <= 56, f"C1.y={c1.y} outside board bounds [0, 56]"

        # Write back and reload
        optimizer.write_to_pcb(loaded_pcb)
        loaded_pcb.save(pcb_path)

        final_pcb = PCB.load(str(pcb_path))

        # Positions should still be within board bounds
        r1_final = final_pcb.get_footprint("R1")
        c1_final = final_pcb.get_footprint("C1")

        assert 0 <= r1_final.position[0] <= 65, f"R1.x={r1_final.position[0]} outside board bounds"
        assert 0 <= r1_final.position[1] <= 56, f"R1.y={r1_final.position[1]} outside board bounds"
        assert 0 <= c1_final.position[0] <= 65, f"C1.x={c1_final.position[0]} outside board bounds"
        assert 0 <= c1_final.position[1] <= 56, f"C1.y={c1_final.position[1]} outside board bounds"


class TestPCBPageFit:
    """Tests for PCB.page_fit() — tight User page + centered board."""

    def test_page_fit_rewrites_paper_to_user(self):
        """page_fit replaces (paper "A4") with (paper "User" W H)."""
        # 200 x 120 board on default A4, centered at (48.5, 45).
        pcb = PCB.create(width=200, height=120)
        assert pcb._sexp.find("paper").get_string(0) == "A4"

        new_w, new_h = pcb.page_fit(margin=5.0)

        paper = pcb._sexp.find("paper")
        assert paper is not None
        assert paper.get_string(0) == "User"
        # W = 200 + 2*5, H = 120 + 2*5
        assert paper.get_float(1) == pytest.approx(210.0)
        assert paper.get_float(2) == pytest.approx(130.0)
        assert new_w == pytest.approx(210.0)
        assert new_h == pytest.approx(130.0)

    def test_page_fit_centers_board_with_uniform_margin(self):
        """After page_fit the board outline sits at (margin, margin)."""
        pcb = PCB.create(width=200, height=120)
        margin = 7.5
        new_w, new_h = pcb.page_fit(margin=margin)

        (min_x, min_y), (max_x, max_y) = _outline_bbox(pcb)

        # Board outline min corner moved to (margin, margin).
        assert min_x == pytest.approx(margin)
        assert min_y == pytest.approx(margin)
        # Max corner = margin + board size.
        assert max_x == pytest.approx(margin + 200)
        assert max_y == pytest.approx(margin + 120)

        # Board center == page center.
        board_cx = (min_x + max_x) / 2
        board_cy = (min_y + max_y) / 2
        assert board_cx == pytest.approx(new_w / 2)
        assert board_cy == pytest.approx(new_h / 2)

    def test_page_fit_translates_footprint_position(self, tmp_path: Path):
        """A footprint's absolute position shifts by (margin - bbox_min)."""
        # Non-centered board: outline at (0,0)..(50,40); a footprint at
        # absolute (10, 20). page_fit(margin=5) shifts everything by +5.
        pcb_text = """(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(footprint "Test:FP"
\t\t(layer "F.Cu")
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(at 10 20)
\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
\t)
\t(gr_rect
\t\t(start 0 0)
\t\t(end 50 40)
\t\t(layer "Edge.Cuts")
\t\t(width 0.15)
\t)
\t(segment (start 5 5) (end 45 35) (width 0.25) (layer "F.Cu") (net 0))
)
"""
        pcb_file = tmp_path / "fp.kicad_pcb"
        pcb_file.write_text(pcb_text)

        pcb = PCB.load(pcb_file)
        pcb.page_fit(margin=5.0)

        # Footprint absolute (at ...) is bbox_min(0,0) -> (5,5), so +5 each.
        fp_node = pcb._sexp.find("footprint")
        at_node = fp_node.find_child("at")
        assert at_node.get_float(0) == pytest.approx(15.0)
        assert at_node.get_float(1) == pytest.approx(25.0)

        # Pad inner (at 0 0) must NOT be translated (footprint-relative).
        pad_at = fp_node.find_child("pad").find_child("at")
        assert pad_at.get_float(0) == pytest.approx(0.0)
        assert pad_at.get_float(1) == pytest.approx(0.0)

        # Segment endpoints shift by +5 too.
        seg = pcb._sexp.find("segment")
        assert seg.find("start").get_float(0) == pytest.approx(10.0)
        assert seg.find("start").get_float(1) == pytest.approx(10.0)
        assert seg.find("end").get_float(0) == pytest.approx(50.0)
        assert seg.find("end").get_float(1) == pytest.approx(40.0)

    def test_page_fit_preserves_relative_geometry(self):
        """Relative spacing between items is unchanged (DRC-preserving)."""
        pcb = PCB.create(width=80, height=60)
        (bmin, bmax) = _outline_bbox(pcb)
        before = (bmax[0] - bmin[0], bmax[1] - bmin[1])
        pcb.page_fit(margin=10.0)
        (amin, amax) = _outline_bbox(pcb)
        after = (amax[0] - amin[0], amax[1] - amin[1])
        assert after[0] == pytest.approx(before[0])
        assert after[1] == pytest.approx(before[1])

    def test_page_fit_preserves_pairwise_distances_exactly(self):
        """page_fit is exactly distance-preserving on the nm grid.

        Regression test for issue #3714: re-rounding each summed coordinate
        independently to 6 decimals introduced sub-nm drift that changed
        pairwise distances and tipped near-miss clearances over the DRC rule.
        The delta must be grid-snapped and applied in integer-nm space so that
        every translated coordinate stays exactly on the grid and all pairwise
        distances are bit-for-bit identical.
        """

        def _all_coords(pcb: PCB) -> list[tuple[float, float]]:
            coords: list[tuple[float, float]] = []

            def _walk(node):
                if node.tag in {"at", "start", "end", "mid", "center", "xy"}:
                    x, y = node.get_float(0), node.get_float(1)
                    if x is not None and y is not None:
                        coords.append((x, y))
                for child in node.iter_children():
                    _walk(child)

            for child in pcb._sexp.iter_children():
                _walk(child)
            return coords

        # Use a non-integer outline so the delta is a fractional mm value.
        pcb = PCB.create(width=80.077, height=60.112)
        before = _all_coords(pcb)
        pcb.page_fit(margin=5.0)
        after = _all_coords(pcb)

        assert len(before) == len(after)

        # Every coordinate must land exactly on the nm grid (no float dust
        # beyond 6 decimals).
        for x, y in after:
            assert round(x * 1_000_000) == pytest.approx(x * 1_000_000, abs=1e-3)
            assert round(y * 1_000_000) == pytest.approx(y * 1_000_000, abs=1e-3)

        # Pairwise nm-grid distances must be IDENTICAL (integer-exact) before
        # and after the translation -- this is what guarantees DRC parity.
        def _pairwise_nm(coords):
            nm = [(round(x * 1_000_000), round(y * 1_000_000)) for x, y in coords]
            out = []
            for i in range(len(nm)):
                for j in range(i + 1, len(nm)):
                    dx = nm[i][0] - nm[j][0]
                    dy = nm[i][1] - nm[j][1]
                    out.append(dx * dx + dy * dy)
            return out

        assert _pairwise_nm(before) == _pairwise_nm(after)

    def test_page_fit_preserves_45_degree_angle_through_save(self, tmp_path: Path):
        """page_fit is angle-preserving: a 45-deg segment stays exactly 45 deg.

        Regression test for issue #3714 (second defect): a rigid translation
        must shift every endpoint by the IDENTICAL delta, so all relative
        geometry -- including segment angles -- is preserved exactly.  The
        bug shifted endpoints by slightly different amounts (re-snapping each
        base coord, then losing a significant digit in the ``%.6g`` float
        serializer, e.g. ``147.9252`` -> ``147.925``), tilting otherwise-exact
        45-deg copper off-angle.  Off-45 copper is non-manufacturable.

        The serializer truncation only surfaces after a save/reload, so this
        test goes through disk -- exactly what the fleet 45-census measures.
        """
        import math

        # A perfect 45-deg segment whose endpoint carries 7 significant
        # digits (243.0748 + the -95mm page_fit delta -> 148.0748): the
        # culprit precision the %.6g serializer used to drop.  dx == dy in
        # magnitude => exactly 45 degrees.
        pcb_text = """(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(gr_rect
\t\t(start 100 100)
\t\t(end 250 200)
\t\t(layer "Edge.Cuts")
\t\t(width 0.15)
\t)
\t(segment (start 243 112) (end 242.9252 112.0748) (width 0.25) (layer "F.Cu") (net 0))
)
"""
        src = tmp_path / "ang.kicad_pcb"
        src.write_text(pcb_text)

        def _seg_angle_deg(path: Path) -> float:
            reloaded = PCB.load(path)
            seg = reloaded._sexp.find("segment")
            sx = seg.find("start").get_float(0)
            sy = seg.find("start").get_float(1)
            ex = seg.find("end").get_float(0)
            ey = seg.find("end").get_float(1)
            return math.degrees(math.atan2(ey - sy, ex - sx))

        before = _seg_angle_deg(src)
        assert abs(before) == pytest.approx(135.0, abs=1e-9)  # exactly 45 off-axis

        pcb = PCB.load(src)
        pcb.page_fit(margin=5.0)
        out = tmp_path / "ang_fit.kicad_pcb"
        pcb.save(out)

        after = _seg_angle_deg(out)
        # The angle must be IDENTICAL through the page_fit + save roundtrip.
        assert after == pytest.approx(before, abs=1e-9), (
            f"page_fit tilted a 45-deg segment: {before} -> {after} deg"
        )
        # And it must still be on the legal {0,45,90,135} set (exact).
        assert abs(after) % 45.0 == pytest.approx(0.0, abs=1e-6)

    def test_page_fit_roundtrip_idempotent_page_size(self, tmp_path: Path):
        """Running page_fit twice yields the same page size (idempotent)."""
        pcb = PCB.create(width=120, height=90)
        w1, h1 = pcb.page_fit(margin=5.0)
        pcb_file = tmp_path / "rt.kicad_pcb"
        pcb.save(pcb_file)

        reloaded = PCB.load(pcb_file)
        w2, h2 = reloaded.page_fit(margin=5.0)
        assert w2 == pytest.approx(w1)
        assert h2 == pytest.approx(h1)

    def test_page_fit_no_edge_cuts_raises(self, tmp_path: Path):
        """page_fit raises ValueError when there is no Edge.Cuts outline."""
        pcb_text = """(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t)
)
"""
        pcb_file = tmp_path / "noedge.kicad_pcb"
        pcb_file.write_text(pcb_text)
        pcb = PCB.load(pcb_file)
        with pytest.raises(ValueError):
            pcb.page_fit(margin=5.0)


class TestPropertySetterProtection:
    """Tests for property setters that prevent silent data loss.

    Issue #1047: PCB.segments and PCB.vias property changes don't persist to save().
    These tests verify that attempting to assign to these properties raises
    AttributeError with helpful guidance.
    """

    def test_segments_setter_raises_error(self, minimal_pcb: Path):
        """Test that assigning to segments raises AttributeError."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        with pytest.raises(AttributeError) as exc_info:
            pcb.segments = []

        error_msg = str(exc_info.value)
        assert "Cannot modify segments directly" in error_msg
        assert "persist to save()" in error_msg
        assert "add_trace()" in error_msg

    def test_vias_setter_raises_error(self, minimal_pcb: Path):
        """Test that assigning to vias raises AttributeError."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        with pytest.raises(AttributeError) as exc_info:
            pcb.vias = []

        error_msg = str(exc_info.value)
        assert "Cannot modify vias directly" in error_msg
        assert "persist to save()" in error_msg
        assert "add_via()" in error_msg

    def test_segments_getter_still_works(self, minimal_pcb: Path):
        """Test that reading segments still works after adding setters."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)

        # Should be able to read segments
        segments = pcb.segments
        assert isinstance(segments, list)
        # The minimal PCB fixture has 1 segment
        assert len(segments) == 1

    def test_vias_getter_still_works(self):
        """Test that reading vias still works after adding setters."""
        pcb = PCB.create(width=100, height=100)

        # Add a via using the proper method
        pcb.add_via(x=50.0, y=50.0, net="TestNet")

        # Should be able to read vias
        vias = pcb.vias
        assert isinstance(vias, list)
        assert len(vias) == 1

    def test_add_trace_still_works(self):
        """Test that add_trace() still works correctly after setter changes."""
        pcb = PCB.create(width=100, height=100)

        initial_count = len(pcb.segments)

        segments = pcb.add_trace(
            start=(10.0, 10.0),
            end=(50.0, 10.0),
            width=0.25,
            layer="F.Cu",
            net="TestNet",
        )

        # Verify add_trace works
        assert len(segments) == 1
        assert len(pcb.segments) == initial_count + 1

    def test_add_via_still_works(self):
        """Test that add_via() still works correctly after setter changes."""
        pcb = PCB.create(width=100, height=100)

        initial_count = len(pcb.vias)

        via = pcb.add_via(x=50.0, y=50.0, size=0.6, drill=0.3, net="TestNet")

        # Verify add_via works
        assert via is not None
        assert len(pcb.vias) == initial_count + 1


class TestStripTraces:
    """Tests for PCB.strip_traces() method."""

    def test_strip_all_traces(self, minimal_pcb: Path):
        """Test stripping all traces and vias from a PCB."""
        pcb = PCB.load(minimal_pcb)

        # Verify there are traces before stripping
        assert len(pcb.segments) >= 1

        # Strip all traces
        stats = pcb.strip_traces()

        # Verify all traces are removed
        assert len(pcb.segments) == 0
        assert len(pcb.vias) == 0
        assert stats["segments"] >= 1

    def test_strip_traces_preserves_footprints(self, minimal_pcb: Path):
        """Test that stripping traces preserves footprints."""
        pcb = PCB.load(minimal_pcb)

        initial_footprint_count = len(pcb.footprints)
        assert initial_footprint_count > 0

        pcb.strip_traces()

        # Footprints should be preserved
        assert len(pcb.footprints) == initial_footprint_count

    def test_strip_traces_preserves_zones_by_default(self, zone_test_pcb: Path):
        """Test that zones are preserved by default when stripping traces."""
        pcb = PCB.load(zone_test_pcb)

        initial_zone_count = len(pcb.zones)

        stats = pcb.strip_traces(keep_zones=True)

        # Zones should be preserved
        assert len(pcb.zones) == initial_zone_count
        assert stats["zones"] == 0

    def test_strip_traces_can_remove_zones(self, zone_test_pcb: Path):
        """Test that zones can be removed with keep_zones=False."""
        pcb = PCB.load(zone_test_pcb)

        initial_zone_count = len(pcb.zones)
        assert initial_zone_count > 0

        stats = pcb.strip_traces(keep_zones=False)

        # Zones should be removed
        assert len(pcb.zones) == 0
        assert stats["zones"] == initial_zone_count

    def test_strip_specific_nets(self, tmp_path: Path):
        """Test stripping only specific nets."""
        pcb = PCB.create(width=100, height=100)

        # Add traces on different nets
        pcb.add_trace(
            start=(10.0, 10.0),
            end=(50.0, 10.0),
            width=0.25,
            layer="F.Cu",
            net="GND",
        )
        pcb.add_trace(
            start=(10.0, 20.0),
            end=(50.0, 20.0),
            width=0.25,
            layer="F.Cu",
            net="VCC",
        )

        initial_segments = len(pcb.segments)
        assert initial_segments == 2

        # Strip only GND net
        stats = pcb.strip_traces(nets=["GND"])

        # Only GND trace should be removed
        assert stats["segments"] == 1
        assert len(pcb.segments) == 1

        # The remaining trace should be VCC
        remaining_net_nums = {seg.net_number for seg in pcb.segments}
        gnd_net_num = None
        for net_num, net in pcb.nets.items():
            if net.name == "GND":
                gnd_net_num = net_num
                break
        assert gnd_net_num not in remaining_net_nums

    def test_strip_traces_updates_sexp(self, minimal_pcb: Path, tmp_path: Path):
        """Test that stripping traces updates the underlying S-expression."""
        pcb = PCB.load(minimal_pcb)

        # Strip traces
        pcb.strip_traces()

        # Save to a new file
        output_path = tmp_path / "stripped.kicad_pcb"
        pcb.save(output_path)

        # Reload and verify traces are gone
        reloaded = PCB.load(output_path)
        assert len(reloaded.segments) == 0
        assert len(reloaded.vias) == 0

    def test_strip_traces_returns_stats(self, minimal_pcb: Path):
        """Test that strip_traces returns correct statistics."""
        pcb = PCB.load(minimal_pcb)

        initial_segments = len(pcb.segments)
        initial_vias = len(pcb.vias)

        stats = pcb.strip_traces()

        # Stats should match what was removed
        assert stats["segments"] == initial_segments
        assert stats["vias"] == initial_vias
        assert "zones" in stats

    def test_strip_traces_on_empty_pcb(self):
        """Test stripping traces on a PCB with no traces."""
        pcb = PCB.create(width=100, height=100)

        # No traces to begin with
        assert len(pcb.segments) == 0

        stats = pcb.strip_traces()

        # Should complete without error
        assert stats["segments"] == 0
        assert stats["vias"] == 0
        assert len(pcb.segments) == 0

    # ------------------------------------------------------------------
    # Layer filtering tests
    # ------------------------------------------------------------------

    def test_strip_layers_only_removes_matching_layer(self):
        """Strip with layers=['In1.Cu'] removes only In1.Cu segments."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="F.Cu", net="SIG_A")
        pcb.add_trace(start=(10, 20), end=(50, 20), width=0.25, layer="In1.Cu", net="SIG_B")
        pcb.add_trace(start=(10, 30), end=(50, 30), width=0.25, layer="In2.Cu", net="SIG_C")
        pcb.add_trace(start=(10, 40), end=(50, 40), width=0.25, layer="B.Cu", net="SIG_D")

        assert len(pcb.segments) == 4

        stats = pcb.strip_traces(layers=["In1.Cu"])

        assert stats["segments"] == 1
        assert len(pcb.segments) == 3
        remaining_layers = {seg.layer for seg in pcb.segments}
        assert "In1.Cu" not in remaining_layers
        assert "F.Cu" in remaining_layers
        assert "B.Cu" in remaining_layers

    def test_strip_multiple_layers(self):
        """Strip with layers=['In1.Cu', 'In2.Cu'] removes both."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="F.Cu", net="SIG_A")
        pcb.add_trace(start=(10, 20), end=(50, 20), width=0.25, layer="In1.Cu", net="SIG_B")
        pcb.add_trace(start=(10, 30), end=(50, 30), width=0.25, layer="In2.Cu", net="SIG_C")
        pcb.add_trace(start=(10, 40), end=(50, 40), width=0.25, layer="B.Cu", net="SIG_D")

        stats = pcb.strip_traces(layers=["In1.Cu", "In2.Cu"])

        assert stats["segments"] == 2
        assert len(pcb.segments) == 2
        remaining_layers = {seg.layer for seg in pcb.segments}
        assert remaining_layers == {"F.Cu", "B.Cu"}

    def test_strip_layers_and_nets_combined(self):
        """Combining layers and nets ANDs the two filters."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="In1.Cu", net="AUDIO_R")
        pcb.add_trace(start=(10, 20), end=(50, 20), width=0.25, layer="In1.Cu", net="AUDIO_L")
        pcb.add_trace(start=(10, 30), end=(50, 30), width=0.25, layer="F.Cu", net="AUDIO_R")

        # Look up net numbers for verification
        audio_r_num = None
        audio_l_num = None
        for num, net in pcb.nets.items():
            if net.name == "AUDIO_R":
                audio_r_num = num
            elif net.name == "AUDIO_L":
                audio_l_num = num

        stats = pcb.strip_traces(layers=["In1.Cu"], nets=["AUDIO_R"])

        # Only the AUDIO_R segment on In1.Cu should be removed
        assert stats["segments"] == 1
        assert len(pcb.segments) == 2
        # Remaining: AUDIO_L on In1.Cu and AUDIO_R on F.Cu
        remaining = {(seg.layer, seg.net_number) for seg in pcb.segments}
        assert ("In1.Cu", audio_l_num) in remaining
        assert ("F.Cu", audio_r_num) in remaining

    # ------------------------------------------------------------------
    # Power net exclusion tests
    # ------------------------------------------------------------------

    def test_strip_exclude_power_default_off(self):
        """By default (API), power nets ARE stripped."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="In1.Cu", net="GND")

        stats = pcb.strip_traces(layers=["In1.Cu"])

        assert stats["segments"] == 1
        assert len(pcb.segments) == 0

    def test_strip_exclude_power_preserves_power_nets(self):
        """With exclude_power=True, power nets are preserved."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="In1.Cu", net="GND")
        pcb.add_trace(start=(10, 20), end=(50, 20), width=0.25, layer="In1.Cu", net="SIG_A")

        gnd_num = None
        for num, net in pcb.nets.items():
            if net.name == "GND":
                gnd_num = num
                break

        stats = pcb.strip_traces(layers=["In1.Cu"], exclude_power=True)

        assert stats["segments"] == 1  # only SIG_A removed
        assert len(pcb.segments) == 1
        assert pcb.segments[0].net_number == gnd_num

    def test_strip_exclude_power_various_names(self):
        """Power heuristic catches common power net names."""
        pcb = PCB.create(width=100, height=100)
        power_names = ["GND", "+3V3", "+5V", "VCC", "VDD", "VBUS"]
        for i, name in enumerate(power_names):
            pcb.add_trace(
                start=(10, 10 + i * 10),
                end=(50, 10 + i * 10),
                width=0.25,
                layer="In1.Cu",
                net=name,
            )
        pcb.add_trace(start=(10, 80), end=(50, 80), width=0.25, layer="In1.Cu", net="AUDIO")

        stats = pcb.strip_traces(layers=["In1.Cu"], exclude_power=True)

        # Only AUDIO should be removed
        assert stats["segments"] == 1
        assert len(pcb.segments) == len(power_names)

    def test_strip_custom_power_pattern(self):
        """Custom power_pattern overrides built-in heuristic."""
        import re

        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="In1.Cu", net="PWR_RAIL")
        pcb.add_trace(start=(10, 20), end=(50, 20), width=0.25, layer="In1.Cu", net="SIG_A")

        pwr_num = None
        for num, net in pcb.nets.items():
            if net.name == "PWR_RAIL":
                pwr_num = num
                break

        # Custom pattern that matches PWR_*
        pattern = re.compile(r"^PWR_", re.IGNORECASE)
        stats = pcb.strip_traces(layers=["In1.Cu"], exclude_power=True, power_pattern=pattern)

        assert stats["segments"] == 1
        assert len(pcb.segments) == 1
        assert pcb.segments[0].net_number == pwr_num

    # ------------------------------------------------------------------
    # Via behavior with layer filtering
    # ------------------------------------------------------------------

    def test_strip_via_kept_when_not_all_layers_match(self):
        """Via connecting F.Cu-B.Cu is kept when stripping In1.Cu."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="In1.Cu", net="SIG_A")
        pcb.add_via(x=50, y=10, layers=("F.Cu", "B.Cu"), net="SIG_A")

        stats = pcb.strip_traces(layers=["In1.Cu"])

        assert stats["segments"] == 1
        assert stats["vias"] == 0  # via NOT removed — it connects F.Cu-B.Cu
        assert len(pcb.vias) == 1

    def test_strip_via_removed_when_all_layers_match(self):
        """Via whose layers are all in the strip set gets removed."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="In1.Cu", net="SIG_A")
        pcb.add_via(x=50, y=10, layers=("In1.Cu", "In2.Cu"), net="SIG_A")

        stats = pcb.strip_traces(layers=["In1.Cu", "In2.Cu"])

        assert stats["segments"] == 1
        assert stats["vias"] == 1
        assert len(pcb.vias) == 0

    # ------------------------------------------------------------------
    # Orphan via removal
    # ------------------------------------------------------------------

    def test_strip_orphan_via_removal(self):
        """After stripping layer segments, orphan vias are removed."""
        pcb = PCB.create(width=100, height=100)
        # Segment on In1.Cu ending at (50, 10)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="In1.Cu", net="SIG_A")
        # Via at (50, 10) connecting F.Cu-B.Cu — after In1.Cu strip, no segment
        # touches this via on F.Cu or B.Cu
        pcb.add_via(x=50, y=10, layers=("F.Cu", "B.Cu"), net="SIG_A")

        stats = pcb.strip_traces(layers=["In1.Cu"], remove_orphan_vias=True)

        assert stats["segments"] == 1
        assert stats["vias"] == 1  # orphan via removed
        assert len(pcb.vias) == 0

    def test_strip_orphan_via_kept_when_connected(self):
        """Via with remaining segments on its layers is NOT orphaned."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="In1.Cu", net="SIG_A")
        pcb.add_trace(start=(50, 10), end=(90, 10), width=0.25, layer="F.Cu", net="SIG_A")
        pcb.add_via(x=50, y=10, layers=("F.Cu", "B.Cu"), net="SIG_A")

        stats = pcb.strip_traces(layers=["In1.Cu"], remove_orphan_vias=True)

        assert stats["segments"] == 1  # In1.Cu segment removed
        assert stats["vias"] == 0  # via still connected to F.Cu segment at (50,10)
        assert len(pcb.vias) == 1

    # ------------------------------------------------------------------
    # Zone interaction with layer filter
    # ------------------------------------------------------------------

    def test_strip_zones_respect_layer_filter(self):
        """Zones are only removed if their layer matches the filter."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="In1.Cu", net="SIG_A")
        # We can't easily add zones via the API, so test via strip_traces
        # with keep_zones=True (default) — zones should never be removed
        stats = pcb.strip_traces(layers=["In1.Cu"], keep_zones=True)
        assert stats["zones"] == 0

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_strip_empty_layers_list_is_noop(self):
        """Empty layer list removes nothing."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="F.Cu", net="SIG_A")

        stats = pcb.strip_traces(layers=[])

        assert stats["segments"] == 0
        assert len(pcb.segments) == 1

    def test_strip_nonexistent_layer_removes_nothing(self):
        """Non-existent layer name removes nothing."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="F.Cu", net="SIG_A")

        stats = pcb.strip_traces(layers=["Nonexistent.Cu"])

        assert stats["segments"] == 0
        assert len(pcb.segments) == 1

    def test_strip_roundtrip_with_layers(self, tmp_path):
        """Stripping with layers saves/loads correctly."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10, 10), end=(50, 10), width=0.25, layer="F.Cu", net="SIG_A")
        pcb.add_trace(start=(10, 20), end=(50, 20), width=0.25, layer="In1.Cu", net="SIG_B")

        pcb.strip_traces(layers=["In1.Cu"])

        out = tmp_path / "stripped.kicad_pcb"
        pcb.save(out)
        reloaded = PCB.load(out)
        assert len(reloaded.segments) == 1
        assert reloaded.segments[0].layer == "F.Cu"

    # ------------------------------------------------------------------
    # Region-scoped stripping (Issue #4136 Phase 1)
    # ------------------------------------------------------------------

    def test_strip_region_segment_fully_inside_removed(self):
        """A segment fully inside the region box is removed."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(20, 20), end=(30, 20), width=0.25, layer="F.Cu", net="SIG_A")

        stats = pcb.strip_traces(region=(10, 10, 40, 40))

        assert stats["segments"] == 1
        assert stats["segments_clipped"] == 0
        assert len(pcb.segments) == 0

    def test_strip_region_segment_fully_outside_kept(self):
        """A segment entirely outside the region box is untouched."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(60, 60), end=(80, 60), width=0.25, layer="F.Cu", net="SIG_A")

        stats = pcb.strip_traces(region=(10, 10, 40, 40))

        assert stats["segments"] == 0
        assert stats["segments_clipped"] == 0
        assert len(pcb.segments) == 1

    def test_strip_region_crossing_segment_is_clipped(self):
        """A segment with one endpoint inside is clipped to the outside piece."""
        pcb = PCB.create(width=100, height=100)
        # Horizontal segment from (20,20) inside the box to (60,20) outside;
        # region right edge is x=40, so the kept piece runs (40,20)->(60,20).
        pcb.add_trace(start=(20, 20), end=(60, 20), width=0.25, layer="F.Cu", net="SIG_A")

        stats = pcb.strip_traces(region=(10, 10, 40, 40))

        assert stats["segments"] == 0  # not a whole-segment removal
        assert stats["segments_clipped"] == 1
        assert len(pcb.segments) == 1
        seg = pcb.segments[0]
        # The inside endpoint (20,20) moved to the boundary x=40; the outside
        # endpoint (60,20) is unchanged.
        xs = {round(seg.start[0], 3), round(seg.end[0], 3)}
        assert xs == {40.0, 60.0}
        ys = {round(seg.start[1], 3), round(seg.end[1], 3)}
        assert ys == {20.0}

    def test_strip_region_clip_roundtrips_through_save(self, tmp_path: Path):
        """Clipped segment endpoints persist through save/reload."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(20, 20), end=(60, 20), width=0.25, layer="F.Cu", net="SIG_A")

        pcb.strip_traces(region=(10, 10, 40, 40))

        out = tmp_path / "clipped.kicad_pcb"
        pcb.save(out)
        reloaded = PCB.load(out)
        assert len(reloaded.segments) == 1
        seg = reloaded.segments[0]
        xs = {round(seg.start[0], 3), round(seg.end[0], 3)}
        assert xs == {40.0, 60.0}

    def test_strip_region_both_endpoints_outside_spanning_box_skipped(self):
        """A segment spanning the box with both ends outside is left untouched."""
        pcb = PCB.create(width=100, height=100)
        # Runs (5,25) -> (55,25): both endpoints outside the x-range [10,40]
        # but the span crosses the box horizontally.
        pcb.add_trace(start=(5, 25), end=(55, 25), width=0.25, layer="F.Cu", net="SIG_A")

        stats = pcb.strip_traces(region=(10, 10, 40, 40))

        assert stats["segments"] == 0
        assert stats["segments_clipped"] == 0
        assert stats["segments_boundary_skipped"] == 1
        assert len(pcb.segments) == 1  # unchanged

    def test_strip_region_via_inside_removed(self):
        """A via whose point is inside the region is removed."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_via(x=25, y=25, layers=("F.Cu", "B.Cu"), net="SIG_A")

        stats = pcb.strip_traces(region=(10, 10, 40, 40))

        assert stats["vias"] == 1
        assert len(pcb.vias) == 0

    def test_strip_region_via_outside_kept(self):
        """A via whose point is outside the region is kept."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_via(x=70, y=70, layers=("F.Cu", "B.Cu"), net="SIG_A")

        stats = pcb.strip_traces(region=(10, 10, 40, 40))

        assert stats["vias"] == 0
        assert len(pcb.vias) == 1

    def test_strip_region_and_nets_combined(self):
        """--region ANDed with --nets: only in-region segments of named net go."""
        pcb = PCB.create(width=100, height=100)
        # Inside region, net A — should be removed.
        pcb.add_trace(start=(20, 20), end=(30, 20), width=0.25, layer="F.Cu", net="NET_A")
        # Inside region, net B — kept (net filter excludes it).
        pcb.add_trace(start=(20, 30), end=(30, 30), width=0.25, layer="F.Cu", net="NET_B")
        # Outside region, net A — kept (spatial filter excludes it).
        pcb.add_trace(start=(60, 60), end=(70, 60), width=0.25, layer="F.Cu", net="NET_A")

        stats = pcb.strip_traces(region=(10, 10, 40, 40), nets=["NET_A"])

        assert stats["segments"] == 1
        assert len(pcb.segments) == 2
        remaining_nets = set()
        for seg in pcb.segments:
            for num, net in pcb.nets.items():
                if num == seg.net_number:
                    remaining_nets.add(net.name)
        assert remaining_nets == {"NET_A", "NET_B"}

    def test_strip_region_and_layers_combined(self):
        """--region ANDed with --layers."""
        pcb = PCB.create(width=100, height=100)
        # Inside region, In1.Cu — removed.
        pcb.add_trace(start=(20, 20), end=(30, 20), width=0.25, layer="In1.Cu", net="SIG_A")
        # Inside region, F.Cu — kept (layer filter excludes it).
        pcb.add_trace(start=(20, 25), end=(30, 25), width=0.25, layer="F.Cu", net="SIG_B")
        # Outside region, In1.Cu — kept (spatial filter excludes it).
        pcb.add_trace(start=(60, 60), end=(70, 60), width=0.25, layer="In1.Cu", net="SIG_C")

        stats = pcb.strip_traces(region=(10, 10, 40, 40), layers=["In1.Cu"])

        assert stats["segments"] == 1
        assert len(pcb.segments) == 2
        remaining_layers = {seg.layer for seg in pcb.segments}
        assert remaining_layers == {"F.Cu", "In1.Cu"}

    def test_strip_region_triple_and_nets_layers(self):
        """--region + --nets + --layers all ANDed simultaneously."""
        pcb = PCB.create(width=100, height=100)
        # The only match: inside region AND In1.Cu AND net TARGET.
        pcb.add_trace(start=(20, 20), end=(30, 20), width=0.25, layer="In1.Cu", net="TARGET")
        # Wrong net.
        pcb.add_trace(start=(20, 22), end=(30, 22), width=0.25, layer="In1.Cu", net="OTHER")
        # Wrong layer.
        pcb.add_trace(start=(20, 24), end=(30, 24), width=0.25, layer="F.Cu", net="TARGET")
        # Wrong region.
        pcb.add_trace(start=(60, 60), end=(70, 60), width=0.25, layer="In1.Cu", net="TARGET")

        stats = pcb.strip_traces(region=(10, 10, 40, 40), nets=["TARGET"], layers=["In1.Cu"])

        assert stats["segments"] == 1
        assert len(pcb.segments) == 3

    def test_strip_region_inverted_coords_normalized(self):
        """Inverted region coords (x1>x2, y1>y2) are normalized by the API."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(20, 20), end=(30, 20), width=0.25, layer="F.Cu", net="SIG_A")

        # Pass the box corners in reversed order — should behave identically.
        stats = pcb.strip_traces(region=(40, 40, 10, 10))

        assert stats["segments"] == 1
        assert len(pcb.segments) == 0

    def test_strip_region_no_traces_is_noop(self):
        """A region containing zero traces removes nothing."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(60, 60), end=(70, 60), width=0.25, layer="F.Cu", net="SIG_A")

        stats = pcb.strip_traces(region=(10, 10, 40, 40))

        assert stats["segments"] == 0
        assert stats["vias"] == 0
        assert stats["segments_clipped"] == 0
        assert stats["segments_boundary_skipped"] == 0
        assert len(pcb.segments) == 1

    def test_strip_region_default_none_is_unbounded(self):
        """region=None (default) preserves existing whole-board behavior."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(20, 20), end=(30, 20), width=0.25, layer="F.Cu", net="SIG_A")
        pcb.add_trace(start=(60, 60), end=(70, 60), width=0.25, layer="F.Cu", net="SIG_B")

        stats = pcb.strip_traces()

        assert stats["segments"] == 2
        assert len(pcb.segments) == 0


# ---------------------------------------------------------------------------
# KiCad 10 name-only net format: net_number recovery from PCB headers
# ---------------------------------------------------------------------------


KICAD10_NAMEONLY_PCB = """\
(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (footprint "TestFP"
    (layer "F.Cu")
    (uuid "fp-uuid-1")
    (at 100 100)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS") (uuid "ref-uuid"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net "GND"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net "VCC"))
  )
  (segment (start 99 100) (end 101 100) (width 0.25) (layer "F.Cu") (net "GND") (uuid "seg-1"))
)
"""


class TestKiCad10NetNumberRecovery:
    """Test that net_number is recovered from PCB header when KiCad 10 uses name-only format."""

    def test_pad_recovers_net_number_from_header(self, tmp_path):
        """Pad.from_sexp with (net "GND") gets net_number=1 from header (net 1 "GND")."""
        pcb_path = tmp_path / "kicad10.kicad_pcb"
        pcb_path.write_text(KICAD10_NAMEONLY_PCB)
        pcb = PCB.load(pcb_path)

        # R1 pad 1 has (net "GND") -> should recover net_number=1
        fp = pcb.footprints[0]
        pad1 = next(p for p in fp.pads if p.number == "1")
        pad2 = next(p for p in fp.pads if p.number == "2")

        assert pad1.net_name == "GND"
        assert pad1.net_number == 1

        assert pad2.net_name == "VCC"
        assert pad2.net_number == 2

    def test_segment_recovers_net_number_from_header(self, tmp_path):
        """Segment with (net "GND") gets net_number=1 from header."""
        pcb_path = tmp_path / "kicad10.kicad_pcb"
        pcb_path.write_text(KICAD10_NAMEONLY_PCB)
        pcb = PCB.load(pcb_path)

        assert len(pcb.segments) == 1
        seg = pcb.segments[0]
        assert seg.net_name == "GND"
        assert seg.net_number == 1

    def test_via_recovers_net_number_from_header(self, tmp_path):
        """Via with (net "VCC") gets net_number=2 from header."""
        pcb_content = """\
(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (via (at 100 100) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "VCC") (uuid "via-1"))
)
"""
        pcb_path = tmp_path / "kicad10_via.kicad_pcb"
        pcb_path.write_text(pcb_content)
        pcb = PCB.load(pcb_path)

        assert len(pcb.vias) == 1
        via = pcb.vias[0]
        assert via.net_name == "VCC"
        assert via.net_number == 2

    def test_empty_net_stays_zero(self, tmp_path):
        """Pad with (net "") or (net 0 "") stays at net_number=0."""
        pcb_content = """\
(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "TestFP"
    (layer "F.Cu")
    (uuid "fp-uuid")
    (at 100 100)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS") (uuid "ref-uuid"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)
"""
        pcb_path = tmp_path / "empty_net.kicad_pcb"
        pcb_path.write_text(pcb_content)
        pcb = PCB.load(pcb_path)

        pad = pcb.footprints[0].pads[0]
        assert pad.net_number == 0
        assert pad.net_name == ""

    def test_pad_without_net_child_stays_zero(self, tmp_path):
        """Pad with no (net ...) child at all stays at net_number=0."""
        pcb_content = """\
(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "TestFP"
    (layer "F.Cu")
    (uuid "fp-uuid")
    (at 100 100)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS") (uuid "ref-uuid"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  )
)
"""
        pcb_path = tmp_path / "no_net.kicad_pcb"
        pcb_path.write_text(pcb_content)
        pcb = PCB.load(pcb_path)

        pad = pcb.footprints[0].pads[0]
        assert pad.net_number == 0
        assert pad.net_name == ""

    def test_fixture_zone_fill_pcb_recovers_nets(self):
        """The test_zone_fill.kicad_pcb fixture (KiCad 10) recovers net numbers."""
        fixture_path = Path(__file__).parent / "fixtures" / "test_zone_fill.kicad_pcb"
        if not fixture_path.exists():
            pytest.skip("test_zone_fill.kicad_pcb fixture not available")

        pcb = PCB.load(fixture_path)

        # Header should have net 1 "GND" and net 2 "VCC"
        assert 1 in pcb.nets
        assert pcb.nets[1].name == "GND"
        assert 2 in pcb.nets
        assert pcb.nets[2].name == "VCC"

        # All pads with net_name "GND" should have net_number=1
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.net_name == "GND":
                    assert pad.net_number == 1, (
                        f"{fp.reference} pad {pad.number}: "
                        f"expected net_number=1, got {pad.net_number}"
                    )
                elif pad.net_name == "VCC":
                    assert pad.net_number == 2, (
                        f"{fp.reference} pad {pad.number}: "
                        f"expected net_number=2, got {pad.net_number}"
                    )

        # Segments should also recover
        for seg in pcb.segments:
            if seg.net_name == "GND":
                assert seg.net_number == 1
            elif seg.net_name == "VCC":
                assert seg.net_number == 2

    def test_save_board_no_header_synthesizes_net_table(self, tmp_path):
        """No top-level (net N) table + inline (net "name") -> table synthesized.

        Reproduces KiCad 10.0.4 ``--save-board`` output: the header net table
        is deleted and every inline ref is name-only.  The parser must
        synthesize the table instead of collapsing every element to
        net_number=0 (the silent false-clean failure mode of issue #4021).
        """
        pcb_content = """\
(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (footprint "TestFP"
    (layer "F.Cu")
    (uuid "fp-uuid")
    (at 100 100)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS") (uuid "ref-uuid"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "VCC"))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net "GND"))
  )
  (segment (start 0 0) (end 10 0) (width 0.25) (layer "F.Cu") (net "GND") (uuid "seg-1"))
  (via (at 5 5) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "VCC") (uuid "via-1"))
)
"""
        pcb_path = tmp_path / "save_board.kicad_pcb"
        pcb_path.write_text(pcb_content)
        pcb = PCB.load(pcb_path)

        # A net table must have been synthesized (net 0 "" plus the two nets).
        assert pcb.nets, "net table should be synthesized from inline names"
        assert pcb.nets[0].name == ""  # sentinel preserved

        # Names -> deterministic first-seen numbering: VCC=1 (pad 1), GND=2.
        name_to_num = {net.name: net.number for net in pcb.nets.values()}
        assert name_to_num["VCC"] == 1
        assert name_to_num["GND"] == 2

        pad1 = next(p for p in pcb.footprints[0].pads if p.number == "1")
        pad2 = next(p for p in pcb.footprints[0].pads if p.number == "2")
        assert (pad1.net_name, pad1.net_number) == ("VCC", 1)
        assert (pad2.net_name, pad2.net_number) == ("GND", 2)

        # Segment and via recover the same numbers, not 0.
        assert pcb.segments[0].net_number == 2  # GND
        assert pcb.vias[0].net_number == 1  # VCC

    def test_save_board_numbering_is_deterministic(self, tmp_path):
        """Synthesized net numbers are stable across repeated loads."""
        pcb_content = """\
(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (footprint "TestFP"
    (layer "F.Cu")
    (uuid "fp-uuid")
    (at 100 100)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS") (uuid "ref-uuid"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "SIGA"))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net "SIGB"))
    (pad "3" smd rect (at 4 0) (size 1 1) (layers "F.Cu") (net "SIGC"))
  )
)
"""
        pcb_path = tmp_path / "save_board_det.kicad_pcb"
        pcb_path.write_text(pcb_content)

        first = {net.name: net.number for net in PCB.load(pcb_path).nets.values()}
        second = {net.name: net.number for net in PCB.load(pcb_path).nets.values()}
        assert first == second
        assert first == {"": 0, "SIGA": 1, "SIGB": 2, "SIGC": 3}

    def test_save_board_preserves_surviving_numeric_ref(self, tmp_path):
        """A surviving inline (net N "name") keeps its number when synthesizing."""
        pcb_content = """\
(kicad_pcb
  (version 20260206)
  (generator "pcbnew")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (footprint "TestFP"
    (layer "F.Cu")
    (uuid "fp-uuid")
    (at 100 100)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS") (uuid "ref-uuid"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "VCC"))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 7 "GND"))
  )
)
"""
        pcb_path = tmp_path / "save_board_survivor.kicad_pcb"
        pcb_path.write_text(pcb_content)
        pcb = PCB.load(pcb_path)

        name_to_num = {net.name: net.number for net in pcb.nets.values()}
        # GND keeps its surviving number 7; VCC fills the lowest free slot (1).
        assert name_to_num["GND"] == 7
        assert name_to_num["VCC"] == 1

    def test_save_board_fixture_recovers_all_nets(self):
        """The committed --save-board fixture recovers all four nets.

        Guards the exact issue #4021 failure: a real KiCad 10.0.4
        ``kicad-cli pcb drc --refill-zones --save-board`` output (no header
        net table, name-only inline refs) must load with a populated net
        table and nonzero pad net_numbers, and ConnectivityValidator must
        report total_nets > 0 rather than a false-clean 0.
        """
        from kicad_tools.validate.connectivity import ConnectivityValidator

        fixture_path = Path(__file__).parent / "fixtures" / "test_kicad10_save_board.kicad_pcb"
        assert fixture_path.exists(), "test_kicad10_save_board.kicad_pcb fixture missing"

        pcb = PCB.load(fixture_path)

        # The board has four nets: "" (sentinel), VCC, LED_ANODE, GND.
        assert pcb.nets, "net table should be synthesized, not empty"
        names = {net.name for net in pcb.nets.values()}
        assert names == {"", "VCC", "LED_ANODE", "GND"}

        name_to_num = {net.name: net.number for net in pcb.nets.values()}
        # Every populated pad must recover a nonzero number matching its name.
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.net_name:
                    assert pad.net_number != 0, (
                        f"{fp.reference} pad {pad.number} ({pad.net_name}) "
                        f"collapsed to net_number=0"
                    )
                    assert pad.net_number == name_to_num[pad.net_name]

        # Regression guard for the "silent false-clean" mode.
        result = ConnectivityValidator(pcb).validate()
        assert result.total_nets > 0

    def test_save_board_fixture_round_trips_through_save(self, tmp_path):
        """Loading + saving the --save-board fixture writes back a net table."""
        fixture_path = Path(__file__).parent / "fixtures" / "test_kicad10_save_board.kicad_pcb"
        assert fixture_path.exists()

        pcb = PCB.load(fixture_path)
        out_path = tmp_path / "resaved.kicad_pcb"
        pcb.save(out_path)

        # The saved file must contain a canonical (net N "name") header table.
        text = out_path.read_text()
        assert '(net 0 "")' in text
        assert "(net 1" in text

        # And re-loading recovers the same nets.
        reloaded = PCB.load(out_path)
        assert {n.name for n in reloaded.nets.values()} == {
            "",
            "VCC",
            "LED_ANODE",
            "GND",
        }

    def test_traditional_format_still_works(self, minimal_pcb):
        """Traditional (net N "name") format still parses correctly."""
        pcb = PCB.load(minimal_pcb)

        # minimal_pcb uses traditional format: (net 1 "GND"), (net 2 "+3.3V")
        fp = pcb.footprints[0]
        pad1 = next(p for p in fp.pads if p.number == "1")
        pad2 = next(p for p in fp.pads if p.number == "2")

        assert pad1.net_number == 1
        assert pad1.net_name == "GND"
        assert pad2.net_number == 2
        assert pad2.net_name == "+3.3V"


class TestRemoveFootprint:
    """Tests for PCB.remove_footprint method."""

    def test_remove_existing_footprint(self, minimal_pcb: Path):
        """Test that a footprint is removed from both sexp and in-memory list."""
        pcb = PCB.load(str(minimal_pcb))
        assert pcb.get_footprint("R1") is not None
        assert len(pcb.footprints) == 1

        result = pcb.remove_footprint("R1")

        assert result is True
        assert pcb.get_footprint("R1") is None
        assert len(pcb.footprints) == 0

    def test_remove_nonexistent_footprint(self, minimal_pcb: Path):
        """Test that removing a nonexistent footprint returns False."""
        pcb = PCB.load(str(minimal_pcb))

        result = pcb.remove_footprint("NONEXISTENT")

        assert result is False
        assert len(pcb.footprints) == 1

    def test_remove_footprint_persists_to_save(self, minimal_pcb: Path, tmp_path: Path):
        """Test that removed footprint is not present after save and reload."""
        pcb = PCB.load(str(minimal_pcb))
        pcb.remove_footprint("R1")

        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(str(output_path))

        pcb2 = PCB.load(str(output_path))
        assert pcb2.get_footprint("R1") is None
        assert len(pcb2.footprints) == 0

    def test_remove_footprint_sexp_node_gone(self, minimal_pcb: Path):
        """Test that the footprint S-expression node is removed from the tree."""
        pcb = PCB.load(str(minimal_pcb))
        fp_nodes_before = pcb._sexp.find_all("footprint")
        assert len(fp_nodes_before) == 1

        pcb.remove_footprint("R1")

        fp_nodes_after = pcb._sexp.find_all("footprint")
        assert len(fp_nodes_after) == 0


class TestRemoveSegments:
    """Tests for PCB.remove_segments method."""

    def test_remove_segment_by_uuid(self, minimal_pcb: Path):
        """Test removing a segment matched by UUID."""
        pcb = PCB.load(str(minimal_pcb))
        assert len(pcb.segments) == 1

        seg = pcb.segments[0]
        assert seg.uuid == "00000000-0000-0000-0000-000000000020"

        removed = pcb.remove_segments([seg])

        assert removed == 1
        assert len(pcb.segments) == 0

    def test_remove_segment_persists_to_save(self, minimal_pcb: Path, tmp_path: Path):
        """Test that removed segments are not present after save and reload."""
        pcb = PCB.load(str(minimal_pcb))
        seg = pcb.segments[0]

        pcb.remove_segments([seg])

        output_path = tmp_path / "output.kicad_pcb"
        pcb.save(str(output_path))

        pcb2 = PCB.load(str(output_path))
        assert len(pcb2.segments) == 0

    def test_remove_empty_list(self, minimal_pcb: Path):
        """Test that removing an empty list returns 0 and changes nothing."""
        pcb = PCB.load(str(minimal_pcb))
        assert len(pcb.segments) == 1

        removed = pcb.remove_segments([])

        assert removed == 0
        assert len(pcb.segments) == 1

    def test_remove_segment_sexp_node_gone(self, minimal_pcb: Path):
        """Test that the segment S-expression node is removed from the tree."""
        pcb = PCB.load(str(minimal_pcb))
        seg_nodes_before = [c for c in pcb._sexp.children if not c.is_atom and c.name == "segment"]
        assert len(seg_nodes_before) == 1

        pcb.remove_segments(pcb.segments[:])

        seg_nodes_after = [c for c in pcb._sexp.children if not c.is_atom and c.name == "segment"]
        assert len(seg_nodes_after) == 0


class TestPCBConstructorGuard:
    """Tests for PCB constructor type guard (issue #1770)."""

    def test_pcb_constructor_rejects_string_path(self):
        """PCB('path/to/file.kicad_pcb') raises TypeError with helpful message."""
        with pytest.raises(TypeError, match=r"PCB\(\) expects a parsed SExp"):
            PCB("/path/to/board.kicad_pcb")

    def test_pcb_constructor_rejects_path_object(self):
        """PCB(Path('file.kicad_pcb')) raises TypeError with helpful message."""
        with pytest.raises(TypeError, match=r"Use PCB\.load\("):
            PCB(Path("/path/to/board.kicad_pcb"))

    def test_pcb_constructor_accepts_sexp(self, minimal_pcb: Path):
        """PCB(sexp) works when passed a proper SExp object."""
        from kicad_tools import load_pcb

        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)
        assert len(pcb.layers) > 0

    def test_pcb_load_kicad9_fixture(self):
        """PCB.load() succeeds on KiCad 9/10 format fixture files."""
        fixture_path = Path(__file__).parent / "fixtures" / "test_zone_fill.kicad_pcb"
        if not fixture_path.exists():
            pytest.skip("test_zone_fill.kicad_pcb fixture not available")

        pcb = PCB.load(fixture_path)
        assert len(pcb.layers) > 0
        assert len(pcb.nets) >= 0


class TestAddFootprintNumericPropertyQuoting:
    """Numeric Reference/Value properties must serialize quoted (issue #3802).

    ``add_footprint_from_file`` embeds Reference/Value into the board's
    S-expression tree. When the value parses as a float (e.g. a unit-less
    resistor value ``470``), the serializer's textual heuristic would emit a
    bare token ``(property "Value" 470)``. KiCad/kicad-cli reject that with
    "Failed to load board" (exit 3), making the whole board unloadable.

    These assertions run without kicad-cli so the regression is caught on any
    runner; ``tests/test_kicad_cli_roundtrip.py`` adds the load-level gate.
    """

    FIXTURES_DIR = Path(__file__).parent / "fixtures"
    TEST_PRETTY_DIR = FIXTURES_DIR / "Test_Library.pretty"

    # Footprint that already carries Reference/Value property nodes, to
    # exercise the existing-property write branch (the fixture footprint
    # below has none, exercising the synthesized-property fallback).
    _FP_WITH_PROPERTIES = """\
(footprint "Test:R_Numeric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-00000000aaaa")
    (property "Reference" "REF**" (at 0 -1.5 0) (layer "F.SilkS")
        (effects (font (size 1 1) (thickness 0.15)))
    )
    (property "Value" "Test:R_Numeric" (at 0 1.5 0) (layer "F.Fab")
        (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu"))
)
"""

    @pytest.mark.parametrize("value", ["470", "0", "100", "-5", "3.3"])
    def test_synthesized_value_property_is_quoted(self, minimal_pcb, tmp_path, value):
        """A numeric Value emits ``(property "Value" "<n>")`` (synthesized branch)."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="R1",
            x=50.0,
            y=30.0,
            value=value,
        )
        out = tmp_path / "synth.kicad_pcb"
        pcb.save(out)
        contents = out.read_text()
        assert f'(property "Value" "{value}"' in contents, (
            f"Numeric Value {value!r} must serialize quoted; bare numeric "
            "makes the board unloadable in kicad-cli. Got:\n"
            + "\n".join(line for line in contents.splitlines() if "Value" in line)
        )
        # The bug emitted the value as a bare numeric token (no quotes).
        assert f'(property "Value" {value}' not in contents

    @pytest.mark.parametrize("value", ["470", "0", "100"])
    def test_existing_value_property_is_quoted(self, minimal_pcb, tmp_path, value):
        """A numeric Value emits quoted when the footprint already has the prop."""
        fp_path = tmp_path / "R_Numeric.kicad_mod"
        fp_path.write_text(self._FP_WITH_PROPERTIES)

        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)
        pcb.add_footprint_from_file(
            kicad_mod_path=fp_path,
            reference="R1",
            x=50.0,
            y=30.0,
            value=value,
        )
        out = tmp_path / "existing.kicad_pcb"
        pcb.save(out)
        contents = out.read_text()
        assert f'(property "Value" "{value}"' in contents
        assert f'(property "Value" {value} ' not in contents

    def test_numeric_reference_is_quoted(self, minimal_pcb, tmp_path):
        """A numeric-looking Reference designator serializes quoted."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="1",
            x=50.0,
            y=30.0,
            value="10k",
        )
        out = tmp_path / "ref.kicad_pcb"
        pcb.save(out)
        contents = out.read_text()
        assert '(property "Reference" "1"' in contents
        assert '(property "Reference" 1' not in contents

    def test_structural_numeric_tokens_stay_unquoted(self, minimal_pcb, tmp_path):
        """The fix must not over-quote structural numerics (at/size/layer idx)."""
        doc = load_pcb(str(minimal_pcb))
        pcb = PCB(doc)
        pcb.add_footprint_from_file(
            kicad_mod_path=self.TEST_PRETTY_DIR / "C_0402_1005Metric.kicad_mod",
            reference="C1",
            x=50.0,
            y=30.0,
            value="470",
        )
        out = tmp_path / "structural.kicad_pcb"
        pcb.save(out)
        contents = out.read_text()
        # Pad/geometry coordinates and sizes must remain bare numerics.
        assert "(size 1 1)" in contents or "(size " in contents
        assert '(size "1" "1")' not in contents
        assert '(thickness "0.15")' not in contents


class TestCopperDedup:
    """Issue #4175: emission-time copper dedup + dedupe_copper() cleanup.

    ``route-auto`` retries re-solve the same corridor and previously appended
    an exact-duplicate, uuid-distinct copy of the same copper on every call
    (717 duplicates observed on one board).  ``add_trace``/``add_via`` now skip
    exact duplicates by default, and ``dedupe_copper()`` cleans up boards that
    were already bloated.
    """

    def _seg_kwargs(self, **overrides):
        base = {
            "start": (10.0, 10.0),
            "end": (50.0, 10.0),
            "width": 0.25,
            "layer": "F.Cu",
            "net": "Sig1",
        }
        base.update(overrides)
        return base

    def test_add_trace_skips_exact_duplicate(self):
        """A second identical add_trace is a no-op (skipped, not appended)."""
        pcb = PCB.create(width=100, height=100)
        first = pcb.add_trace(**self._seg_kwargs())
        assert len(first) == 1
        assert len(pcb.segments) == 1

        # Identical geometry -> skipped, returns empty list, count unchanged.
        second = pcb.add_trace(**self._seg_kwargs())
        assert second == []
        assert len(pcb.segments) == 1

    def test_add_trace_dedup_is_order_insensitive(self):
        """Reversed start/end is treated as the same segment (duplicate)."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(**self._seg_kwargs())
        reversed_seg = pcb.add_trace(**self._seg_kwargs(start=(50.0, 10.0), end=(10.0, 10.0)))
        assert reversed_seg == []
        assert len(pcb.segments) == 1

    def test_add_trace_different_layer_not_deduped(self):
        """Same endpoints on a different layer are distinct (via transition)."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(**self._seg_kwargs(layer="F.Cu"))
        pcb.add_trace(**self._seg_kwargs(layer="B.Cu"))
        assert len(pcb.segments) == 2

    def test_add_trace_different_net_not_deduped(self):
        """Identical geometry on different nets is not deduped."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(**self._seg_kwargs(net="Sig1"))
        pcb.add_trace(**self._seg_kwargs(net="Sig2"))
        assert len(pcb.segments) == 2

    def test_add_trace_different_width_not_deduped(self):
        """Same endpoints but different width is a distinct segment (neck-down)."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(**self._seg_kwargs(width=0.25))
        pcb.add_trace(**self._seg_kwargs(width=0.5))
        assert len(pcb.segments) == 2

    def test_add_trace_shared_endpoint_not_deduped(self):
        """Two segments sharing one endpoint (different other end) are distinct."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10.0, 10.0), end=(50.0, 10.0), net="Sig1")
        pcb.add_trace(start=(50.0, 10.0), end=(50.0, 40.0), net="Sig1")
        assert len(pcb.segments) == 2

    def test_add_trace_dedup_can_be_disabled(self):
        """dedupe=False preserves the historical always-append behavior."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(**self._seg_kwargs())
        pcb.add_trace(**self._seg_kwargs(dedupe=False))
        assert len(pcb.segments) == 2

    def test_add_via_skips_exact_duplicate(self):
        """A second identical add_via is skipped and returns None."""
        pcb = PCB.create(width=100, height=100)
        via1 = pcb.add_via(x=50.0, y=30.0, net="GND")
        assert via1 is not None
        assert len(pcb.vias) == 1

        via2 = pcb.add_via(x=50.0, y=30.0, net="GND")
        assert via2 is None
        assert len(pcb.vias) == 1

    def test_add_via_different_net_not_deduped(self):
        """Same position on a different net is not a duplicate."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_via(x=50.0, y=30.0, net="GND")
        pcb.add_via(x=50.0, y=30.0, net="VCC")
        assert len(pcb.vias) == 2

    def test_add_via_different_position_not_deduped(self):
        """Different positions are distinct vias."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_via(x=50.0, y=30.0, net="GND")
        pcb.add_via(x=50.0, y=31.0, net="GND")
        assert len(pcb.vias) == 2

    def test_dedup_persists_across_save_reload(self, tmp_path):
        """Dedup at emission time leaves exactly one instance after save."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(**self._seg_kwargs())
        pcb.add_trace(**self._seg_kwargs())  # duplicate, skipped
        pcb.add_via(x=50.0, y=30.0, net="GND")
        pcb.add_via(x=50.0, y=30.0, net="GND")  # duplicate, skipped

        out = tmp_path / "deduped.kicad_pcb"
        pcb.save(out)
        reloaded = PCB.load(out)
        assert len(reloaded.segments) == 1
        assert len(reloaded.vias) == 1

    def test_dedupe_copper_removes_preexisting_duplicates(self, tmp_path):
        """dedupe_copper() cleans up a board bloated with exact duplicates."""
        pcb = PCB.create(width=100, height=100)
        # Seed exact-duplicate copper using dedupe=False so the bloat exists.
        for _ in range(4):
            pcb.add_trace(**self._seg_kwargs(dedupe=False))
        for _ in range(3):
            pcb.add_via(x=50.0, y=30.0, net="GND", dedupe=False)
        # A genuinely-distinct segment/via must survive.
        pcb.add_trace(**self._seg_kwargs(end=(50.0, 40.0), dedupe=False))
        pcb.add_via(x=60.0, y=30.0, net="GND", dedupe=False)

        assert len(pcb.segments) == 5
        assert len(pcb.vias) == 4

        stats = pcb.dedupe_copper()
        assert stats["segments"] == 3  # 4 identical -> 1 kept
        assert stats["vias"] == 2  # 3 identical -> 1 kept
        assert len(pcb.segments) == 2
        assert len(pcb.vias) == 2

        # Cleanup persists across save/reload (sexp nodes removed too).
        out = tmp_path / "cleaned.kicad_pcb"
        pcb.save(out)
        reloaded = PCB.load(out)
        assert len(reloaded.segments) == 2
        assert len(reloaded.vias) == 2

    def test_dedupe_copper_noop_on_clean_board(self):
        """dedupe_copper() reports zero removed on a board with no duplicates."""
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(**self._seg_kwargs())
        pcb.add_via(x=50.0, y=30.0, net="GND")
        stats = pcb.dedupe_copper()
        assert stats == {"segments": 0, "vias": 0}
        assert len(pcb.segments) == 1
        assert len(pcb.vias) == 1

    def test_dedupe_preserves_net_connectivity(self):
        """Removing a duplicate keeps an identical copy, so nets stay linked."""
        pcb = PCB.create(width=100, height=100)
        net_num = pcb.add_net("Sig1").number
        for _ in range(3):
            pcb.add_trace(**self._seg_kwargs(dedupe=False))

        before = {
            (round(s.start[0], 3), round(s.start[1], 3), round(s.end[0], 3), round(s.end[1], 3))
            for s in pcb.segments_in_net(net_num)
        }
        pcb.dedupe_copper()
        after = {
            (round(s.start[0], 3), round(s.start[1], 3), round(s.end[0], 3), round(s.end[1], 3))
            for s in pcb.segments_in_net(net_num)
        }
        # The set of distinct segment geometries is unchanged: no net edge lost.
        assert before == after
