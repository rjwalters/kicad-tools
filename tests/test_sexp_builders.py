"""Tests for S-expression builders."""

import pytest

from kicad_tools.sexp.builders import (
    fmt,
    xy,
    at,
    stroke,
    font,
    effects,
    uuid_node,
    property_node,
    color,
    pts,
    wire_node,
    junction_node,
    label_node,
    hier_label_node,
    text_node,
    symbol_property_node,
    pin_uuid_node,
    symbol_instances_node,
    title_block,
    sheet_instances,
    segment_node,
    via_node,
    zone_node,
    footprint_at_node,
)
from kicad_tools.sexp.parser import SExp


class TestFmtFunction:
    """Tests for the fmt() coordinate formatting function."""

    def test_fmt_integer_value(self):
        """Integer values should return int type."""
        assert fmt(10.0) == 10
        assert isinstance(fmt(10.0), int)

    def test_fmt_fractional_value(self):
        """Fractional values should return float."""
        assert fmt(10.5) == 10.5
        assert isinstance(fmt(10.5), float)

    def test_fmt_rounds_to_two_decimals(self):
        """Values should be rounded to 2 decimal places."""
        assert fmt(10.123) == 10.12
        assert fmt(10.126) == 10.13
        assert fmt(10.1234567) == 10.12

    def test_fmt_negative_values(self):
        """Negative values should work correctly."""
        assert fmt(-5.5) == -5.5
        assert fmt(-5.0) == -5

    def test_fmt_zero(self):
        """Zero should return integer 0."""
        assert fmt(0.0) == 0
        assert isinstance(fmt(0.0), int)


class TestXyBuilder:
    """Tests for the xy() coordinate builder."""

    def test_xy_basic(self):
        """Build basic xy node."""
        node = xy(10, 20)
        assert node.name == "xy"
        assert len(node.children) == 2
        assert node.children[0].value == 10
        assert node.children[1].value == 20

    def test_xy_with_floats(self):
        """xy with float coordinates."""
        node = xy(10.5, 20.75)
        assert node.children[0].value == 10.5
        assert node.children[1].value == 20.75

    def test_xy_formatting(self):
        """xy values should be formatted properly."""
        node = xy(10.999, 20.001)
        assert node.children[0].value == 11  # Rounded
        assert node.children[1].value == 20  # Rounded to int


class TestAtBuilder:
    """Tests for the at() position builder."""

    def test_at_without_rotation(self):
        """at() without rotation omits rotation value."""
        node = at(100, 200)
        assert node.name == "at"
        assert len(node.children) == 2
        assert node.children[0].value == 100
        assert node.children[1].value == 200

    def test_at_with_rotation(self):
        """at() with rotation includes rotation value."""
        node = at(100, 200, 90)
        assert node.name == "at"
        assert len(node.children) == 3
        assert node.children[0].value == 100
        assert node.children[1].value == 200
        assert node.children[2].value == 90

    def test_at_zero_rotation_omitted(self):
        """Zero rotation should be omitted."""
        node = at(100, 200, 0)
        assert len(node.children) == 2


class TestStrokeBuilder:
    """Tests for the stroke() builder."""

    def test_stroke_defaults(self):
        """stroke() with default values."""
        node = stroke()
        assert node.name == "stroke"
        # Find width child
        width_node = node.get("width")
        assert width_node is not None
        assert width_node.children[0].value == 0
        # Find type child
        type_node = node.get("type")
        assert type_node is not None
        assert type_node.children[0].value == "solid"

    def test_stroke_custom_values(self):
        """stroke() with custom width and type."""
        node = stroke(width=0.25, stroke_type="dash")
        width_node = node.get("width")
        assert width_node.children[0].value == 0.25
        type_node = node.get("type")
        assert type_node.children[0].value == "dash"


class TestFontBuilder:
    """Tests for the font() builder."""

    def test_font_default_size(self):
        """font() with default size."""
        node = font()
        assert node.name == "font"
        size_node = node.get("size")
        assert size_node is not None
        assert size_node.children[0].value == 1.27
        assert size_node.children[1].value == 1.27

    def test_font_custom_size(self):
        """font() with custom size."""
        node = font(size=2.0)
        size_node = node.get("size")
        assert size_node.children[0].value == 2.0


