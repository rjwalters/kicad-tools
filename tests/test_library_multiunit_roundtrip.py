"""Regression tests for multi-unit symbol serialization (issue #3874).

When a ``SymbolLibrary``'s cached ``._sexp`` is present, ``save()``
round-trips multi-unit symbols by re-emitting the cached tree verbatim.
The bug exercised here is the *from-scratch* path (``_sexp = None``), where
the s-expression must be regenerated from the dataclass model.

Previously, the from-scratch path corrupted multi-unit symbols:
  - pins were flattened onto the parent symbol (all defaulting to unit 1),
  - the ``units`` count was never parsed (stayed at the default of 1),
  - nested ``_N_1`` sub-symbols were re-parsed as spurious empty top-level
    symbols, and
  - a spurious ``_0_1`` decoration block could appear.

These tests assert the from-scratch regeneration reproduces the original
multi-unit structure.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.schema.library import SymbolLibrary

FIXTURE = Path(__file__).parent / "fixtures" / "multiunit_test.kicad_sym"


def _pin_unit_map(symbol) -> dict[str, int]:
    """Map each pin number to its unit index."""
    return {pin.number: pin.unit for pin in symbol.pins}


class TestMultiUnitRoundtrip:
    """From-scratch (``_sexp = None``) round-trip of multi-unit symbols."""

    def test_load_parses_units_and_pin_units(self):
        """Loading a multi-unit symbol records unit count and per-pin unit."""
        lib = SymbolLibrary.load(str(FIXTURE))

        # Only the top-level symbol is a library entry -- the nested _N_1
        # sub-symbols must NOT appear as standalone symbols.
        assert set(lib.symbols.keys()) == {"MultiUnitPart"}

        symbol = lib.get_symbol("MultiUnitPart")
        assert symbol is not None
        assert symbol.units == 3
        assert symbol.pin_count == 6

        # Pins 1-2 -> unit 1, 3-4 -> unit 2, 5-6 -> unit 3.
        assert _pin_unit_map(symbol) == {
            "1": 1,
            "2": 1,
            "3": 2,
            "4": 2,
            "5": 3,
            "6": 3,
        }

    def test_from_scratch_save_preserves_units(self, tmp_path):
        """Clearing _sexp and saving reparses to an equivalent model."""
        lib = SymbolLibrary.load(str(FIXTURE))
        original = lib.get_symbol("MultiUnitPart")
        original_unit_map = _pin_unit_map(original)
        original_units = original.units

        # Force the from-scratch serialization path.
        lib._sexp = None
        out = tmp_path / "multiunit_out.kicad_sym"
        lib.save(str(out))

        # Reload the regenerated file and compare structure.
        reloaded = SymbolLibrary.load(str(out))

        # No spurious top-level symbols (e.g. _1_1, _0_1) leaked into the lib.
        assert set(reloaded.symbols.keys()) == {"MultiUnitPart"}

        reloaded_symbol = reloaded.get_symbol("MultiUnitPart")
        assert reloaded_symbol is not None
        assert reloaded_symbol.units == original_units == 3
        assert _pin_unit_map(reloaded_symbol) == original_unit_map

    def test_from_scratch_no_spurious_0_1_block(self, tmp_path):
        """A graphics-free multi-unit symbol emits no _0_1 decoration block."""
        lib = SymbolLibrary.load(str(FIXTURE))
        lib._sexp = None
        out = tmp_path / "multiunit_out.kicad_sym"
        lib.save(str(out))

        text = out.read_text()
        # No _0_1 graphics sub-symbol should appear for a pin-only symbol.
        assert "MultiUnitPart_0_1" not in text
        # All three pin units must be present.
        assert "MultiUnitPart_1_1" in text
        assert "MultiUnitPart_2_1" in text
        assert "MultiUnitPart_3_1" in text

    def test_from_scratch_pins_under_correct_units(self, tmp_path):
        """Each pin is emitted under its own _N_1 unit sub-symbol."""
        lib = SymbolLibrary.load(str(FIXTURE))
        lib._sexp = None
        out = tmp_path / "multiunit_out.kicad_sym"
        lib.save(str(out))

        # Reparse the regenerated tree and confirm each unit sub-symbol holds
        # exactly its original pins.
        reloaded = SymbolLibrary.load(str(out))
        symbol = reloaded.get_symbol("MultiUnitPart")

        unit1 = {p.number for p in symbol.get_pins_for_unit(1)}
        unit2 = {p.number for p in symbol.get_pins_for_unit(2)}
        unit3 = {p.number for p in symbol.get_pins_for_unit(3)}

        assert unit1 == {"1", "2"}
        assert unit2 == {"3", "4"}
        assert unit3 == {"5", "6"}


class TestSingleUnitRoundtrip:
    """Single-unit symbols must not regress."""

    SINGLE_UNIT = """(kicad_symbol_lib
        (version 20231120)
        (generator "kicad_symbol_editor")
        (symbol "SinglePart"
            (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (property "Value" "SinglePart" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (symbol "SinglePart_1_1"
                (pin input line (at -5.08 2.54 0) (length 2.54) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
                (pin output line (at 5.08 0 180) (length 2.54) (name "OUT" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
            )
        )
    )"""

    def test_single_unit_from_scratch(self, tmp_path):
        """A single-unit symbol stays single-unit through a from-scratch save."""
        src = tmp_path / "single.kicad_sym"
        src.write_text(self.SINGLE_UNIT)

        lib = SymbolLibrary.load(str(src))
        symbol = lib.get_symbol("SinglePart")
        assert symbol.units == 1
        assert all(pin.unit == 1 for pin in symbol.pins)

        lib._sexp = None
        out = tmp_path / "single_out.kicad_sym"
        lib.save(str(out))

        reloaded = SymbolLibrary.load(str(out))
        assert set(reloaded.symbols.keys()) == {"SinglePart"}
        reloaded_symbol = reloaded.get_symbol("SinglePart")
        assert reloaded_symbol.units == 1
        assert reloaded_symbol.pin_count == 2
        # No spurious _0_1 for a graphics-free symbol.
        assert "SinglePart_0_1" not in out.read_text()


class TestGraphicsDecorationRoundtrip:
    """Symbols with _0_1 graphics decoration round-trip unchanged."""

    WITH_GRAPHICS = """(kicad_symbol_lib
        (version 20231120)
        (generator "kicad_symbol_editor")
        (symbol "GfxPart"
            (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (property "Value" "GfxPart" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (symbol "GfxPart_0_1"
                (rectangle (start -5.08 5.08) (end 5.08 -5.08)
                    (stroke (width 0.254) (type default))
                    (fill (type background)))
            )
            (symbol "GfxPart_1_1"
                (pin input line (at -7.62 0 0) (length 2.54) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
            )
        )
    )"""

    def test_graphics_decoration_from_scratch(self, tmp_path):
        """A _0_1 decoration block survives a from-scratch save."""
        src = tmp_path / "gfx.kicad_sym"
        src.write_text(self.WITH_GRAPHICS)

        lib = SymbolLibrary.load(str(src))
        symbol = lib.get_symbol("GfxPart")
        # Graphics in _0_1 are unit 0 and must not be counted as a pin unit.
        assert symbol.units == 1
        assert len(symbol.graphics) == 1
        assert symbol.pin_count == 1

        lib._sexp = None
        out = tmp_path / "gfx_out.kicad_sym"
        lib.save(str(out))

        text = out.read_text()
        # The graphics block is re-emitted under _0_1, pins under _1_1.
        assert "GfxPart_0_1" in text
        assert "GfxPart_1_1" in text

        reloaded = SymbolLibrary.load(str(out))
        assert set(reloaded.symbols.keys()) == {"GfxPart"}
        reloaded_symbol = reloaded.get_symbol("GfxPart")
        assert reloaded_symbol.units == 1
        assert reloaded_symbol.pin_count == 1
        assert len(reloaded_symbol.graphics) == 1


class TestUnderscoreNameRoundtrip:
    """Symbol names containing underscores parse units correctly (rsplit)."""

    UNDERSCORE_NAME = """(kicad_symbol_lib
        (version 20231120)
        (generator "kicad_symbol_editor")
        (symbol "My_Part_X"
            (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (property "Value" "My_Part_X" (at 0 0 0) (effects (font (size 1.27 1.27))))
            (symbol "My_Part_X_1_1"
                (pin input line (at -5.08 2.54 0) (length 2.54) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
            )
            (symbol "My_Part_X_2_1"
                (pin output line (at 5.08 0 180) (length 2.54) (name "B" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
            )
        )
    )"""

    def test_underscore_name_units(self, tmp_path):
        """Units parse correctly when the symbol name contains underscores."""
        src = tmp_path / "underscore.kicad_sym"
        src.write_text(self.UNDERSCORE_NAME)

        lib = SymbolLibrary.load(str(src))
        symbol = lib.get_symbol("My_Part_X")
        assert symbol is not None
        assert symbol.units == 2
        assert {p.number: p.unit for p in symbol.pins} == {"1": 1, "2": 2}

        lib._sexp = None
        out = tmp_path / "underscore_out.kicad_sym"
        lib.save(str(out))

        reloaded = SymbolLibrary.load(str(out))
        assert set(reloaded.symbols.keys()) == {"My_Part_X"}
        reloaded_symbol = reloaded.get_symbol("My_Part_X")
        assert reloaded_symbol.units == 2
        assert {p.number: p.unit for p in reloaded_symbol.pins} == {"1": 1, "2": 2}
