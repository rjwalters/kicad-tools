"""Tests for the footprints module."""


class TestPadType:
    """Tests for PadType enum."""

    def test_smd_value(self):
        from kicad_tools.footprints import PadType

        assert PadType.SMD.value == "smd"

    def test_tht_value(self):
        from kicad_tools.footprints import PadType

        assert PadType.THT.value == "thru_hole"

    def test_npth_value(self):
        from kicad_tools.footprints import PadType

        assert PadType.NPTH.value == "np_thru_hole"

    def test_connect_value(self):
        from kicad_tools.footprints import PadType

        assert PadType.CONNECT.value == "connect"


class TestPadShape:
    """Tests for PadShape enum."""

    def test_rect_value(self):
        from kicad_tools.footprints import PadShape

        assert PadShape.RECT.value == "rect"

    def test_roundrect_value(self):
        from kicad_tools.footprints import PadShape

        assert PadShape.ROUNDRECT.value == "roundrect"

    def test_circle_value(self):
        from kicad_tools.footprints import PadShape

        assert PadShape.CIRCLE.value == "circle"

    def test_oval_value(self):
        from kicad_tools.footprints import PadShape

        assert PadShape.OVAL.value == "oval"

    def test_trapezoid_value(self):
        from kicad_tools.footprints import PadShape

        assert PadShape.TRAPEZOID.value == "trapezoid"


class TestLayer:
    """Tests for Layer enum."""

    def test_front_copper(self):
        from kicad_tools.footprints import Layer

        assert Layer.F_CU.value == "F.Cu"

    def test_back_copper(self):
        from kicad_tools.footprints import Layer

        assert Layer.B_CU.value == "B.Cu"

    def test_front_paste(self):
        from kicad_tools.footprints import Layer

        assert Layer.F_PASTE.value == "F.Paste"

    def test_back_paste(self):
        from kicad_tools.footprints import Layer

        assert Layer.B_PASTE.value == "B.Paste"

    def test_front_mask(self):
        from kicad_tools.footprints import Layer

        assert Layer.F_MASK.value == "F.Mask"

    def test_back_mask(self):
        from kicad_tools.footprints import Layer

        assert Layer.B_MASK.value == "B.Mask"

    def test_front_silkscreen(self):
        from kicad_tools.footprints import Layer

        assert Layer.F_SILKS.value == "F.SilkS"

    def test_back_silkscreen(self):
        from kicad_tools.footprints import Layer

        assert Layer.B_SILKS.value == "B.SilkS"

    def test_front_courtyard(self):
        from kicad_tools.footprints import Layer

        assert Layer.F_CRTYD.value == "F.CrtYd"

    def test_back_courtyard(self):
        from kicad_tools.footprints import Layer

        assert Layer.B_CRTYD.value == "B.CrtYd"

    def test_edge_cuts(self):
        from kicad_tools.footprints import Layer

        assert Layer.EDGE_CUTS.value == "Edge.Cuts"


