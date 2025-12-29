"""Tests for schema modules (wire, label, bom, schematic, library, hierarchy)."""

import pytest
from pathlib import Path

from kicad_tools.core.sexp import parse_sexp
from kicad_tools.schema.wire import Wire, Junction, Bus
from kicad_tools.schema.label import Label, HierarchicalLabel, GlobalLabel, PowerSymbol
from kicad_tools.schema.bom import BOMItem, BOMGroup, BOM
from kicad_tools.schema.schematic import Schematic, TitleBlock, SheetInstance
from kicad_tools.schema.library import LibraryPin, LibrarySymbol, SymbolLibrary, LibraryManager
from kicad_tools.schema.hierarchy import (
    SheetPin, SheetInstance as HierarchySheetInstance, HierarchyNode, HierarchyBuilder
)


class TestWire:
    """Tests for Wire class."""

    def test_wire_from_sexp(self):
        """Test parsing wire from S-expression."""
        sexp = parse_sexp("""(wire
            (pts (xy 90 100) (xy 110 100))
            (stroke (width 0.2) (type solid))
            (uuid "test-uuid-123")
        )""")
        wire = Wire.from_sexp(sexp)
        assert wire.start == (90.0, 100.0)
        assert wire.end == (110.0, 100.0)
        assert wire.uuid == "test-uuid-123"
        assert wire.stroke_width == 0.2
        assert wire.stroke_type == "solid"

    def test_wire_from_sexp_minimal(self):
        """Test parsing wire with minimal data."""
        sexp = parse_sexp("(wire)")
        wire = Wire.from_sexp(sexp)
        assert wire.start == (0.0, 0.0)
        assert wire.end == (0.0, 0.0)
        assert wire.uuid == ""

    def test_wire_length_horizontal(self):
        """Test length calculation for horizontal wire."""
        wire = Wire(start=(0, 0), end=(10, 0))
        assert wire.length == 10.0

    def test_wire_length_vertical(self):
        """Test length calculation for vertical wire."""
        wire = Wire(start=(0, 0), end=(0, 5))
        assert wire.length == 5.0

    def test_wire_length_diagonal(self):
        """Test length calculation for diagonal wire (3-4-5 triangle)."""
        wire = Wire(start=(0, 0), end=(3, 4))
        assert wire.length == 5.0

    def test_wire_length_zero(self):
        """Test length calculation for zero-length wire."""
        wire = Wire(start=(5, 5), end=(5, 5))
        assert wire.length == 0.0

    def test_wire_contains_point_on_wire(self):
        """Test point on wire."""
        wire = Wire(start=(0, 0), end=(10, 0))
        assert wire.contains_point((5, 0)) is True

    def test_wire_contains_point_at_start(self):
        """Test point at wire start."""
        wire = Wire(start=(0, 0), end=(10, 0))
        assert wire.contains_point((0, 0)) is True

    def test_wire_contains_point_at_end(self):
        """Test point at wire end."""
        wire = Wire(start=(0, 0), end=(10, 0))
        assert wire.contains_point((10, 0)) is True

    def test_wire_contains_point_off_wire(self):
        """Test point not on wire."""
        wire = Wire(start=(0, 0), end=(10, 0))
        assert wire.contains_point((5, 5)) is False

    def test_wire_contains_point_beyond_end(self):
        """Test point beyond wire endpoints."""
        wire = Wire(start=(0, 0), end=(10, 0))
        assert wire.contains_point((15, 0)) is False

    def test_wire_contains_point_near_wire(self):
        """Test point near wire within tolerance."""
        wire = Wire(start=(0, 0), end=(10, 0))
        # Point is 0.05 away, within default 0.1 tolerance
        assert wire.contains_point((5, 0.05)) is True

    def test_wire_contains_point_zero_length(self):
        """Test contains_point on zero-length wire."""
        wire = Wire(start=(5, 5), end=(5, 5))
        assert wire.contains_point((5, 5)) is True
        assert wire.contains_point((5.05, 5)) is True  # Within tolerance
        assert wire.contains_point((6, 6)) is False

    def test_wire_repr(self):
        """Test wire string representation."""
        wire = Wire(start=(0, 0), end=(10, 10))
        assert "Wire" in repr(wire)
        assert "(0, 0)" in repr(wire)
        assert "(10, 10)" in repr(wire)


