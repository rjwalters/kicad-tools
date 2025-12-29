"""Tests for PCB modules (blocks, editor)."""

import pytest
from pathlib import Path

# =============================================================================
# PCB Blocks Tests
# =============================================================================

from kicad_tools.pcb.blocks import (
    Point, Rectangle, Layer, Pad, Port, TraceSegment, Via,
    ComponentPlacement, PCBBlock, MCUBlock, LDOBlock, OscillatorBlock,
    LEDBlock, PCBLayout, KiCadPCBExporter, get_footprint_pads, FOOTPRINT_PADS
)


class TestPoint:
    """Tests for Point class."""

    def test_point_creation(self):
        """Test creating a point."""
        p = Point(10.0, 20.0)
        assert p.x == 10.0
        assert p.y == 20.0

    def test_point_addition(self):
        """Test point addition."""
        p1 = Point(10.0, 20.0)
        p2 = Point(5.0, 3.0)
        result = p1 + p2
        assert result.x == 15.0
        assert result.y == 23.0

    def test_point_subtraction(self):
        """Test point subtraction."""
        p1 = Point(10.0, 20.0)
        p2 = Point(5.0, 3.0)
        result = p1 - p2
        assert result.x == 5.0
        assert result.y == 17.0

    def test_point_rotate_zero(self):
        """Test point rotation by zero degrees."""
        p = Point(10.0, 0.0)
        rotated = p.rotate(0)
        assert rotated.x == pytest.approx(10.0)
        assert rotated.y == pytest.approx(0.0)

    def test_point_rotate_90(self):
        """Test point rotation by 90 degrees."""
        p = Point(10.0, 0.0)
        rotated = p.rotate(90)
        assert rotated.x == pytest.approx(0.0, abs=0.01)
        assert rotated.y == pytest.approx(10.0, abs=0.01)

    def test_point_rotate_around_origin(self):
        """Test point rotation around a custom origin."""
        p = Point(10.0, 0.0)
        origin = Point(5.0, 0.0)
        rotated = p.rotate(90, origin)
        assert rotated.x == pytest.approx(5.0, abs=0.01)
        assert rotated.y == pytest.approx(5.0, abs=0.01)

    def test_point_tuple(self):
        """Test point tuple conversion."""
        p = Point(10.0, 20.0)
        assert p.tuple() == (10.0, 20.0)


class TestRectangle:
    """Tests for Rectangle class."""

    def test_rectangle_creation(self):
        """Test creating a rectangle."""
        r = Rectangle(0, 0, 10, 20)
        assert r.min_x == 0
        assert r.min_y == 0
        assert r.max_x == 10
        assert r.max_y == 20

    def test_rectangle_width(self):
        """Test rectangle width property."""
        r = Rectangle(0, 0, 10, 20)
        assert r.width == 10

    def test_rectangle_height(self):
        """Test rectangle height property."""
        r = Rectangle(0, 0, 10, 20)
        assert r.height == 20

    def test_rectangle_center(self):
        """Test rectangle center property."""
        r = Rectangle(0, 0, 10, 20)
        center = r.center
        assert center.x == 5.0
        assert center.y == 10.0

    def test_rectangle_contains(self):
        """Test point containment."""
        r = Rectangle(0, 0, 10, 20)
        assert r.contains(Point(5, 10)) is True
        assert r.contains(Point(0, 0)) is True
        assert r.contains(Point(11, 10)) is False

    def test_rectangle_expand(self):
        """Test rectangle expansion."""
        r = Rectangle(0, 0, 10, 20)
        expanded = r.expand(2)
        assert expanded.min_x == -2
        assert expanded.min_y == -2
        assert expanded.max_x == 12
        assert expanded.max_y == 22


class TestLayer:
    """Tests for Layer enum."""

    def test_layer_values(self):
        """Test layer values."""
        assert Layer.F_CU.value == "F.Cu"
        assert Layer.B_CU.value == "B.Cu"
        assert Layer.EDGE.value == "Edge.Cuts"