class TestPad:
    """Tests for Pad class."""

    def test_smd_pad_creation(self):
        from kicad_tools.footprints import Layer, Pad, PadShape, PadType

        pad = Pad(
            number="1",
            pad_type=PadType.SMD,
            shape=PadShape.RECT,
            position=(0.0, 0.0),
            size=(1.0, 0.5),
            layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
        )

        assert pad.number == "1"
        assert pad.pad_type == PadType.SMD
        assert pad.shape == PadShape.RECT
        assert pad.position == (0.0, 0.0)
        assert pad.size == (1.0, 0.5)
        assert Layer.F_CU in pad.layers
        assert Layer.F_PASTE in pad.layers
        assert Layer.F_MASK in pad.layers
        assert pad.drill == 0

    def test_tht_pad_with_drill(self):
        from kicad_tools.footprints import Layer, Pad, PadShape, PadType

        pad = Pad(
            number="1",
            pad_type=PadType.THT,
            shape=PadShape.CIRCLE,
            position=(0.0, 0.0),
            size=(1.7, 1.7),
            layers=[Layer.F_CU, Layer.B_CU, Layer.F_MASK, Layer.B_MASK],
            drill=1.0,
        )

        assert pad.pad_type == PadType.THT
        assert pad.shape == PadShape.CIRCLE
        assert pad.drill == 1.0
        assert Layer.F_CU in pad.layers
        assert Layer.B_CU in pad.layers

    def test_roundrect_pad(self):
        from kicad_tools.footprints import Layer, Pad, PadShape, PadType

        pad = Pad(
            number="2",
            pad_type=PadType.SMD,
            shape=PadShape.ROUNDRECT,
            position=(1.5, 0.0),
            size=(0.6, 0.8),
            layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
        )

        assert pad.shape == PadShape.ROUNDRECT

    def test_oval_pad(self):
        from kicad_tools.footprints import Layer, Pad, PadShape, PadType

        pad = Pad(
            number="3",
            pad_type=PadType.THT,
            shape=PadShape.OVAL,
            position=(0.0, 2.54),
            size=(1.7, 1.7),
            layers=[Layer.F_CU, Layer.B_CU],
            drill=1.0,
        )

        assert pad.shape == PadShape.OVAL

    def test_npth_pad(self):
        from kicad_tools.footprints import Pad, PadShape, PadType

        pad = Pad(
            number="",
            pad_type=PadType.NPTH,
            shape=PadShape.CIRCLE,
            position=(0.0, 0.0),
            size=(3.2, 3.2),
            layers=[],
            drill=3.0,
        )

        assert pad.pad_type == PadType.NPTH
        assert pad.drill == 3.0


class TestFootprint:
    """Tests for Footprint class."""

    def test_footprint_creation(self):
        from kicad_tools.footprints import Footprint

        fp = Footprint(name="R_0402_1005Metric")
        assert fp.name == "R_0402_1005Metric"
        assert fp.pads == []
        assert fp.silkscreen == []
        assert fp.courtyard == []

    def test_add_pad(self):
        from kicad_tools.footprints import Footprint, Layer, Pad, PadShape, PadType

        fp = Footprint(name="R_0402_1005Metric")

        pad1 = Pad(
            number="1",
            pad_type=PadType.SMD,
            shape=PadShape.ROUNDRECT,
            position=(-0.51, 0.0),
            size=(0.54, 0.64),
            layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
        )

        pad2 = Pad(
            number="2",
            pad_type=PadType.SMD,
            shape=PadShape.ROUNDRECT,
            position=(0.51, 0.0),
            size=(0.54, 0.64),
            layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
        )

        fp.add_pad(pad1)
        fp.add_pad(pad2)

        assert len(fp.pads) == 2
        assert fp.pads[0].number == "1"
        assert fp.pads[1].number == "2"

    def test_to_sexp_minimal(self):
        """Test to_sexp() with a minimal footprint."""
        from kicad_tools.footprints import Footprint

        fp = Footprint(name="test")
        sexp = fp.to_sexp()

        assert "(footprint" in sexp
        assert '"test"' in sexp
        assert "(version 20240108)" in sexp
        assert '(generator "kicad-tools")' in sexp
        assert '(layer "F.Cu")' in sexp
        assert "(attr smd)" in sexp

    def test_to_sexp_with_description_and_tags(self):
        """Test to_sexp() with description and tags."""
        from kicad_tools.footprints import Footprint

        fp = Footprint(
            name="C_0402_1005Metric",
            description="Capacitor SMD 0402",
            tags="capacitor smd 0402",
        )
        sexp = fp.to_sexp()

        assert '(descr "Capacitor SMD 0402")' in sexp
        assert '(tags "capacitor smd 0402")' in sexp

    def test_to_sexp_with_pads(self):
        """Test to_sexp() with pads."""
        from kicad_tools.footprints import Footprint, Layer, Pad, PadShape, PadType

        fp = Footprint(name="C_0402_1005Metric")
        fp.add_pad(
            Pad(
                number="1",
                pad_type=PadType.SMD,
                shape=PadShape.ROUNDRECT,
                position=(-0.48, 0),
                size=(0.56, 0.62),
                layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
            )
        )
        fp.add_pad(
            Pad(
                number="2",
                pad_type=PadType.SMD,
                shape=PadShape.ROUNDRECT,
                position=(0.48, 0),
                size=(0.56, 0.62),
                layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
            )
        )

        sexp = fp.to_sexp()

        assert '(pad "1" smd roundrect' in sexp
        assert '(pad "2" smd roundrect' in sexp
        assert "(at -0.48 0)" in sexp
        assert "(at 0.48 0)" in sexp
        assert "(size 0.56 0.62)" in sexp
        assert '(layers "F.Cu" "F.Paste" "F.Mask")' in sexp
        assert "(roundrect_rratio 0.25)" in sexp