class TestJunction:
    """Tests for Junction class."""

    def test_junction_from_sexp(self):
        """Test parsing junction from S-expression."""
        sexp = parse_sexp("""(junction
            (at 50.8 76.2)
            (diameter 1.0)
            (uuid "junction-uuid")
        )""")
        junc = Junction.from_sexp(sexp)
        assert junc.position == (50.8, 76.2)
        assert junc.diameter == 1.0
        assert junc.uuid == "junction-uuid"

    def test_junction_from_sexp_minimal(self):
        """Test parsing junction with minimal data."""
        sexp = parse_sexp("(junction)")
        junc = Junction.from_sexp(sexp)
        assert junc.position == (0.0, 0.0)
        assert junc.diameter == 0.0
        assert junc.uuid == ""

    def test_junction_repr(self):
        """Test junction string representation."""
        junc = Junction(position=(10, 20))
        assert "Junction" in repr(junc)
        assert "(10, 20)" in repr(junc)


class TestBus:
    """Tests for Bus class."""

    def test_bus_from_sexp(self):
        """Test parsing bus from S-expression."""
        sexp = parse_sexp("""(bus
            (pts (xy 10 20) (xy 30 40))
            (uuid "bus-uuid")
        )""")
        bus = Bus.from_sexp(sexp)
        assert bus.start == (10.0, 20.0)
        assert bus.end == (30.0, 40.0)
        assert bus.uuid == "bus-uuid"

    def test_bus_from_sexp_minimal(self):
        """Test parsing bus with minimal data."""
        sexp = parse_sexp("(bus)")
        bus = Bus.from_sexp(sexp)
        assert bus.start == (0.0, 0.0)
        assert bus.end == (0.0, 0.0)


class TestLabel:
    """Tests for Label class."""

    def test_label_from_sexp(self):
        """Test parsing label from S-expression."""
        sexp = parse_sexp("""(label "NET1"
            (at 50 60 90)
            (uuid "label-uuid")
        )""")
        label = Label.from_sexp(sexp)
        assert label.text == "NET1"
        assert label.position == (50.0, 60.0)
        assert label.rotation == 90.0
        assert label.uuid == "label-uuid"

    def test_label_from_sexp_no_rotation(self):
        """Test parsing label without rotation."""
        sexp = parse_sexp("""(label "VCC"
            (at 10 20)
            (uuid "uuid")
        )""")
        label = Label.from_sexp(sexp)
        assert label.text == "VCC"
        assert label.rotation == 0.0

    def test_label_repr(self):
        """Test label string representation."""
        label = Label(text="GND", position=(0, 0))
        assert "Label" in repr(label)
        assert "GND" in repr(label)


class TestHierarchicalLabel:
    """Tests for HierarchicalLabel class."""

    def test_hierarchical_label_from_sexp(self):
        """Test parsing hierarchical label."""
        sexp = parse_sexp("""(hierarchical_label "CLK"
            (shape output)
            (at 100 50 180)
            (uuid "hlabel-uuid")
        )""")
        label = HierarchicalLabel.from_sexp(sexp)
        assert label.text == "CLK"
        assert label.shape == "output"
        assert label.position == (100.0, 50.0)
        assert label.rotation == 180.0
        assert label.uuid == "hlabel-uuid"

    def test_hierarchical_label_shapes(self):
        """Test different hierarchical label shapes."""
        for shape in ["input", "output", "bidirectional", "tri_state", "passive"]:
            sexp = parse_sexp(f"""(hierarchical_label "SIG"
                (shape {shape})
                (at 0 0)
            )""")
            label = HierarchicalLabel.from_sexp(sexp)
            assert label.shape == shape

    def test_hierarchical_label_default_shape(self):
        """Test default shape for hierarchical label."""
        sexp = parse_sexp("""(hierarchical_label "SIG" (at 0 0))""")
        label = HierarchicalLabel.from_sexp(sexp)
        assert label.shape == "input"

    def test_hierarchical_label_repr(self):
        """Test hierarchical label string representation."""
        label = HierarchicalLabel(text="DATA", position=(0, 0), shape="bidirectional")
        assert "HierarchicalLabel" in repr(label)
        assert "DATA" in repr(label)
        assert "bidirectional" in repr(label)


class TestGlobalLabel:
    """Tests for GlobalLabel class."""

    def test_global_label_from_sexp(self):
        """Test parsing global label."""
        sexp = parse_sexp("""(global_label "RESET"
            (shape input)
            (at 75 25 0)
            (uuid "glabel-uuid")
        )""")
        label = GlobalLabel.from_sexp(sexp)
        assert label.text == "RESET"
        assert label.shape == "input"
        assert label.position == (75.0, 25.0)
        assert label.rotation == 0.0
        assert label.uuid == "glabel-uuid"

    def test_global_label_repr(self):
        """Test global label string representation."""
        label = GlobalLabel(text="SPI_CLK", position=(0, 0))
        assert "GlobalLabel" in repr(label)
        assert "SPI_CLK" in repr(label)


