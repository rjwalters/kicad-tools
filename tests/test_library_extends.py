"""Tests for derived symbol (extends) resolution in the schema library module.

Verifies that:
- resolve_extends() copies base pins/graphics to derived symbols
- Multi-level extends chains resolve correctly
- Circular extends chains raise ValueError
- Missing base symbols leave the derived symbol unresolved (no crash)
- Derived symbols with own pins are not overwritten
- LibraryManager.load_embedded() resolves extends automatically
- SymbolLibrary.load() resolves extends automatically
- Schematic.get_lib_symbol_resolved() resolves extends
- Internal methods (pin_count, get_pin, get_all_pin_positions) work after resolution
"""

from __future__ import annotations

import pytest

from kicad_tools.schema.library import (
    LibraryManager,
    LibraryPin,
    LibrarySymbol,
    SymbolLibrary,
    SymbolPolyline,
    resolve_extends,
)
from kicad_tools.sexp import parse_string


def _make_pin(number: str, name: str, pin_type: str = "passive") -> LibraryPin:
    """Create a simple test pin."""
    return LibraryPin(
        number=number,
        name=name,
        type=pin_type,
        position=(0.0, float(int(number)) * 2.54 if number.isdigit() else 0.0),
        rotation=0,
        length=2.54,
    )


def _make_polyline() -> SymbolPolyline:
    """Create a simple test polyline."""
    return SymbolPolyline(points=[(0, 0), (5, 0), (5, 5), (0, 5), (0, 0)])


class TestResolveExtends:
    """Tests for the resolve_extends() function."""

    def test_basic_extends_resolution(self):
        """Derived symbol with extends gets base symbol's pins."""
        base = LibrarySymbol(
            name="OpAmp",
            pins=[_make_pin("1", "+"), _make_pin("2", "-"), _make_pin("3", "OUT")],
            graphics=[_make_polyline()],
        )
        derived = LibrarySymbol(name="LM358", extends="OpAmp")

        symbols = {"OpAmp": base, "LM358": derived}
        resolve_extends(symbols)

        assert len(derived.pins) == 3
        assert derived.pins[0].name == "+"
        assert derived.pins[1].name == "-"
        assert derived.pins[2].name == "OUT"
        assert len(derived.graphics) == 1

    def test_multi_level_extends(self):
        """A -> B -> C chain resolves correctly."""
        base = LibrarySymbol(
            name="Base",
            pins=[_make_pin("1", "P1"), _make_pin("2", "P2")],
        )
        mid = LibrarySymbol(name="Mid", extends="Base")
        leaf = LibrarySymbol(name="Leaf", extends="Mid")

        symbols = {"Base": base, "Mid": mid, "Leaf": leaf}
        resolve_extends(symbols)

        # Both mid and leaf should have base's pins
        assert len(mid.pins) == 2
        assert len(leaf.pins) == 2
        assert leaf.pins[0].name == "P1"

    def test_circular_extends_raises(self):
        """Circular extends chain raises ValueError."""
        a = LibrarySymbol(name="A", extends="B")
        b = LibrarySymbol(name="B", extends="A")

        symbols = {"A": a, "B": b}
        with pytest.raises(ValueError, match="Circular extends chain"):
            resolve_extends(symbols)

    def test_missing_base_leaves_unresolved(self):
        """If base symbol is not in the dict, derived stays empty."""
        derived = LibrarySymbol(name="Derived", extends="Missing")

        symbols = {"Derived": derived}
        resolve_extends(symbols)

        assert len(derived.pins) == 0

    def test_derived_with_own_pins_not_overwritten(self):
        """Derived symbol that already has pins is not overwritten."""
        base = LibrarySymbol(
            name="Base",
            pins=[_make_pin("1", "B1"), _make_pin("2", "B2")],
        )
        derived = LibrarySymbol(
            name="Derived",
            extends="Base",
            pins=[_make_pin("1", "D1")],
        )

        symbols = {"Base": base, "Derived": derived}
        resolve_extends(symbols)

        # Should keep its own pins
        assert len(derived.pins) == 1
        assert derived.pins[0].name == "D1"

    def test_qualified_name_resolution(self):
        """Base name matches against qualified key (lib:symbol)."""
        base = LibrarySymbol(
            name="Amplifier_Operational:OpAmp",
            pins=[_make_pin("1", "+"), _make_pin("2", "-")],
        )
        derived = LibrarySymbol(name="LM358", extends="OpAmp")

        symbols = {"Amplifier_Operational:OpAmp": base, "LM358": derived}
        resolve_extends(symbols)

        assert len(derived.pins) == 2

    def test_no_extends_symbols_unaffected(self):
        """Symbols without extends are left unchanged."""
        sym = LibrarySymbol(
            name="Resistor",
            pins=[_make_pin("1", "1"), _make_pin("2", "2")],
        )

        symbols = {"Resistor": sym}
        resolve_extends(symbols)

        assert len(sym.pins) == 2


