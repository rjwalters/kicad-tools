"""Tests for PCB parsing and editing."""

import pytest
from pathlib import Path

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
        from kicad_tools.pcb.editor import Track, Point

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
        from kicad_tools.pcb.editor import Track, Point

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
        from kicad_tools.pcb.editor import Via, Point

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
        from kicad_tools.pcb.editor import Via, Point

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
        from kicad_tools.pcb.editor import Zone, Point

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
        from kicad_tools.pcb.editor import Zone, Point

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

        for name, pads in COMMON_FOOTPRINTS.items():
            assert isinstance(pads, dict)
            for pad_name, pos in pads.items():
                assert isinstance(pad_name, str)
                assert len(pos) == 2
                assert isinstance(pos[0], (int, float))
                assert isinstance(pos[1], (int, float))

    def test_footprint_aliases_valid(self):
        """Test FOOTPRINT_ALIASES resolve to valid footprints."""
        from kicad_tools.pcb.footprints import FOOTPRINT_ALIASES, COMMON_FOOTPRINTS

        for alias, full_name in FOOTPRINT_ALIASES.items():
            assert full_name in COMMON_FOOTPRINTS, f"Alias {alias} -> {full_name} not found"


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