class TestPad:
    """Tests for Pad dataclass."""

    def test_pad_creation(self):
        """Test creating a pad."""
        pad = Pad(name="1", position=Point(0, 0))
        assert pad.name == "1"
        assert pad.position.x == 0
        assert pad.layer == Layer.F_CU  # Default

    def test_pad_with_options(self):
        """Test pad with custom options."""
        pad = Pad(
            name="VDD",
            position=Point(1, 2),
            layer=Layer.B_CU,
            net="VCC",
            shape="rect",
            size=(1.0, 0.5),
            drill=0.3
        )
        assert pad.net == "VCC"
        assert pad.shape == "rect"
        assert pad.drill == 0.3


class TestPort:
    """Tests for Port dataclass."""

    def test_port_creation(self):
        """Test creating a port."""
        port = Port(name="VDD", position=Point(5, 10))
        assert port.name == "VDD"
        assert port.direction == "inout"  # Default

    def test_port_with_options(self):
        """Test port with custom options."""
        port = Port(
            name="GND",
            position=Point(0, 0),
            direction="power",
            internal_pad="U1.VSS"
        )
        assert port.direction == "power"
        assert port.internal_pad == "U1.VSS"


class TestTraceSegment:
    """Tests for TraceSegment dataclass."""

    def test_trace_segment_creation(self):
        """Test creating a trace segment."""
        trace = TraceSegment(
            start=Point(0, 0),
            end=Point(10, 0),
            width=0.25
        )
        assert trace.start.x == 0
        assert trace.end.x == 10
        assert trace.width == 0.25


class TestVia:
    """Tests for Via dataclass."""

    def test_via_creation(self):
        """Test creating a via."""
        via = Via(position=Point(5, 5))
        assert via.position.x == 5
        assert via.drill == 0.3  # Default
        assert via.size == 0.6  # Default


class TestComponentPlacement:
    """Tests for ComponentPlacement dataclass."""

    def test_component_placement_creation(self):
        """Test creating component placement."""
        comp = ComponentPlacement(
            ref="U1",
            footprint="Package_SO:TSSOP-20",
            position=Point(10, 20),
            pads={"1": Point(-2, 0), "2": Point(2, 0)}
        )
        assert comp.ref == "U1"
        assert comp.rotation == 0  # Default

    def test_component_pad_position(self):
        """Test getting pad position."""
        comp = ComponentPlacement(
            ref="U1",
            footprint="Package_SO:TSSOP-20",
            position=Point(10, 20),
            pads={"1": Point(-2, 0), "2": Point(2, 0)}
        )
        pad_pos = comp.pad_position("1")
        assert pad_pos.x == 8  # 10 + (-2)
        assert pad_pos.y == 20

    def test_component_pad_position_with_rotation(self):
        """Test pad position with component rotation."""
        comp = ComponentPlacement(
            ref="U1",
            footprint="Test",
            position=Point(10, 20),
            rotation=90,
            pads={"1": Point(5, 0)}
        )
        pad_pos = comp.pad_position("1")
        # 90 degree rotation of (5, 0) around origin = (0, 5)
        # Then add component position (10, 20)
        assert pad_pos.x == pytest.approx(10.0, abs=0.01)
        assert pad_pos.y == pytest.approx(25.0, abs=0.01)

    def test_component_pad_position_not_found(self):
        """Test pad position with invalid pad name."""
        comp = ComponentPlacement(
            ref="U1",
            footprint="Test",
            position=Point(0, 0),
            pads={"1": Point(0, 0)}
        )
        with pytest.raises(KeyError, match="Pad '99'"):
            comp.pad_position("99")