class TestPowerSymbol:
    """Tests for PowerSymbol class."""

    def test_power_symbol_from_sexp(self):
        """Test parsing power symbol."""
        sexp = parse_sexp("""(symbol
            (lib_id "power:GND")
            (at 50 100 0)
            (uuid "power-uuid")
            (property "Reference" "#PWR01" (at 0 0 0))
            (property "Value" "GND" (at 0 0 0))
        )""")
        power = PowerSymbol.from_symbol_sexp(sexp)
        assert power is not None
        assert power.lib_id == "power:GND"
        assert power.position == (50.0, 100.0)
        assert power.value == "GND"
        assert power.uuid == "power-uuid"

    def test_power_symbol_vcc(self):
        """Test parsing VCC power symbol."""
        sexp = parse_sexp("""(symbol
            (lib_id "power:+5V")
            (at 30 40 0)
            (property "Value" "+5V" (at 0 0 0))
        )""")
        power = PowerSymbol.from_symbol_sexp(sexp)
        assert power is not None
        assert power.lib_id == "power:+5V"
        assert power.value == "+5V"

    def test_power_symbol_non_power_returns_none(self):
        """Test that non-power symbol returns None."""
        sexp = parse_sexp("""(symbol
            (lib_id "Device:R")
            (at 50 100 0)
        )""")
        power = PowerSymbol.from_symbol_sexp(sexp)
        assert power is None


class TestBOMItem:
    """Tests for BOMItem class."""

    def test_bom_item_is_power_symbol(self):
        """Test power symbol detection."""
        power = BOMItem(
            reference="#PWR01",
            value="GND",
            footprint="",
            lib_id="power:GND",
        )
        assert power.is_power_symbol is True

        resistor = BOMItem(
            reference="R1",
            value="10k",
            footprint="R_0402",
            lib_id="Device:R",
        )
        assert resistor.is_power_symbol is False

    def test_bom_item_is_virtual(self):
        """Test virtual component detection."""
        # Power symbol is virtual
        power = BOMItem(
            reference="#PWR01",
            value="GND",
            footprint="",
            lib_id="power:GND",
            in_bom=True,
        )
        assert power.is_virtual is True

        # Component with in_bom=False is virtual
        virtual = BOMItem(
            reference="TP1",
            value="Test",
            footprint="",
            lib_id="Connector:TestPoint",
            in_bom=False,
        )
        assert virtual.is_virtual is True

        # Regular component is not virtual
        resistor = BOMItem(
            reference="R1",
            value="10k",
            footprint="R_0402",
            lib_id="Device:R",
            in_bom=True,
        )
        assert resistor.is_virtual is False


class TestBOMGroup:
    """Tests for BOMGroup class."""

    def test_bom_group_quantity(self):
        """Test quantity property."""
        group = BOMGroup(
            value="10k",
            footprint="R_0402",
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="R3", value="10k", footprint="R_0402", lib_id="Device:R"),
            ],
        )
        assert group.quantity == 3

    def test_bom_group_references_sorted(self):
        """Test that references are sorted correctly."""
        group = BOMGroup(
            value="100nF",
            footprint="C_0402",
            items=[
                BOMItem(reference="C10", value="100nF", footprint="C_0402", lib_id="Device:C"),
                BOMItem(reference="C2", value="100nF", footprint="C_0402", lib_id="Device:C"),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            ],
        )
        # Should be sorted by prefix then number
        assert group.references == "C1, C2, C10"

    def test_bom_group_lcsc(self):
        """Test LCSC part number extraction."""
        group = BOMGroup(
            value="10k",
            footprint="R_0402",
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R", lcsc="C25744"),
            ],
        )
        assert group.lcsc == "C25744"

    def test_bom_group_lcsc_empty(self):
        """Test LCSC when no item has it."""
        group = BOMGroup(
            value="10k",
            footprint="R_0402",
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            ],
        )
        assert group.lcsc == ""

    def test_bom_group_mpn(self):
        """Test MPN extraction."""
        group = BOMGroup(
            value="10k",
            footprint="R_0402",
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R", mpn="RC0402FR-0710KL"),
            ],
        )
        assert group.mpn == "RC0402FR-0710KL"

    def test_bom_group_description(self):
        """Test description extraction."""
        group = BOMGroup(
            value="10k",
            footprint="R_0402",
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R", description="Resistor"),
            ],
        )
        assert group.description == "Resistor"