class TestFootprintExports:
    """Tests for __all__ exports."""

    def test_all_exports(self):
        from kicad_tools.footprints import __all__

        expected = ["PadType", "PadShape", "Layer", "Pad", "Footprint"]
        assert set(__all__) == set(expected)

    def test_can_import_all_exports(self):
        from kicad_tools.footprints import Footprint, Layer, Pad, PadShape, PadType

        # Just verify they're importable
        assert PadType is not None
        assert PadShape is not None
        assert Layer is not None
        assert Pad is not None
        assert Footprint is not None


class TestFootprintUsagePatterns:
    """Tests for typical usage patterns."""

    def test_create_0402_resistor_footprint(self):
        """Test creating a typical 0402 resistor footprint."""
        from kicad_tools.footprints import Footprint, Layer, Pad, PadShape, PadType

        fp = Footprint(name="R_0402_1005Metric")

        # Standard 0402 pad dimensions
        pad_size = (0.54, 0.64)
        pad_spacing = 0.51

        smd_layers = [Layer.F_CU, Layer.F_PASTE, Layer.F_MASK]

        fp.add_pad(
            Pad(
                number="1",
                pad_type=PadType.SMD,
                shape=PadShape.ROUNDRECT,
                position=(-pad_spacing, 0.0),
                size=pad_size,
                layers=smd_layers,
            )
        )

        fp.add_pad(
            Pad(
                number="2",
                pad_type=PadType.SMD,
                shape=PadShape.ROUNDRECT,
                position=(pad_spacing, 0.0),
                size=pad_size,
                layers=smd_layers,
            )
        )

        assert len(fp.pads) == 2
        assert fp.pads[0].position[0] < 0  # Left pad
        assert fp.pads[1].position[0] > 0  # Right pad

    def test_create_soic8_footprint(self):
        """Test creating a SOIC-8 footprint."""
        from kicad_tools.footprints import Footprint, Layer, Pad, PadShape, PadType

        fp = Footprint(name="SOIC-8_3.9x4.9mm_P1.27mm")

        smd_layers = [Layer.F_CU, Layer.F_PASTE, Layer.F_MASK]
        pad_size = (1.5, 0.6)
        x_offset = 2.7
        y_pitch = 1.27
        y_start = -1.905  # (4 pins - 1) / 2 * pitch

        # Add 8 pads (4 on each side)
        for i in range(4):
            # Left side pads (1-4)
            fp.add_pad(
                Pad(
                    number=str(i + 1),
                    pad_type=PadType.SMD,
                    shape=PadShape.RECT,
                    position=(-x_offset, y_start + i * y_pitch),
                    size=pad_size,
                    layers=smd_layers,
                )
            )

        for i in range(4):
            # Right side pads (5-8, numbered bottom to top)
            fp.add_pad(
                Pad(
                    number=str(8 - i),
                    pad_type=PadType.SMD,
                    shape=PadShape.RECT,
                    position=(x_offset, y_start + i * y_pitch),
                    size=pad_size,
                    layers=smd_layers,
                )
            )

        assert len(fp.pads) == 8
        # Verify pin 1 is at expected position
        assert fp.pads[0].number == "1"
        assert fp.pads[0].position[0] < 0  # Left side

    def test_create_through_hole_header(self):
        """Test creating a through-hole pin header footprint."""
        from kicad_tools.footprints import Footprint, Layer, Pad, PadShape, PadType

        fp = Footprint(name="PinHeader_1x04_P2.54mm_Vertical")

        tht_layers = [Layer.F_CU, Layer.B_CU, Layer.F_MASK, Layer.B_MASK]
        pitch = 2.54
        pad_size = (1.7, 1.7)
        drill = 1.0

        for i in range(4):
            fp.add_pad(
                Pad(
                    number=str(i + 1),
                    pad_type=PadType.THT,
                    shape=PadShape.OVAL if i > 0 else PadShape.RECT,  # Pin 1 is rect
                    position=(0.0, i * pitch),
                    size=pad_size,
                    layers=tht_layers,
                    drill=drill,
                )
            )

        assert len(fp.pads) == 4
        assert fp.pads[0].shape == PadShape.RECT  # Pin 1
        assert fp.pads[1].shape == PadShape.OVAL  # Other pins
        assert all(p.drill == 1.0 for p in fp.pads)