class TestPCBBlock:
    """Tests for PCBBlock class."""

    def test_block_creation(self):
        """Test creating a PCB block."""
        block = PCBBlock("test_block")
        assert block.name == "test_block"
        assert block.placed is False
        assert len(block.components) == 0

    def test_add_component(self):
        """Test adding a component to block."""
        block = PCBBlock("test")
        comp = block.add_component(
            "U1",
            "Package_SO:TSSOP-20",
            x=0, y=0,
            pads={"1": (-1, 0), "2": (1, 0)}
        )
        assert "U1" in block.components
        assert comp.ref == "U1"

    def test_add_trace(self):
        """Test adding a trace to block."""
        block = PCBBlock("test")
        trace = block.add_trace((0, 0), (10, 0), width=0.3)
        assert len(block.traces) == 1
        assert trace.width == 0.3

    def test_add_trace_with_point(self):
        """Test adding trace with Point objects."""
        block = PCBBlock("test")
        trace = block.add_trace(Point(0, 0), Point(10, 0))
        assert trace.start.x == 0
        assert trace.end.x == 10

    def test_add_via(self):
        """Test adding a via to block."""
        block = PCBBlock("test")
        via = block.add_via(5, 5, net="GND")
        assert len(block.vias) == 1
        assert via.net == "GND"

    def test_add_port(self):
        """Test adding a port to block."""
        block = PCBBlock("test")
        port = block.add_port("VDD", 10, 0, direction="power")
        assert "VDD" in block.ports
        assert port.direction == "power"

    def test_place_block(self):
        """Test placing a block."""
        block = PCBBlock("test")
        block.place(100, 50, rotation=45)
        assert block.placed is True
        assert block.origin.x == 100
        assert block.origin.y == 50
        assert block.rotation == 45

    def test_port_position(self):
        """Test getting port position after placement."""
        block = PCBBlock("test")
        block.add_port("OUT", 10, 0)
        block.place(100, 50)
        pos = block.port("OUT")
        assert pos.x == 110  # 100 + 10
        assert pos.y == 50

    def test_port_position_not_found(self):
        """Test getting non-existent port."""
        block = PCBBlock("test")
        block.add_port("VDD", 0, 0)
        with pytest.raises(KeyError, match="Port 'MISSING'"):
            block.port("MISSING")

    def test_component_position(self):
        """Test getting component position after placement."""
        block = PCBBlock("test")
        block.add_component("U1", "Test", x=10, y=5)
        block.place(100, 50)
        pos = block.component_position("U1")
        assert pos.x == 110
        assert pos.y == 55

    def test_bounding_box(self):
        """Test bounding box calculation."""
        block = PCBBlock("test")
        block.add_component("U1", "Test", x=0, y=0)
        block.add_component("U2", "Test", x=10, y=20)
        bbox = block.bounding_box
        assert bbox.min_x == -2  # 0 - 2 margin
        assert bbox.max_x == 12  # 10 + 2 margin

    def test_bounding_box_empty(self):
        """Test bounding box with no components."""
        block = PCBBlock("test")
        bbox = block.bounding_box
        assert bbox.width == 0

    def test_get_placed_components(self):
        """Test exporting placed components."""
        block = PCBBlock("test")
        block.add_component("U1", "Test:FP", x=10, y=5)
        block.place(100, 50, rotation=90)
        components = block.get_placed_components()
        assert len(components) == 1
        assert components[0]["ref"] == "U1"

    def test_get_placed_traces(self):
        """Test exporting placed traces."""
        block = PCBBlock("test")
        block.add_trace((0, 0), (10, 0))
        block.place(100, 50)
        traces = block.get_placed_traces()
        assert len(traces) == 1
        assert traces[0]["start"] == (100, 50)
        assert traces[0]["end"] == (110, 50)

    def test_repr(self):
        """Test block string representation."""
        block = PCBBlock("my_block")
        block.add_component("U1", "Test", 0, 0)
        block.add_port("VDD", 0, 0)
        s = repr(block)
        assert "my_block" in s
        assert "1 components" in s
        assert "1 ports" in s

    def test_route_to_port(self):
        """Test routing from internal pad to port."""
        block = PCBBlock("test")
        block.add_component("U1", "Test", x=0, y=0, pads={"1": (0, 0)})
        block.add_port("OUT", 10, 0)
        block.route_to_port("U1.1", "OUT", width=0.3)
        assert len(block.traces) == 1

    def test_route_to_port_invalid_component(self):
        """Test routing with invalid component."""
        block = PCBBlock("test")
        block.add_port("OUT", 0, 0)
        with pytest.raises(KeyError, match="Component 'U1'"):
            block.route_to_port("U1.1", "OUT")

    def test_route_to_port_invalid_port(self):
        """Test routing with invalid port."""
        block = PCBBlock("test")
        block.add_component("U1", "Test", x=0, y=0, pads={"1": (0, 0)})
        with pytest.raises(KeyError, match="Port 'MISSING'"):
            block.route_to_port("U1.1", "MISSING")