class TestBOM:
    """Tests for BOM class."""

    def test_bom_total_components(self):
        """Test total component count (excluding virtual and DNP)."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="#PWR01", value="GND", footprint="", lib_id="power:GND"),
            BOMItem(reference="R3", value="10k", footprint="R_0402", lib_id="Device:R", dnp=True),
        ])
        assert bom.total_components == 2  # Only R1 and R2

    def test_bom_dnp_count(self):
        """Test DNP count."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R", dnp=True),
            BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C", dnp=True),
        ])
        assert bom.dnp_count == 2

    def test_bom_grouped_by_value_footprint(self):
        """Test grouping by value+footprint (default)."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R3", value="10k", footprint="R_0603", lib_id="Device:R"),
            BOMItem(reference="R4", value="4.7k", footprint="R_0402", lib_id="Device:R"),
        ])
        groups = bom.grouped()
        assert len(groups) == 3  # 10k/0402, 10k/0603, 4.7k/0402

    def test_bom_grouped_by_value(self):
        """Test grouping by value only."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R2", value="10k", footprint="R_0603", lib_id="Device:R"),
            BOMItem(reference="R3", value="4.7k", footprint="R_0402", lib_id="Device:R"),
        ])
        groups = bom.grouped(by="value")
        assert len(groups) == 2  # 10k, 4.7k

    def test_bom_grouped_by_footprint(self):
        """Test grouping by footprint only."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R2", value="4.7k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R3", value="10k", footprint="R_0603", lib_id="Device:R"),
        ])
        groups = bom.grouped(by="footprint")
        assert len(groups) == 2  # R_0402, R_0603

    def test_bom_grouped_by_mpn(self):
        """Test grouping by MPN."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R", mpn="RC0402FR-0710KL"),
            BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R", mpn="RC0402FR-0710KL"),
            BOMItem(reference="R3", value="10k", footprint="R_0402", lib_id="Device:R", mpn="ERJ-2RKF1002X"),
        ])
        groups = bom.grouped(by="mpn")
        assert len(groups) == 2

    def test_bom_grouped_excludes_virtual(self):
        """Test that grouped excludes virtual components."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="#PWR01", value="GND", footprint="", lib_id="power:GND"),
        ])
        groups = bom.grouped()
        assert len(groups) == 1
        assert groups[0].items[0].reference == "R1"

    def test_bom_unique_parts(self):
        """Test unique parts count."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
        ])
        assert bom.unique_parts == 2

    def test_bom_filter_exclude_dnp(self):
        """Test filtering to exclude DNP."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R", dnp=True),
        ])
        filtered = bom.filter(include_dnp=False)
        assert len(filtered.items) == 1
        assert filtered.items[0].reference == "R1"

    def test_bom_filter_include_dnp(self):
        """Test filtering to include DNP."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R", dnp=True),
        ])
        filtered = bom.filter(include_dnp=True)
        assert len(filtered.items) == 2

    def test_bom_filter_by_reference_pattern(self):
        """Test filtering by reference pattern."""
        bom = BOM(items=[
            BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R"),
            BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            BOMItem(reference="U1", value="ATmega", footprint="QFP", lib_id="MCU:ATmega"),
        ])
        filtered = bom.filter(reference_pattern="R*")
        assert len(filtered.items) == 2
        assert all(item.reference.startswith("R") for item in filtered.items)


class TestTitleBlock:
    """Tests for TitleBlock class."""

    def test_title_block_from_sexp(self):
        """Test parsing title block."""
        sexp = parse_sexp("""(title_block
            (title "Test Project")
            (date "2024-01-15")
            (rev "1.0")
            (company "ACME Corp")
            (comment 1 "First comment")
            (comment 2 "Second comment")
        )""")
        tb = TitleBlock.from_sexp(sexp)
        assert tb.title == "Test Project"
        assert tb.date == "2024-01-15"
        assert tb.rev == "1.0"
        assert tb.company == "ACME Corp"
        assert tb.comments[1] == "First comment"
        assert tb.comments[2] == "Second comment"

    def test_title_block_empty(self):
        """Test empty title block."""
        sexp = parse_sexp("(title_block)")
        tb = TitleBlock.from_sexp(sexp)
        assert tb.title == ""
        assert tb.date == ""
        assert tb.rev == ""
        assert tb.company == ""
        assert len(tb.comments) == 0