class TestPadToSexp:
    """Tests for Pad.to_sexp() method."""

    def test_smd_roundrect_pad_to_sexp(self):
        """Test S-expression generation for SMD roundrect pad."""
        from kicad_tools.footprints import Layer, Pad, PadShape, PadType

        pad = Pad(
            number="1",
            pad_type=PadType.SMD,
            shape=PadShape.ROUNDRECT,
            position=(-0.48, 0),
            size=(0.56, 0.62),
            layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
        )
        sexp = pad.to_sexp()
        sexp_str = sexp.to_string(compact=True)

        assert '"1"' in sexp_str
        assert "smd" in sexp_str
        assert "roundrect" in sexp_str
        assert "(roundrect_rratio 0.25)" in sexp_str

    def test_smd_rect_pad_no_roundrect_ratio(self):
        """Test that rectangular pads don't include roundrect_rratio."""
        from kicad_tools.footprints import Layer, Pad, PadShape, PadType

        pad = Pad(
            number="1",
            pad_type=PadType.SMD,
            shape=PadShape.RECT,
            position=(0, 0),
            size=(1.0, 0.5),
            layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
        )
        sexp = pad.to_sexp()
        sexp_str = sexp.to_string(compact=True)

        assert "rect" in sexp_str
        assert "roundrect_rratio" not in sexp_str

    def test_tht_pad_with_drill(self):
        """Test S-expression generation for THT pad with drill."""
        from kicad_tools.footprints import Layer, Pad, PadShape, PadType

        pad = Pad(
            number="1",
            pad_type=PadType.THT,
            shape=PadShape.CIRCLE,
            position=(0, 0),
            size=(1.7, 1.7),
            layers=[Layer.F_CU, Layer.B_CU, Layer.F_MASK, Layer.B_MASK],
            drill=1.0,
        )
        sexp = pad.to_sexp()
        sexp_str = sexp.to_string(compact=True)

        assert "thru_hole" in sexp_str
        assert "circle" in sexp_str
        assert "(drill 1)" in sexp_str

    def test_custom_roundrect_ratio(self):
        """Test pad with custom roundrect ratio."""
        from kicad_tools.footprints import Layer, Pad, PadShape, PadType

        pad = Pad(
            number="1",
            pad_type=PadType.SMD,
            shape=PadShape.ROUNDRECT,
            position=(0, 0),
            size=(1.0, 0.5),
            layers=[Layer.F_CU],
            roundrect_rratio=0.5,
        )
        sexp = pad.to_sexp()
        sexp_str = sexp.to_string(compact=True)

        assert "(roundrect_rratio 0.5)" in sexp_str