class TestGetFootprintPads:
    """Tests for get_footprint_pads function."""

    def test_known_footprint(self):
        """Test getting pads for known footprint."""
        pads = get_footprint_pads("Capacitor_SMD:C_0603_1608Metric")
        assert "1" in pads
        assert "2" in pads

    def test_unknown_footprint_returns_default(self):
        """Test unknown footprint returns default pads."""
        pads = get_footprint_pads("Unknown:Footprint")
        assert "1" in pads
        assert "2" in pads


class TestMCUBlock:
    """Tests for MCUBlock class."""

    def test_mcu_block_creation(self):
        """Test creating MCU block."""
        mcu = MCUBlock(mcu_ref="U3", bypass_caps=["C10", "C11"])
        assert mcu.name == "MCU_U3"
        assert "U3" in mcu.components
        assert "C10" in mcu.components
        assert "C11" in mcu.components

    def test_mcu_block_default_caps(self):
        """Test MCU block with default bypass caps."""
        mcu = MCUBlock()
        assert "C1" in mcu.components
        assert "C2" in mcu.components

    def test_mcu_block_has_ports(self):
        """Test MCU block has power ports."""
        mcu = MCUBlock()
        assert "VDD" in mcu.ports
        assert "GND" in mcu.ports


class TestLDOBlock:
    """Tests for LDOBlock class."""

    def test_ldo_block_creation(self):
        """Test creating LDO block."""
        ldo = LDOBlock(ldo_ref="U2", input_cap="C5", output_caps=["C6", "C7"])
        assert ldo.name == "LDO_U2"
        assert "U2" in ldo.components
        assert "C5" in ldo.components
        assert "C6" in ldo.components

    def test_ldo_block_has_ports(self):
        """Test LDO block has power ports."""
        ldo = LDOBlock()
        assert "VIN" in ldo.ports
        assert "VOUT" in ldo.ports
        assert "GND" in ldo.ports
        assert "EN" in ldo.ports


class TestOscillatorBlock:
    """Tests for OscillatorBlock class."""

    def test_oscillator_block_creation(self):
        """Test creating oscillator block."""
        osc = OscillatorBlock(osc_ref="Y1", cap_ref="C8")
        assert osc.name == "OSC_Y1"
        assert "Y1" in osc.components
        assert "C8" in osc.components

    def test_oscillator_block_has_ports(self):
        """Test oscillator block has ports."""
        osc = OscillatorBlock()
        assert "VDD" in osc.ports
        assert "GND" in osc.ports
        assert "OUT" in osc.ports
        assert "EN" in osc.ports


class TestLEDBlock:
    """Tests for LEDBlock class."""

    def test_led_block_creation(self):
        """Test creating LED block."""
        led = LEDBlock(led_ref="D1", res_ref="R1")
        assert led.name == "LED_D1"
        assert "D1" in led.components
        assert "R1" in led.components

    def test_led_block_has_ports(self):
        """Test LED block has ports."""
        led = LEDBlock()
        assert "ANODE" in led.ports
        assert "CATHODE" in led.ports