class TestSchematicSheetInstance:
    """Tests for SheetInstance class in schematic module."""

    def test_sheet_instance_from_sexp(self):
        """Test parsing sheet instance."""
        sexp = parse_sexp("""(sheet
            (at 100 50)
            (size 76.2 50.8)
            (uuid "sheet-uuid")
            (property "Sheetname" "Power Supply")
            (property "Sheetfile" "power.kicad_sch")
        )""")
        sheet = SheetInstance.from_sexp(sexp)
        assert sheet.name == "Power Supply"
        assert sheet.filename == "power.kicad_sch"
        assert sheet.uuid == "sheet-uuid"
        assert sheet.position == (100.0, 50.0)
        assert sheet.size == (76.2, 50.8)


class TestSchematic:
    """Tests for Schematic class."""

    def test_schematic_load(self, minimal_schematic: Path):
        """Test loading schematic from file."""
        sch = Schematic.load(minimal_schematic)
        assert sch.path == minimal_schematic
        assert len(sch.symbols) == 1

    def test_schematic_version(self, minimal_schematic: Path):
        """Test getting schematic version."""
        sch = Schematic.load(minimal_schematic)
        assert sch.version == 20231120

    def test_schematic_generator(self, minimal_schematic: Path):
        """Test getting generator."""
        sch = Schematic.load(minimal_schematic)
        assert sch.generator == "test"

    def test_schematic_paper(self, minimal_schematic: Path):
        """Test getting paper size."""
        sch = Schematic.load(minimal_schematic)
        assert sch.paper == "A4"

    def test_schematic_uuid(self, minimal_schematic: Path):
        """Test getting UUID."""
        sch = Schematic.load(minimal_schematic)
        assert sch.uuid == "00000000-0000-0000-0000-000000000001"

    def test_schematic_symbols(self, minimal_schematic: Path):
        """Test getting symbols."""
        sch = Schematic.load(minimal_schematic)
        assert len(sch.symbols) == 1
        assert sch.symbols[0].reference == "R1"

    def test_schematic_get_symbol(self, minimal_schematic: Path):
        """Test getting symbol by reference."""
        sch = Schematic.load(minimal_schematic)
        sym = sch.get_symbol("R1")
        assert sym is not None
        assert sym.reference == "R1"
        assert sym.value == "10k"

    def test_schematic_get_symbol_not_found(self, minimal_schematic: Path):
        """Test getting non-existent symbol."""
        sch = Schematic.load(minimal_schematic)
        assert sch.get_symbol("R99") is None

    def test_schematic_find_symbols_by_lib(self, minimal_schematic: Path):
        """Test finding symbols by library ID."""
        sch = Schematic.load(minimal_schematic)
        symbols = sch.find_symbols_by_lib("Device:R")
        assert len(symbols) == 1
        assert symbols[0].reference == "R1"

    def test_schematic_iter_symbols(self, minimal_schematic: Path):
        """Test iterating over symbols."""
        sch = Schematic.load(minimal_schematic)
        symbols = list(sch.iter_symbols())
        assert len(symbols) == 1

    def test_schematic_wires(self, minimal_schematic: Path):
        """Test getting wires."""
        sch = Schematic.load(minimal_schematic)
        assert len(sch.wires) == 1
        assert sch.wires[0].start == (90.0, 100.0)

    def test_schematic_labels(self, minimal_schematic: Path):
        """Test getting labels."""
        sch = Schematic.load(minimal_schematic)
        assert len(sch.labels) == 1
        assert sch.labels[0].text == "NET1"

    def test_schematic_junctions(self, minimal_schematic: Path):
        """Test getting junctions (empty in minimal)."""
        sch = Schematic.load(minimal_schematic)
        assert len(sch.junctions) == 0

    def test_schematic_is_hierarchical(self, minimal_schematic: Path):
        """Test hierarchical check."""
        sch = Schematic.load(minimal_schematic)
        assert sch.is_hierarchical() is False

    def test_schematic_invalidate_cache(self, minimal_schematic: Path):
        """Test cache invalidation."""
        sch = Schematic.load(minimal_schematic)
        # Access to populate cache
        _ = sch.symbols
        _ = sch.wires
        # Invalidate
        sch.invalidate_cache()
        # Check that cache is cleared
        assert sch._symbols is None
        assert sch._wires is None

    def test_schematic_save(self, minimal_schematic: Path, tmp_path: Path):
        """Test saving schematic."""
        sch = Schematic.load(minimal_schematic)
        new_path = tmp_path / "saved.kicad_sch"
        sch.save(new_path)
        assert new_path.exists()
        # Load saved file and verify
        sch2 = Schematic.load(new_path)
        assert len(sch2.symbols) == 1

    def test_schematic_repr(self, minimal_schematic: Path):
        """Test schematic string representation."""
        sch = Schematic.load(minimal_schematic)
        s = repr(sch)
        assert "Schematic" in s
        assert "symbols=1" in s