class TestFootprintToSexpIntegration:
    """Integration tests for complete footprint S-expression generation."""

    def test_0402_capacitor_footprint(self):
        """Test generating a complete 0402 capacitor footprint."""
        from kicad_tools.footprints import Footprint, Layer, Pad, PadShape, PadType

        fp = Footprint(
            name="C_0402_1005Metric",
            description="Capacitor SMD 0402 (1005 Metric), standard IPC-7351",
            tags="capacitor smd 0402 1005",
        )
        smd_layers = [Layer.F_CU, Layer.F_PASTE, Layer.F_MASK]

        fp.add_pad(
            Pad(
                number="1",
                pad_type=PadType.SMD,
                shape=PadShape.ROUNDRECT,
                position=(-0.48, 0),
                size=(0.56, 0.62),
                layers=smd_layers,
            )
        )
        fp.add_pad(
            Pad(
                number="2",
                pad_type=PadType.SMD,
                shape=PadShape.ROUNDRECT,
                position=(0.48, 0),
                size=(0.56, 0.62),
                layers=smd_layers,
            )
        )

        sexp = fp.to_sexp()

        # Verify the footprint can be parsed back
        from kicad_tools.sexp import parse_string

        parsed = parse_string(sexp)
        assert parsed.name == "footprint"
        assert parsed.get_first_atom() == "C_0402_1005Metric"

        # Verify pads are present
        pads = parsed.find_all("pad")
        assert len(pads) == 2

    def test_tht_header_footprint(self):
        """Test generating a through-hole header footprint."""
        from kicad_tools.footprints import Footprint, Layer, Pad, PadShape, PadType

        fp = Footprint(
            name="PinHeader_1x04",
            description="Pin header 1x4",
            tags="pin header",
        )
        tht_layers = [Layer.F_CU, Layer.B_CU, Layer.F_MASK, Layer.B_MASK]

        for i in range(4):
            fp.add_pad(
                Pad(
                    number=str(i + 1),
                    pad_type=PadType.THT,
                    shape=PadShape.CIRCLE,
                    position=(0, i * 2.54),
                    size=(1.7, 1.7),
                    layers=tht_layers,
                    drill=1.0,
                )
            )

        sexp = fp.to_sexp()

        # THT footprints should have through_hole attribute
        assert "(attr through_hole)" in sexp
        assert "(drill 1)" in sexp

        # Verify it can be parsed
        from kicad_tools.sexp import parse_string

        parsed = parse_string(sexp)
        pads = parsed.find_all("pad")
        assert len(pads) == 4

    def test_roundtrip_parsing(self):
        """Test that generated footprint can be parsed and matches original structure."""
        from kicad_tools.footprints import Footprint, Layer, Pad, PadShape, PadType
        from kicad_tools.sexp import parse_string

        fp = Footprint(
            name="Test_Footprint",
            description="Test description",
            tags="test tags",
        )
        fp.add_pad(
            Pad(
                number="A1",
                pad_type=PadType.SMD,
                shape=PadShape.RECT,
                position=(1.5, -2.0),
                size=(0.8, 1.2),
                layers=[Layer.F_CU, Layer.F_PASTE, Layer.F_MASK],
            )
        )

        sexp_str = fp.to_sexp()
        parsed = parse_string(sexp_str)

        # Check structure
        assert parsed.name == "footprint"
        assert parsed["version"].get_first_atom() == 20240108
        assert parsed["generator"].get_first_atom() == "kicad-tools"
        assert parsed["layer"].get_first_atom() == "F.Cu"
        assert parsed["descr"].get_first_atom() == "Test description"
        assert parsed["tags"].get_first_atom() == "test tags"

        # Check pad
        pad = parsed.find("pad")
        assert pad is not None
        at_node = pad["at"]
        assert at_node.get_atoms() == [1.5, -2]