class TestPCBLayout:
    """Tests for PCBLayout class."""

    def test_layout_creation(self):
        """Test creating a layout."""
        layout = PCBLayout("test_board")
        assert layout.name == "test_board"
        assert len(layout.blocks) == 0

    def test_add_block(self):
        """Test adding a block to layout."""
        layout = PCBLayout("test")
        block = PCBBlock("my_block")
        layout.add_block(block, "block1")
        assert "block1" in layout.blocks

    def test_add_block_default_name(self):
        """Test adding block with default name."""
        layout = PCBLayout("test")
        block = PCBBlock("my_block")
        layout.add_block(block)
        assert "my_block" in layout.blocks

    def test_route_between_blocks(self):
        """Test routing between blocks."""
        layout = PCBLayout("test")

        block1 = PCBBlock("B1")
        block1.add_port("OUT", 10, 0)
        block1.place(0, 0)
        layout.add_block(block1, "B1")

        block2 = PCBBlock("B2")
        block2.add_port("IN", 0, 0)
        block2.place(50, 0)
        layout.add_block(block2, "B2")

        layout.route("B1", "OUT", "B2", "IN", net="SIG1")
        assert len(layout.inter_block_traces) == 1

    def test_export_placements(self):
        """Test exporting all placements."""
        layout = PCBLayout("test")
        block = PCBBlock("B1")
        block.add_component("U1", "Test", 0, 0)
        block.place(10, 10)
        layout.add_block(block)

        placements = layout.export_placements()
        assert len(placements) == 1
        assert placements[0]["ref"] == "U1"

    def test_export_traces(self):
        """Test exporting all traces."""
        layout = PCBLayout("test")
        block = PCBBlock("B1")
        block.add_trace((0, 0), (5, 0))
        block.place(10, 10)
        layout.add_block(block)

        traces = layout.export_traces()
        assert len(traces) == 1

    def test_summary(self):
        """Test layout summary generation."""
        layout = PCBLayout("test_board")
        block = PCBBlock("B1")
        block.add_component("U1", "Test", 0, 0)
        block.add_port("VDD", 0, 0)
        block.place(10, 10)
        layout.add_block(block)

        summary = layout.summary()
        assert "test_board" in summary
        assert "B1" in summary


class TestKiCadPCBExporter:
    """Tests for KiCadPCBExporter class."""

    def test_exporter_creation(self):
        """Test creating exporter."""
        layout = PCBLayout("test")
        exporter = KiCadPCBExporter(layout)
        assert exporter.layout is layout

    def test_get_net_number(self):
        """Test net number assignment."""
        layout = PCBLayout("test")
        exporter = KiCadPCBExporter(layout)

        num1 = exporter._get_net_number("GND")
        num2 = exporter._get_net_number("VCC")
        num3 = exporter._get_net_number("GND")  # Same net

        assert num1 == 1
        assert num2 == 2
        assert num3 == 1  # Should return same number

    def test_get_net_number_none(self):
        """Test net number for None."""
        layout = PCBLayout("test")
        exporter = KiCadPCBExporter(layout)
        assert exporter._get_net_number(None) == 0

    def test_generate_basic(self):
        """Test generating PCB content."""
        layout = PCBLayout("test")
        block = PCBBlock("B1")
        block.add_component("U1", "Test:FP", 0, 0)
        block.place(10, 10)
        layout.add_block(block)

        exporter = KiCadPCBExporter(layout)
        content = exporter.generate()

        assert "(kicad_pcb" in content
        assert "U1" in content

    def test_write(self, tmp_path):
        """Test writing PCB file."""
        layout = PCBLayout("test")
        exporter = KiCadPCBExporter(layout)

        output_path = tmp_path / "test.kicad_pcb"
        exporter.write(str(output_path))

        assert output_path.exists()
        content = output_path.read_text()
        assert "(kicad_pcb" in content


# =============================================================================
# PCB Editor Tests
# =============================================================================

from kicad_tools.pcb.editor import (
    PCBEditor, Point as EditorPoint, Track, Via as EditorVia, Zone,
    SeeedFusion4Layer, AudioLayoutRules
)


class TestEditorPoint:
    """Tests for editor Point class."""

    def test_point_creation(self):
        """Test creating a point."""
        p = EditorPoint(10.0, 20.0)
        assert p.x == 10.0
        assert p.y == 20.0

    def test_point_iter(self):
        """Test point iteration."""
        p = EditorPoint(10.0, 20.0)
        assert list(p) == [10.0, 20.0]