class TestLibraryPin:
    """Tests for LibraryPin class."""

    def test_library_pin_from_sexp(self):
        """Test parsing library pin."""
        sexp = parse_sexp("""(pin input line
            (at 5.08 0 180)
            (length 2.54)
            (name "IN")
            (number "1")
        )""")
        pin = LibraryPin.from_sexp(sexp)
        assert pin.number == "1"
        assert pin.name == "IN"
        assert pin.type == "input"
        assert pin.position == (5.08, 0.0)
        assert pin.rotation == 180.0
        assert pin.length == 2.54

    def test_library_pin_connection_offset(self):
        """Test connection offset property."""
        pin = LibraryPin(
            number="1",
            name="IN",
            type="input",
            position=(5.08, 0),
            rotation=0,
            length=2.54,
        )
        # Currently returns (0, 0)
        assert pin.connection_offset == (0, 0)


class TestLibrarySymbol:
    """Tests for LibrarySymbol class."""

    def test_library_symbol_from_sexp(self):
        """Test parsing library symbol."""
        sexp = parse_sexp("""(symbol "Device:R"
            (property "Reference" "R")
            (property "Value" "R")
            (symbol "Device:R_0_1"
                (pin passive line (at -2.54 0 0) (length 2.54) (name "1") (number "1"))
                (pin passive line (at 2.54 0 180) (length 2.54) (name "2") (number "2"))
            )
        )""")
        sym = LibrarySymbol.from_sexp(sexp)
        assert sym.name == "Device:R"
        assert "Reference" in sym.properties
        assert len(sym.pins) == 2

    def test_library_symbol_pin_count(self):
        """Test pin count property."""
        sym = LibrarySymbol(
            name="Test",
            pins=[
                LibraryPin(number="1", name="A", type="input", position=(0, 0), rotation=0, length=2.54),
                LibraryPin(number="2", name="B", type="output", position=(0, 0), rotation=0, length=2.54),
            ],
        )
        assert sym.pin_count == 2

    def test_library_symbol_get_pin(self):
        """Test getting pin by number."""
        sym = LibrarySymbol(
            name="Test",
            pins=[
                LibraryPin(number="1", name="A", type="input", position=(0, 0), rotation=0, length=2.54),
                LibraryPin(number="2", name="B", type="output", position=(0, 0), rotation=0, length=2.54),
            ],
        )
        pin = sym.get_pin("2")
        assert pin is not None
        assert pin.name == "B"
        assert sym.get_pin("99") is None

    def test_library_symbol_get_pins_by_name(self):
        """Test getting pins by name."""
        sym = LibrarySymbol(
            name="Test",
            pins=[
                LibraryPin(number="1", name="GND", type="power_in", position=(0, 0), rotation=0, length=2.54),
                LibraryPin(number="2", name="VCC", type="power_in", position=(0, 0), rotation=0, length=2.54),
                LibraryPin(number="3", name="GND", type="power_in", position=(0, 0), rotation=0, length=2.54),
            ],
        )
        gnd_pins = sym.get_pins_by_name("GND")
        assert len(gnd_pins) == 2

    def test_library_symbol_get_pin_position(self):
        """Test calculating pin position in schematic."""
        sym = LibrarySymbol(
            name="Test",
            pins=[
                LibraryPin(number="1", name="A", type="input", position=(5, 0), rotation=0, length=2.54),
            ],
        )
        pos = sym.get_pin_position("1", instance_pos=(100, 100))
        assert pos == (105.0, 100.0)

    def test_library_symbol_get_pin_position_with_rotation(self):
        """Test pin position with symbol rotation."""
        sym = LibrarySymbol(
            name="Test",
            pins=[
                LibraryPin(number="1", name="A", type="input", position=(5, 0), rotation=0, length=2.54),
            ],
        )
        # 90 degree rotation
        pos = sym.get_pin_position("1", instance_pos=(100, 100), instance_rot=90)
        assert pos is not None
        assert pos[0] == pytest.approx(100.0, abs=0.01)
        assert pos[1] == pytest.approx(105.0, abs=0.01)

    def test_library_symbol_get_pin_position_with_mirror_x(self):
        """Test pin position with X mirror."""
        sym = LibrarySymbol(
            name="Test",
            pins=[
                LibraryPin(number="1", name="A", type="input", position=(5, 0), rotation=0, length=2.54),
            ],
        )
        pos = sym.get_pin_position("1", instance_pos=(100, 100), mirror="x")
        assert pos == (95.0, 100.0)

    def test_library_symbol_get_pin_position_with_mirror_y(self):
        """Test pin position with Y mirror."""
        sym = LibrarySymbol(
            name="Test",
            pins=[
                LibraryPin(number="1", name="A", type="input", position=(0, 5), rotation=0, length=2.54),
            ],
        )
        pos = sym.get_pin_position("1", instance_pos=(100, 100), mirror="y")
        assert pos == (100.0, 95.0)

    def test_library_symbol_get_all_pin_positions(self):
        """Test getting all pin positions."""
        sym = LibrarySymbol(
            name="Test",
            pins=[
                LibraryPin(number="1", name="A", type="input", position=(5, 0), rotation=0, length=2.54),
                LibraryPin(number="2", name="B", type="output", position=(-5, 0), rotation=0, length=2.54),
            ],
        )
        positions = sym.get_all_pin_positions(instance_pos=(100, 100))
        assert len(positions) == 2
        assert positions["1"] == (105.0, 100.0)
        assert positions["2"] == (95.0, 100.0)