class TestLibrarySymbolInternalMethods:
    """Verify internal methods work correctly after extends resolution."""

    def test_pin_count_after_resolution(self):
        """pin_count returns base pin count after resolution."""
        base = LibrarySymbol(
            name="Base",
            pins=[_make_pin("1", "A"), _make_pin("2", "B"), _make_pin("3", "C")],
        )
        derived = LibrarySymbol(name="Derived", extends="Base")

        assert derived.pin_count == 0  # Before resolution

        resolve_extends({"Base": base, "Derived": derived})

        assert derived.pin_count == 3  # After resolution

    def test_get_pin_after_resolution(self):
        """get_pin() finds inherited pins."""
        base = LibrarySymbol(
            name="Base",
            pins=[_make_pin("1", "VCC"), _make_pin("2", "GND")],
        )
        derived = LibrarySymbol(name="Derived", extends="Base")

        resolve_extends({"Base": base, "Derived": derived})

        pin = derived.get_pin("1")
        assert pin is not None
        assert pin.name == "VCC"

        assert derived.get_pin("99") is None

    def test_get_pins_by_name_after_resolution(self):
        """get_pins_by_name() finds inherited pins."""
        base = LibrarySymbol(
            name="Base",
            pins=[_make_pin("1", "GND"), _make_pin("2", "VCC"), _make_pin("3", "GND")],
        )
        derived = LibrarySymbol(name="Derived", extends="Base")

        resolve_extends({"Base": base, "Derived": derived})

        gnd_pins = derived.get_pins_by_name("GND")
        assert len(gnd_pins) == 2

    def test_get_all_pin_positions_after_resolution(self):
        """get_all_pin_positions() returns positions for inherited pins."""
        base = LibrarySymbol(
            name="Base",
            pins=[
                LibraryPin(
                    number="1",
                    name="A",
                    type="passive",
                    position=(2.54, 0),
                    rotation=180,
                    length=2.54,
                ),
                LibraryPin(
                    number="2",
                    name="B",
                    type="passive",
                    position=(-2.54, 0),
                    rotation=0,
                    length=2.54,
                ),
            ],
        )
        derived = LibrarySymbol(name="Derived", extends="Base")

        resolve_extends({"Base": base, "Derived": derived})

        positions = derived.get_all_pin_positions(
            instance_pos=(10.0, 20.0),
            instance_rot=0,
        )
        assert len(positions) == 2
        assert "1" in positions
        assert "2" in positions