class TestTrack:
    """Tests for Track class."""

    def test_track_creation(self):
        """Test creating a track."""
        track = Track(
            net=1,
            start=EditorPoint(0, 0),
            end=EditorPoint(10, 0),
            width=0.2,
            layer="F.Cu"
        )
        assert track.net == 1
        assert track.width == 0.2

    def test_track_to_sexp_node(self):
        """Test track S-expression generation."""
        track = Track(
            net=1,
            start=EditorPoint(0, 0),
            end=EditorPoint(10, 0),
            width=0.2,
            layer="F.Cu"
        )
        node = track.to_sexp_node()
        assert node is not None


class TestEditorVia:
    """Tests for editor Via class."""

    def test_via_creation(self):
        """Test creating a via."""
        via = EditorVia(
            net=1,
            position=EditorPoint(5, 5),
            size=0.6,
            drill=0.3
        )
        assert via.size == 0.6
        assert via.drill == 0.3

    def test_via_to_sexp_node(self):
        """Test via S-expression generation."""
        via = EditorVia(
            net=1,
            position=EditorPoint(5, 5),
            size=0.6,
            drill=0.3
        )
        node = via.to_sexp_node()
        assert node is not None


class TestZone:
    """Tests for Zone class."""

    def test_zone_creation(self):
        """Test creating a zone."""
        zone = Zone(
            net=1,
            net_name="GND",
            layer="F.Cu",
            points=[EditorPoint(0, 0), EditorPoint(10, 0), EditorPoint(10, 10), EditorPoint(0, 10)]
        )
        assert zone.net == 1
        assert len(zone.points) == 4

    def test_zone_to_sexp_node(self):
        """Test zone S-expression generation."""
        zone = Zone(
            net=1,
            net_name="GND",
            layer="F.Cu",
            points=[EditorPoint(0, 0), EditorPoint(10, 0), EditorPoint(10, 10), EditorPoint(0, 10)]
        )
        node = zone.to_sexp_node()
        assert node is not None