class TestLibraryManager:
    """Tests for LibraryManager class."""

    def test_library_manager_add_library(self):
        """Test adding library."""
        manager = LibraryManager()
        lib = SymbolLibrary(path="test.kicad_sym", symbols={})
        manager.add_library("Test", lib)
        assert "Test" in manager.libraries

    def test_library_manager_get_symbol_no_colon(self):
        """Test getting symbol without library prefix."""
        manager = LibraryManager()
        sym = LibrarySymbol(name="R", properties={}, pins=[])
        lib = SymbolLibrary(path="Device.kicad_sym", symbols={"R": sym})
        manager.add_library("Device", lib)

        found = manager.get_symbol("R")
        assert found is not None
        assert found.name == "R"

    def test_library_manager_get_symbol_with_colon(self):
        """Test getting symbol with library prefix."""
        manager = LibraryManager()
        sym = LibrarySymbol(name="R", properties={}, pins=[])
        lib = SymbolLibrary(path="Device.kicad_sym", symbols={"R": sym})
        manager.add_library("Device", lib)

        found = manager.get_symbol("Device:R")
        assert found is not None
        assert found.name == "R"

    def test_library_manager_get_symbol_not_found(self):
        """Test getting non-existent symbol."""
        manager = LibraryManager()
        assert manager.get_symbol("Device:NonExistent") is None


class TestHierarchySheetPin:
    """Tests for SheetPin in hierarchy module."""

    def test_sheet_pin_from_sexp(self):
        """Test parsing sheet pin."""
        sexp = parse_sexp("""(pin "CLK" input
            (at 100 50 0)
            (uuid "pin-uuid")
        )""")
        pin = SheetPin.from_sexp(sexp)
        assert pin.name == "CLK"
        assert pin.direction == "input"
        assert pin.position == (100.0, 50.0)
        assert pin.uuid == "pin-uuid"


class TestHierarchySheetInstance:
    """Tests for SheetInstance in hierarchy module."""

    def test_sheet_instance_from_sexp(self):
        """Test parsing sheet instance."""
        sexp = parse_sexp("""(sheet
            (at 100 50)
            (size 76.2 50.8)
            (uuid "sheet-uuid")
            (property "Sheetname" "Power")
            (property "Sheetfile" "power.kicad_sch")
            (pin "VIN" input (at 100 60 0) (uuid "pin1"))
            (pin "VOUT" output (at 176.2 60 180) (uuid "pin2"))
        )""")
        sheet = HierarchySheetInstance.from_sexp(sexp)
        assert sheet.name == "Power"
        assert sheet.filename == "power.kicad_sch"
        assert len(sheet.pins) == 2

    def test_sheet_instance_input_pins(self):
        """Test filtering input pins."""
        sheet = HierarchySheetInstance(
            name="Test",
            filename="test.kicad_sch",
            uuid="uuid",
            position=(0, 0),
            size=(50, 25),
            pins=[
                SheetPin(name="IN1", direction="input", position=(0, 0), rotation=0, uuid="1"),
                SheetPin(name="OUT1", direction="output", position=(0, 0), rotation=0, uuid="2"),
                SheetPin(name="IN2", direction="input", position=(0, 0), rotation=0, uuid="3"),
            ],
        )
        assert len(sheet.input_pins) == 2

    def test_sheet_instance_output_pins(self):
        """Test filtering output pins."""
        sheet = HierarchySheetInstance(
            name="Test",
            filename="test.kicad_sch",
            uuid="uuid",
            position=(0, 0),
            size=(50, 25),
            pins=[
                SheetPin(name="IN1", direction="input", position=(0, 0), rotation=0, uuid="1"),
                SheetPin(name="OUT1", direction="output", position=(0, 0), rotation=0, uuid="2"),
            ],
        )
        assert len(sheet.output_pins) == 1