class TestLibraryManagerLoadEmbedded:
    """Verify LibraryManager.load_embedded() resolves extends."""

    def test_embedded_extends_resolved(self):
        """Embedded derived symbols get their base's pins."""
        # Build a minimal lib_symbols sexp with base and derived symbol
        sexp_text = """(lib_symbols
          (symbol "mylib:OpAmp"
            (symbol "OpAmp_0_1"
              (polyline (pts (xy 0 0) (xy 5 0)) (stroke (width 0) (type default)) (fill (type none)))
            )
            (symbol "OpAmp_1_1"
              (pin input line (at -5.08 2.54 0) (length 2.54) (name "+" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
              (pin input line (at -5.08 -2.54 0) (length 2.54) (name "-" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
              (pin output line (at 5.08 0 180) (length 2.54) (name "OUT" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
            )
          )
          (symbol "mylib:LM358"
            (extends "OpAmp")
            (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (property "Value" "LM358" (at 0 0 0) (effects (font (size 1.27 1.27))))
          )
        )"""
        lib_symbols = parse_string(sexp_text)

        class FakeSchematic:
            @property
            def lib_symbols(self):
                return lib_symbols

        mgr = LibraryManager()
        mgr.load_embedded(FakeSchematic())

        derived = mgr.get_symbol("mylib:LM358")
        assert derived is not None
        assert derived.extends == "OpAmp"
        assert derived.pin_count == 3
        assert derived.get_pin("1") is not None
        assert derived.get_pin("1").name == "+"

    def test_embedded_base_without_extends_unaffected(self):
        """Base symbols without extends keep their own pins."""
        sexp_text = """(lib_symbols
          (symbol "mylib:OpAmp"
            (symbol "OpAmp_1_1"
              (pin input line (at 0 0 0) (length 2.54) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
            )
          )
        )"""
        lib_symbols = parse_string(sexp_text)

        class FakeSchematic:
            @property
            def lib_symbols(self):
                return lib_symbols

        mgr = LibraryManager()
        mgr.load_embedded(FakeSchematic())

        sym = mgr.get_symbol("mylib:OpAmp")
        assert sym is not None
        assert sym.pin_count == 1
        assert sym.extends is None


class TestSymbolLibraryLoad:
    """Verify SymbolLibrary.load() resolves extends."""

    def test_load_resolves_extends(self, tmp_path):
        """Symbols loaded from .kicad_sym file have extends resolved."""
        lib_content = """(kicad_symbol_lib
          (version 20231120)
          (generator "kicad_symbol_editor")
          (symbol "OpAmp"
            (symbol "OpAmp_1_1"
              (pin input line (at -5.08 2.54 0) (length 2.54) (name "+" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
              (pin input line (at -5.08 -2.54 0) (length 2.54) (name "-" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
              (pin output line (at 5.08 0 180) (length 2.54) (name "OUT" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
            )
          )
          (symbol "LM358"
            (extends "OpAmp")
            (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (property "Value" "LM358" (at 0 0 0) (effects (font (size 1.27 1.27))))
          )
        )"""
        lib_path = tmp_path / "test.kicad_sym"
        lib_path.write_text(lib_content)

        lib = SymbolLibrary.load(str(lib_path))

        derived = lib.get_symbol("LM358")
        assert derived is not None
        assert derived.extends == "OpAmp"
        assert derived.pin_count == 3
        assert derived.get_pin("1").name == "+"
        assert derived.get_pin("3").name == "OUT"


class TestSchematicGetLibSymbolResolved:
    """Verify Schematic.get_lib_symbol_resolved() resolves extends."""

    def test_resolved_method_returns_pins_for_derived(self, tmp_path):
        """get_lib_symbol_resolved returns inherited pins for derived symbols."""
        # Create a minimal schematic with embedded lib_symbols
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "kicad_tools")
          (lib_symbols
            (symbol "mylib:OpAmp"
              (symbol "OpAmp_1_1"
                (pin input line (at -5.08 2.54 0) (length 2.54) (name "+" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
                (pin input line (at -5.08 -2.54 0) (length 2.54) (name "-" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
                (pin output line (at 5.08 0 180) (length 2.54) (name "OUT" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
              )
            )
            (symbol "mylib:LM358"
              (extends "OpAmp")
              (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
              (property "Value" "LM358" (at 0 0 0) (effects (font (size 1.27 1.27))))
            )
          )
        )"""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text(sch_content)

        from kicad_tools.schema import Schematic

        sch = Schematic.load(str(sch_path))

        # Non-derived symbol
        base = sch.get_lib_symbol_resolved("mylib:OpAmp")
        assert base is not None
        assert base.pin_count == 3

        # Derived symbol
        derived = sch.get_lib_symbol_resolved("mylib:LM358")
        assert derived is not None
        assert derived.pin_count == 3
        assert derived.get_pin("1").name == "+"

    def test_resolved_method_returns_none_for_missing(self, tmp_path):
        """get_lib_symbol_resolved returns None for missing lib_id."""
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "kicad_tools")
          (lib_symbols)
        )"""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text(sch_content)

        from kicad_tools.schema import Schematic

        sch = Schematic.load(str(sch_path))

        result = sch.get_lib_symbol_resolved("nonexistent:Symbol")
        assert result is None