class TestEffectsBuilder:
    """Tests for the effects() builder."""

    def test_effects_default(self):
        """effects() with defaults."""
        node = effects()
        assert node.name == "effects"
        font_node = node.get("font")
        assert font_node is not None

    def test_effects_with_justify(self):
        """effects() with single justify value."""
        node = effects(justify="left")
        justify_node = node.get("justify")
        assert justify_node is not None
        assert justify_node.children[0].value == "left"

    def test_effects_with_multi_justify(self):
        """effects() with multi-word justify."""
        node = effects(justify="left bottom")
        justify_node = node.get("justify")
        assert justify_node is not None
        assert len(justify_node.children) == 2

    def test_effects_with_hide(self):
        """effects() with hide=True."""
        node = effects(hide=True)
        hide_node = node.get("hide")
        assert hide_node is not None
        assert hide_node.children[0].value == "yes"

    def test_effects_without_hide(self):
        """effects() without hide doesn't add hide node."""
        node = effects(hide=False)
        hide_node = node.get("hide")
        assert hide_node is None


class TestUuidNode:
    """Tests for the uuid_node() builder."""

    def test_uuid_node(self):
        """Build uuid node."""
        node = uuid_node("test-uuid-123")
        assert node.name == "uuid"
        assert node.children[0].value == "test-uuid-123"


class TestColorBuilder:
    """Tests for the color() builder."""

    def test_color_default(self):
        """color() with default RGBA."""
        node = color()
        assert node.name == "color"
        assert len(node.children) == 4
        assert all(c.value == 0 for c in node.children)

    def test_color_custom(self):
        """color() with custom RGBA."""
        node = color(255, 128, 64, 255)
        assert node.children[0].value == 255
        assert node.children[1].value == 128
        assert node.children[2].value == 64
        assert node.children[3].value == 255


class TestPtsBuilder:
    """Tests for the pts() builder."""

    def test_pts_empty(self):
        """pts() with no points."""
        node = pts()
        assert node.name == "pts"
        assert len(node.children) == 0

    def test_pts_with_points(self):
        """pts() with xy nodes."""
        node = pts(xy(10, 20), xy(30, 40))
        assert node.name == "pts"
        assert len(node.children) == 2
        assert node.children[0].name == "xy"
        assert node.children[1].name == "xy"


class TestWireNode:
    """Tests for the wire_node() builder."""

    def test_wire_node(self):
        """Build complete wire node."""
        node = wire_node(10, 20, 30, 40, "wire-uuid")
        assert node.name == "wire"

        pts_node = node.get("pts")
        assert pts_node is not None
        assert len(pts_node.children) == 2

        stroke_node = node.get("stroke")
        assert stroke_node is not None

        uuid_n = node.get("uuid")
        assert uuid_n is not None
        assert uuid_n.children[0].value == "wire-uuid"


class TestJunctionNode:
    """Tests for the junction_node() builder."""

    def test_junction_node(self):
        """Build complete junction node."""
        node = junction_node(50, 60, "junc-uuid")
        assert node.name == "junction"

        at_node = node.get("at")
        assert at_node is not None

        diameter_node = node.get("diameter")
        assert diameter_node is not None

        color_node = node.get("color")
        assert color_node is not None

        uuid_n = node.get("uuid")
        assert uuid_n.children[0].value == "junc-uuid"


class TestLabelNode:
    """Tests for the label_node() builder."""

    def test_label_node(self):
        """Build complete label node."""
        node = label_node("GND", 100, 200, 0, "label-uuid")
        assert node.name == "label"
        assert node.children[0].value == "GND"

        at_node = node.get("at")
        assert at_node is not None

        effects_node = node.get("effects")
        assert effects_node is not None


class TestHierLabelNode:
    """Tests for the hier_label_node() builder."""

    def test_hier_label_node_left(self):
        """Build hier label with left justify."""
        node = hier_label_node("MCLK", 100, 200, "output", 0, "hl-uuid")
        assert node.name == "hierarchical_label"
        assert node.children[0].value == "MCLK"

        shape_node = node.get("shape")
        assert shape_node.children[0].value == "output"

    def test_hier_label_node_right_justify(self):
        """Rotation 180 gets right justify."""
        node = hier_label_node("MCLK", 100, 200, "output", 180, "hl-uuid")
        effects_node = node.get("effects")
        justify_node = effects_node.get("justify")
        assert justify_node.children[0].value == "right"


class TestTextNode:
    """Tests for the text_node() builder."""

    def test_text_node(self):
        """Build complete text node."""
        node = text_node("Test note", 100, 200, "text-uuid")
        assert node.name == "text"
        assert node.children[0].value == "Test note"

        exclude_node = node.get("exclude_from_sim")
        assert exclude_node is not None


class TestSymbolPropertyNode:
    """Tests for the symbol_property_node() builder."""

    def test_symbol_property_node(self):
        """Build symbol property node."""
        node = symbol_property_node("Reference", "U1", 100, 200)
        assert node.name == "property"
        assert node.children[0].value == "Reference"
        assert node.children[1].value == "U1"

    def test_symbol_property_node_hidden(self):
        """Build hidden symbol property node."""
        node = symbol_property_node("Footprint", "Package:SOIC-8", 100, 200, hide=True)
        effects_node = node.get("effects")
        hide_node = effects_node.get("hide")
        assert hide_node is not None