class TestPCBEditor:
    """Tests for PCBEditor class."""

    def test_editor_creation_new_file(self, tmp_path):
        """Test creating editor for non-existent file."""
        pcb_path = tmp_path / "new.kicad_pcb"
        editor = PCBEditor(str(pcb_path))
        assert editor.doc is None

    def test_editor_load_existing(self, minimal_pcb):
        """Test loading existing PCB file."""
        editor = PCBEditor(str(minimal_pcb))
        assert editor.doc is not None
        assert len(editor.nets) > 0

    def test_get_net_number(self, minimal_pcb):
        """Test getting net number."""
        editor = PCBEditor(str(minimal_pcb))
        gnd_num = editor.get_net_number("GND")
        assert gnd_num == 1

    def test_get_net_number_unknown(self, minimal_pcb):
        """Test getting net number for unknown net."""
        editor = PCBEditor(str(minimal_pcb))
        num = editor.get_net_number("UNKNOWN_NET")
        assert num == 0

    def test_place_component(self, minimal_pcb):
        """Test placing a component."""
        editor = PCBEditor(str(minimal_pcb))
        result = editor.place_component("R1", 50, 50, rotation=90)
        assert result is True
        assert editor.footprints["R1"]["x"] == 50
        assert editor.footprints["R1"]["y"] == 50

    def test_place_component_not_found(self, minimal_pcb):
        """Test placing non-existent component."""
        editor = PCBEditor(str(minimal_pcb))
        result = editor.place_component("NONEXISTENT", 0, 0)
        assert result is False

    def test_add_track(self, minimal_pcb):
        """Test adding a track."""
        editor = PCBEditor(str(minimal_pcb))
        tracks = editor.add_track("GND", [(0, 0), (10, 0), (10, 10)], width=0.3)
        assert len(tracks) == 2  # Two segments

    def test_add_via(self, minimal_pcb):
        """Test adding a via."""
        editor = PCBEditor(str(minimal_pcb))
        via = editor.add_via((5, 5), "GND", drill=0.4, size=0.8)
        assert via.drill == 0.4
        assert via.size == 0.8

    def test_add_zone(self, minimal_pcb):
        """Test adding a zone."""
        editor = PCBEditor(str(minimal_pcb))
        boundary = [(0, 0), (10, 0), (10, 10), (0, 10)]
        zone = editor.add_zone("GND", "F.Cu", boundary, priority=1)
        assert zone.net_name == "GND"
        assert zone.priority == 1

    def test_create_ground_pour(self, minimal_pcb):
        """Test creating ground pour."""
        editor = PCBEditor(str(minimal_pcb))
        zone = editor.create_ground_pour(layer="F.Cu")
        assert zone.net_name == "GND"

    def test_create_ground_pour_custom_boundary(self, minimal_pcb):
        """Test creating ground pour with custom boundary."""
        editor = PCBEditor(str(minimal_pcb))
        boundary = [(0, 0), (50, 0), (50, 50), (0, 50)]
        zone = editor.create_ground_pour(layer="F.Cu", boundary=boundary)
        assert len(zone.points) == 4

    def test_generate_routing_script(self, minimal_pcb):
        """Test generating routing script."""
        editor = PCBEditor(str(minimal_pcb))
        connections = [
            {"net": "GND", "from": (0, 0), "to": (10, 0), "width": 0.2, "layer": "F.Cu"},
            {"net": "GND", "from": (10, 0), "to": (10, 10), "width": 0.2, "layer": "F.Cu", "via": True},
        ]
        script = editor.generate_routing_script(connections)
        assert "pcbnew" in script
        assert "add_track" in script
        assert "add_via" in script

    def test_validate_placement(self, minimal_pcb):
        """Test placement validation."""
        editor = PCBEditor(str(minimal_pcb))
        issues = editor.validate_placement()
        assert isinstance(issues, list)

    def test_save(self, minimal_pcb, tmp_path):
        """Test saving PCB file."""
        editor = PCBEditor(str(minimal_pcb))
        output_path = tmp_path / "output.kicad_pcb"
        editor.save(str(output_path))
        assert output_path.exists()


class TestSeeedFusion4Layer:
    """Tests for SeeedFusion4Layer design rules."""

    def test_constants(self):
        """Test design rule constants."""
        assert SeeedFusion4Layer.MIN_TRACE_WIDTH == 0.1
        assert SeeedFusion4Layer.MIN_VIA_DRILL == 0.2
        assert SeeedFusion4Layer.RECOMMENDED_TRACE == 0.15

    def test_power_trace_width(self):
        """Test power trace width calculation."""
        # 100mA should need ~0.1mm trace
        width = SeeedFusion4Layer.power_trace_width(100)
        assert width >= SeeedFusion4Layer.MIN_TRACE_WIDTH

        # Higher current needs wider trace
        width_high = SeeedFusion4Layer.power_trace_width(1000)
        assert width_high > width


class TestAudioLayoutRules:
    """Tests for AudioLayoutRules helpers."""

    def test_analog_ground_zone(self):
        """Test analog ground zone calculation."""
        zone = AudioLayoutRules.analog_ground_zone(100, 30)
        assert len(zone) == 4
        assert zone[0] == (0, 0)
        assert zone[1] == (30, 0)

    def test_star_ground_point(self):
        """Test star ground point."""
        center = (50, 25)
        point = AudioLayoutRules.star_ground_point(center)
        assert point == center

    def test_clock_trace_length_match(self):
        """Test clock trace length matching."""
        source = (0, 0)
        dest1 = (10, 0)  # 10mm
        dest2 = (20, 0)  # 20mm

        result = AudioLayoutRules.clock_trace_length_match(source, dest1, dest2)
        assert result["length_diff_mm"] == pytest.approx(10.0)
        assert result["shorter_path"] == "dest1"
        assert result["serpentine_needed"] is True