class TestHierarchyNode:
    """Tests for HierarchyNode class."""

    def test_hierarchy_node_depth_root(self):
        """Test depth of root node."""
        root = HierarchyNode(name="Root", path="/root.kicad_sch", uuid="1")
        assert root.depth == 0

    def test_hierarchy_node_depth_child(self):
        """Test depth of child nodes."""
        root = HierarchyNode(name="Root", path="/root.kicad_sch", uuid="1")
        child = HierarchyNode(name="Child", path="/child.kicad_sch", uuid="2", parent=root)
        grandchild = HierarchyNode(name="Grandchild", path="/gc.kicad_sch", uuid="3", parent=child)

        assert child.depth == 1
        assert grandchild.depth == 2

    def test_hierarchy_node_is_root(self):
        """Test is_root property."""
        root = HierarchyNode(name="Root", path="/root.kicad_sch", uuid="1")
        child = HierarchyNode(name="Child", path="/child.kicad_sch", uuid="2", parent=root)

        assert root.is_root is True
        assert child.is_root is False

    def test_hierarchy_node_is_leaf(self):
        """Test is_leaf property."""
        root = HierarchyNode(name="Root", path="/root.kicad_sch", uuid="1")
        child = HierarchyNode(name="Child", path="/child.kicad_sch", uuid="2", parent=root)
        root.children.append(child)

        assert root.is_leaf is False
        assert child.is_leaf is True

    def test_hierarchy_node_get_path_string_root(self):
        """Test path string for root."""
        root = HierarchyNode(name="Root", path="/root.kicad_sch", uuid="1")
        assert root.get_path_string() == "/"

    def test_hierarchy_node_get_path_string_nested(self):
        """Test path string for nested nodes."""
        root = HierarchyNode(name="Root", path="/root.kicad_sch", uuid="1")
        child = HierarchyNode(name="Power", path="/power.kicad_sch", uuid="2", parent=root)
        grandchild = HierarchyNode(name="Regulator", path="/reg.kicad_sch", uuid="3", parent=child)

        assert child.get_path_string() == "/Power"
        assert grandchild.get_path_string() == "/Power/Regulator"

    def test_hierarchy_node_find_by_name(self):
        """Test finding node by name."""
        root = HierarchyNode(name="Root", path="/root.kicad_sch", uuid="1")
        child1 = HierarchyNode(name="Power", path="/power.kicad_sch", uuid="2", parent=root)
        child2 = HierarchyNode(name="Audio", path="/audio.kicad_sch", uuid="3", parent=root)
        grandchild = HierarchyNode(name="Amp", path="/amp.kicad_sch", uuid="4", parent=child2)
        root.children = [child1, child2]
        child2.children = [grandchild]

        assert root.find_by_name("Power") == child1
        assert root.find_by_name("Amp") == grandchild
        assert root.find_by_name("NonExistent") is None

    def test_hierarchy_node_find_by_path(self):
        """Test finding node by path."""
        root = HierarchyNode(name="Root", path="/root.kicad_sch", uuid="1")
        child = HierarchyNode(name="Power", path="/power.kicad_sch", uuid="2", parent=root)
        grandchild = HierarchyNode(name="Regulator", path="/reg.kicad_sch", uuid="3", parent=child)
        root.children = [child]
        child.children = [grandchild]

        assert root.find_by_path("/Power") == child
        assert root.find_by_path("/Power/Regulator") == grandchild
        assert root.find_by_path("/NonExistent") is None

    def test_hierarchy_node_all_nodes(self):
        """Test getting all nodes in hierarchy."""
        root = HierarchyNode(name="Root", path="/root.kicad_sch", uuid="1")
        child1 = HierarchyNode(name="Power", path="/power.kicad_sch", uuid="2", parent=root)
        child2 = HierarchyNode(name="Audio", path="/audio.kicad_sch", uuid="3", parent=root)
        grandchild = HierarchyNode(name="Amp", path="/amp.kicad_sch", uuid="4", parent=child2)
        root.children = [child1, child2]
        child2.children = [grandchild]

        all_nodes = root.all_nodes()
        assert len(all_nodes) == 4
        assert root in all_nodes
        assert child1 in all_nodes
        assert child2 in all_nodes
        assert grandchild in all_nodes
