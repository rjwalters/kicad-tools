"""Tests for the schematic editing API.

Covers add_symbol(), add_power(), add_wire(), embed_lib_symbol(),
snap_to_grid(), snap_all_to_grid(), and round-trip serialization.
"""

from pathlib import Path

import pytest

from kicad_tools.schema import Schematic
from kicad_tools.schema.library import LibrarySymbol
from kicad_tools.schema.symbol import SymbolInstance, SymbolPin, SymbolProperty
from kicad_tools.schema.wire import Junction, Wire

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A minimal schematic with a lib_symbols section containing Device:R
SCHEMATIC_WITH_LIB = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "Device:R_0_1"
        (polyline (pts (xy -1.016 -2.54) (xy -1.016 2.54)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "Device:R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )
    (symbol "power:GND"
      (property "Reference" "#PWR" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "GND" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "power:GND_0_1"
        (polyline (pts (xy 0 0) (xy 0 -1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "power:GND_1_1"
        (pin power_in line (at 0 0 0) (length 0) (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )
  )
  (sheet_instances
    (path "/" (page "1"))
  )
)
"""


def _write_schematic(tmp_path: Path, content: str = SCHEMATIC_WITH_LIB) -> Path:
    p = tmp_path / "edit_test.kicad_sch"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# SymbolProperty.to_sexp round-trip
# ---------------------------------------------------------------------------


class TestSymbolPropertyToSexp:
    def test_visible_property_round_trip(self):
        prop = SymbolProperty(
            name="Reference", value="R1", position=(10, 20), rotation=0, visible=True
        )
        sexp = prop.to_sexp()
        parsed = SymbolProperty.from_sexp(sexp)
        assert parsed.name == "Reference"
        assert parsed.value == "R1"
        assert parsed.visible is True

    def test_hidden_property_round_trip(self):
        prop = SymbolProperty(
            name="Footprint", value="SMD:R_0402", position=(5, 5), rotation=0, visible=False
        )
        sexp = prop.to_sexp()
        parsed = SymbolProperty.from_sexp(sexp)
        assert parsed.name == "Footprint"
        assert parsed.value == "SMD:R_0402"
        assert parsed.visible is False


# ---------------------------------------------------------------------------
# SymbolPin.to_sexp round-trip
# ---------------------------------------------------------------------------


class TestSymbolPinToSexp:
    def test_round_trip(self):
        pin = SymbolPin(number="1", uuid="aaaa-bbbb", name=None)
        sexp = pin.to_sexp()
        parsed = SymbolPin.from_sexp(sexp)
        assert parsed.number == "1"
        assert parsed.uuid == "aaaa-bbbb"


# ---------------------------------------------------------------------------
# Wire.to_sexp round-trip
# ---------------------------------------------------------------------------


class TestWireToSexp:
    def test_round_trip(self):
        wire = Wire(start=(10, 20), end=(30, 40), uuid="wire-uuid-1")
        sexp = wire.to_sexp()
        parsed = Wire.from_sexp(sexp)
        assert parsed.start == (10, 20)
        assert parsed.end == (30, 40)
        assert parsed.uuid == "wire-uuid-1"


# ---------------------------------------------------------------------------
# Junction.to_sexp round-trip
# ---------------------------------------------------------------------------


class TestJunctionToSexp:
    def test_round_trip(self):
        junc = Junction(position=(15, 25), uuid="junc-uuid-1", diameter=0)
        sexp = junc.to_sexp()
        parsed = Junction.from_sexp(sexp)
        assert parsed.position == (15, 25)
        assert parsed.uuid == "junc-uuid-1"


# ---------------------------------------------------------------------------
# SymbolInstance.to_sexp round-trip
# ---------------------------------------------------------------------------


class TestSymbolInstanceToSexp:
    def test_round_trip(self):
        props = {
            "Reference": SymbolProperty("Reference", "C1", (50, 60), 0, True),
            "Value": SymbolProperty("Value", "100nF", (50, 62), 0, True),
            "Footprint": SymbolProperty("Footprint", "SMD:C_0402", (50, 60), 0, False),
            "Datasheet": SymbolProperty("Datasheet", "", (50, 60), 0, False),
        }
        pins = [
            SymbolPin("1", "pin-uuid-1"),
            SymbolPin("2", "pin-uuid-2"),
        ]
        inst = SymbolInstance(
            lib_id="Device:C",
            uuid="sym-uuid-1",
            position=(50, 60),
            rotation=90,
            mirror="x",
            unit=1,
            in_bom=True,
            on_board=True,
            properties=props,
            pins=pins,
        )
        sexp = inst.to_sexp()
        parsed = SymbolInstance.from_sexp(sexp)

        assert parsed.lib_id == "Device:C"
        assert parsed.uuid == "sym-uuid-1"
        assert parsed.position == (50, 60)
        assert parsed.rotation == 90
        assert parsed.mirror == "x"
        assert parsed.in_bom is True
        assert parsed.on_board is True
        assert parsed.reference == "C1"
        assert parsed.value == "100nF"
        assert parsed.footprint == "SMD:C_0402"
        assert len(parsed.pins) == 2


# ---------------------------------------------------------------------------
# Schematic.add_symbol
# ---------------------------------------------------------------------------


class TestAddSymbol:
    def test_add_symbol_basic(self, tmp_path: Path):
        sch = Schematic.load(_write_schematic(tmp_path))
        assert len(sch.symbols) == 0

        sym = sch.add_symbol(
            lib_id="Device:R",
            reference="R1",
            value="10k",
            footprint="Resistor_SMD:R_0402",
            position=(100, 100),
        )

        assert isinstance(sym, SymbolInstance)
        assert sym.reference == "R1"
        assert sym.value == "10k"
        assert sym.lib_id == "Device:R"
        assert sym.position == (100, 100)
        assert len(sym.pins) == 2  # auto-detected from lib_symbols
        assert sym.in_bom is True
        assert sym.on_board is True

        # Cache should reflect the new symbol
        assert len(sch.symbols) == 1
        assert sch.symbols[0].reference == "R1"

    def test_add_symbol_with_rotation(self, tmp_path: Path):
        sch = Schematic.load(_write_schematic(tmp_path))
        sym = sch.add_symbol(
            lib_id="Device:R",
            reference="R2",
            value="4.7k",
            footprint="Resistor_SMD:R_0402",
            position=(120, 80),
            rotation=90,
            mirror="x",
        )
        assert sym.rotation == 90
        assert sym.mirror == "x"

    def test_add_symbol_missing_lib_raises(self, tmp_path: Path):
        sch = Schematic.load(_write_schematic(tmp_path))
        with pytest.raises(ValueError, match="not found in schematic lib_symbols"):
            sch.add_symbol(
                lib_id="Device:C",
                reference="C1",
                value="100nF",
                footprint="SMD:C_0402",
                position=(50, 50),
            )

    def test_add_symbol_explicit_pins(self, tmp_path: Path):
        """When pin_numbers is given, lib_symbols lookup is not required."""
        sch = Schematic.load(_write_schematic(tmp_path))
        sym = sch.add_symbol(
            lib_id="Device:C",
            reference="C1",
            value="100nF",
            footprint="SMD:C_0402",
            position=(50, 50),
            pin_numbers=["1", "2"],
        )
        assert len(sym.pins) == 2

    def test_add_multiple_symbols(self, tmp_path: Path):
        sch = Schematic.load(_write_schematic(tmp_path))
        sch.add_symbol("Device:R", "R1", "10k", "SMD:R_0402", (100, 100))
        sch.add_symbol("Device:R", "R2", "4.7k", "SMD:R_0402", (120, 100))
        assert len(sch.symbols) == 2

    def test_insertion_before_sheet_instances(self, tmp_path: Path):
        """New symbols must be inserted before sheet_instances."""
        sch = Schematic.load(_write_schematic(tmp_path))
        sch.add_symbol("Device:R", "R1", "10k", "SMD:R_0402", (100, 100))

        # Verify sheet_instances is still the last named section
        last_named = None
        for child in sch.sexp.children:
            if child.name:
                last_named = child.name
        assert last_named == "sheet_instances"


# ---------------------------------------------------------------------------
# Schematic.add_power
# ---------------------------------------------------------------------------


class TestAddPower:
    def test_add_power_gnd(self, tmp_path: Path):
        sch = Schematic.load(_write_schematic(tmp_path))
        sym = sch.add_power("GND", (100, 110))

        assert sym.lib_id == "power:GND"
        assert sym.in_bom is False
        assert sym.on_board is False
        assert sym.position == (100, 110)
        assert len(sym.pins) == 1  # auto-detected from lib_symbols
        assert len(sch.symbols) == 1


# ---------------------------------------------------------------------------
# Schematic.add_wire
# ---------------------------------------------------------------------------


class TestAddWire:
    def test_add_wire(self, tmp_path: Path):
        sch = Schematic.load(_write_schematic(tmp_path))
        assert len(sch.wires) == 0

        wire = sch.add_wire((90, 100), (110, 100))
        assert isinstance(wire, Wire)
        assert wire.start == (90, 100)
        assert wire.end == (110, 100)
        assert wire.uuid  # UUID was generated
        assert len(sch.wires) == 1


# ---------------------------------------------------------------------------
# Schematic.embed_lib_symbol
# ---------------------------------------------------------------------------


class TestEmbedLibSymbol:
    def test_embed_new_symbol(self, tmp_path: Path):
        sch = Schematic.load(_write_schematic(tmp_path))
        # Device:R already exists -- a new one should not duplicate
        lib_sym_r = LibrarySymbol(name="Device:R")
        sch.embed_lib_symbol(lib_sym_r)
        # Count how many Device:R entries there are
        count = sum(1 for s in sch.lib_symbols.find_all("symbol") if s.get_string(0) == "Device:R")
        assert count == 1  # not duplicated

    def test_embed_completely_new_symbol(self, tmp_path: Path):
        sch = Schematic.load(_write_schematic(tmp_path))
        lib_sym = LibrarySymbol(name="Device:C")
        lib_sym.add_pin("1", "~", "passive", (0, 3.81), 270, 1.27)
        lib_sym.add_pin("2", "~", "passive", (0, -3.81), 90, 1.27)
        sch.embed_lib_symbol(lib_sym)

        assert sch.get_lib_symbol("Device:C") is not None


# ---------------------------------------------------------------------------
# snap_to_grid
# ---------------------------------------------------------------------------


class TestSnapToGrid:
    def test_snap_to_1_27(self):
        assert Schematic.snap_to_grid(88.91, 1.27) == pytest.approx(88.9)

    def test_snap_exact(self):
        assert Schematic.snap_to_grid(2.54, 1.27) == pytest.approx(2.54)

    def test_snap_to_2_54(self):
        assert Schematic.snap_to_grid(3.0, 2.54) == pytest.approx(2.54)


class TestSnapAllToGrid:
    def test_snap_symbols_and_wires(self, tmp_path: Path):
        sch = Schematic.load(_write_schematic(tmp_path))
        # Add elements at off-grid positions
        sch.add_symbol(
            "Device:R",
            "R1",
            "10k",
            "SMD:R_0402",
            position=(88.91, 101.5),
            pin_numbers=["1", "2"],
        )
        sch.add_wire((88.91, 101.5), (92.0, 101.5))

        sch.snap_all_to_grid(1.27)

        # Reload from sexp to check underlying values
        sym = sch.symbols[0]
        assert sym.position[0] == pytest.approx(88.9)
        assert sym.position[1] == pytest.approx(101.6)

        wire = sch.wires[0]
        assert wire.start[0] == pytest.approx(88.9)
        assert wire.end[0] == pytest.approx(91.44)


# ---------------------------------------------------------------------------
# Round-trip: save then reload
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_add_and_save_reload(self, tmp_path: Path):
        """load -> add_symbol -> add_wire -> save -> load preserves new elements."""
        path = _write_schematic(tmp_path)
        sch = Schematic.load(path)

        sch.add_symbol("Device:R", "R1", "10k", "SMD:R_0402", (100, 100))
        sch.add_wire((90, 100), (100, 100))

        out_path = tmp_path / "output.kicad_sch"
        sch.save(out_path)

        sch2 = Schematic.load(out_path)
        assert len(sch2.symbols) == 1
        assert sch2.symbols[0].reference == "R1"
        assert len(sch2.wires) == 1
        assert sch2.wires[0].start == (90, 100)
        assert sch2.wires[0].end == (100, 100)

    def test_preserves_existing_content(self, minimal_schematic: Path):
        """Adding elements to a real schematic preserves existing content."""
        sch = Schematic.load(minimal_schematic)
        original_sym_count = len(sch.symbols)
        original_wire_count = len(sch.wires)
        original_label_count = len(sch.labels)

        # Add a symbol with explicit pins (lib_symbols may be empty in minimal)
        sch.add_symbol(
            "Device:R",
            "R99",
            "1M",
            "SMD:R_0402",
            position=(150, 150),
            pin_numbers=["1", "2"],
        )
        sch.add_wire((140, 150), (150, 150))

        out = minimal_schematic.parent / "round_trip.kicad_sch"
        sch.save(out)

        sch2 = Schematic.load(out)
        assert len(sch2.symbols) == original_sym_count + 1
        assert len(sch2.wires) == original_wire_count + 1
        assert len(sch2.labels) == original_label_count