class TestPinUuidNode:
    """Tests for the pin_uuid_node() builder."""

    def test_pin_uuid_node(self):
        """Build pin uuid mapping node."""
        node = pin_uuid_node("1", "pin-uuid-123")
        assert node.name == "pin"
        assert node.children[0].value == "1"
        uuid_n = node.get("uuid")
        assert uuid_n.children[0].value == "pin-uuid-123"


class TestSymbolInstancesNode:
    """Tests for the symbol_instances_node() builder."""

    def test_symbol_instances_node(self):
        """Build symbol instances node."""
        node = symbol_instances_node("MyProject", "/", "U1", 1)
        assert node.name == "instances"

        project_node = node.get("project")
        assert project_node.children[0].value == "MyProject"

        path_node = project_node.get("path")
        assert path_node is not None


class TestTitleBlock:
    """Tests for the title_block() builder."""

    def test_title_block(self):
        """Build title block."""
        node = title_block("My Schematic", "2025-01", "A")
        assert node.name == "title_block"

        title_node = node.get("title")
        assert title_node.children[0].value == "My Schematic"

        date_node = node.get("date")
        assert date_node.children[0].value == "2025-01"

        rev_node = node.get("rev")
        assert rev_node.children[0].value == "A"

    def test_title_block_with_company(self):
        """Build title block with company."""
        node = title_block("My Schematic", "2025-01", "A", company="ACME Corp")
        company_node = node.get("company")
        assert company_node.children[0].value == "ACME Corp"


class TestSheetInstances:
    """Tests for the sheet_instances() builder."""

    def test_sheet_instances(self):
        """Build sheet instances node."""
        node = sheet_instances("/", "1")
        assert node.name == "sheet_instances"

        path_node = node.get("path")
        assert path_node is not None


class TestSegmentNode:
    """Tests for the segment_node() PCB builder."""

    def test_segment_node(self):
        """Build PCB track segment."""
        node = segment_node(100, 200, 110, 200, 0.25, "F.Cu", 5, "seg-uuid")
        assert node.name == "segment"

        start_node = node.get("start")
        assert start_node.children[0].value == 100
        assert start_node.children[1].value == 200

        end_node = node.get("end")
        assert end_node.children[0].value == 110
        assert end_node.children[1].value == 200

        width_node = node.get("width")
        assert width_node.children[0].value == 0.25

        layer_node = node.get("layer")
        assert layer_node.children[0].value == "F.Cu"

        net_node = node.get("net")
        assert net_node.children[0].value == 5


class TestViaNode:
    """Tests for the via_node() PCB builder."""

    def test_via_node(self):
        """Build PCB via."""
        node = via_node(150, 100, 0.6, 0.3, ("F.Cu", "B.Cu"), 3, "via-uuid")
        assert node.name == "via"

        at_node = node.get("at")
        assert at_node is not None

        size_node = node.get("size")
        assert size_node.children[0].value == 0.6

        drill_node = node.get("drill")
        assert drill_node.children[0].value == 0.3

        layers_node = node.get("layers")
        assert len(layers_node.children) == 2


class TestZoneNode:
    """Tests for the zone_node() PCB builder."""

    def test_zone_node_basic(self):
        """Build basic PCB zone."""
        points = [(0, 0), (100, 0), (100, 50), (0, 50)]
        node = zone_node(1, "GND", "F.Cu", points, "zone-uuid")
        assert node.name == "zone"

        net_node = node.get("net")
        assert net_node.children[0].value == 1

        net_name_node = node.get("net_name")
        assert net_name_node.children[0].value == "GND"

        polygon_node = node.get("polygon")
        assert polygon_node is not None

    def test_zone_node_with_priority(self):
        """Build PCB zone with priority."""
        points = [(0, 0), (100, 0), (100, 50), (0, 50)]
        node = zone_node(2, "VCC", "F.Cu", points, "zone-uuid", priority=1)
        priority_node = node.get("priority")
        assert priority_node.children[0].value == 1


class TestFootprintAtNode:
    """Tests for the footprint_at_node() builder."""

    def test_footprint_at_no_rotation(self):
        """Footprint at without rotation."""
        node = footprint_at_node(100, 200)
        assert node.name == "at"
        assert len(node.children) == 2

    def test_footprint_at_with_rotation(self):
        """Footprint at with rotation."""
        node = footprint_at_node(100, 200, 45)
        assert node.name == "at"
        assert len(node.children) == 3
        assert node.children[2].value == 45
