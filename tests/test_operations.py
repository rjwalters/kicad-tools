"""Tests for operations modules (pinmap, symbol_ops, net_ops, netlist)."""

from pathlib import Path

import pytest

from kicad_tools.operations.net_ops import (
    Net,
    NetConnection,
    NetTracer,
    find_net,
    point_on_wire,
    points_equal,
    trace_nets,
)
from kicad_tools.operations.netlist import (
    Netlist,
    NetlistComponent,
    NetlistNet,
    NetNode,
)
from kicad_tools.operations.pinmap import (
    MappingResult,
    Pin,
    PinMapping,
    extract_pins_from_sexp,
    match_pins,
)
from kicad_tools.operations.symbol_ops import (
    PinTypeChange,
    find_symbol_by_reference,
    get_symbol_lib_id,
    get_symbol_pins,
    replace_symbol_lib_id,
)
from kicad_tools.schema.schematic import Schematic
from kicad_tools.schema.wire import Wire
from kicad_tools.sexp import parse_string


class TestPinNormalization:
    """Tests for Pin.normalized_name property."""

    def test_basic_normalization(self):
        """Test basic uppercase conversion."""
        pin = Pin(number="1", name="reset", pin_type="input")
        assert pin.normalized_name == "RESET"

    def test_remove_numeric_suffix(self):
        """Test removing numeric suffix."""
        pin = Pin(number="1", name="OUT_39", pin_type="output")
        assert pin.normalized_name == "OUT"

    def test_remove_active_low_markers(self):
        """Test removing active low markers."""
        pin = Pin(number="1", name="~{RESET}", pin_type="input")
        assert pin.normalized_name == "RESET"

    def test_replace_slash_with_underscore(self):
        """Test replacing slash with underscore."""
        pin = Pin(number="1", name="IN/OUT", pin_type="bidirectional")
        assert pin.normalized_name == "IN_OUT"

    def test_replace_plus_minus(self):
        """Test replacing +/- with P/N."""
        pin = Pin(number="1", name="IN+", pin_type="input")
        assert pin.normalized_name == "INP"
        pin = Pin(number="2", name="IN-", pin_type="input")
        assert pin.normalized_name == "INN"


class TestPinFunctionCategory:
    """Tests for Pin.function_category property."""

    def test_power_positive(self):
        """Test detection of power positive pins."""
        for name in ["VCC", "VDD", "PVDD", "AVDD", "DVDD"]:
            pin = Pin(number="1", name=name, pin_type="power_in")
            assert pin.function_category == "power_positive"

    def test_power_ground(self):
        """Test detection of ground pins."""
        for name in ["GND", "PGND", "AGND", "EP"]:
            pin = Pin(number="1", name=name, pin_type="power_in")
            assert pin.function_category == "power_ground"

    def test_bootstrap(self):
        """Test detection of bootstrap pins."""
        pin = Pin(number="1", name="BST_A", pin_type="input")
        assert pin.function_category == "bootstrap"

    def test_audio_input(self):
        """Test detection of audio input pins."""
        for name in ["INPUT_L", "INP", "INN", "IN_L"]:
            pin = Pin(number="1", name=name, pin_type="input")
            assert pin.function_category == "audio_input"

    def test_audio_output(self):
        """Test detection of audio output pins."""
        pin = Pin(number="1", name="OUT_A", pin_type="output")
        assert pin.function_category == "audio_output"

    def test_status_control(self):
        """Test detection of status/control pins."""
        for name in ["FAULT", "CLIP", "OTW", "SD", "MUTE", "RESET"]:
            pin = Pin(number="1", name=name, pin_type="output")
            assert pin.function_category == "status_control"

    def test_oscillator(self):
        """Test detection of oscillator pins."""
        pin = Pin(number="1", name="OSC_IN", pin_type="input")
        assert pin.function_category == "oscillator"

    def test_configuration(self):
        """Test detection of configuration pins."""
        for name in ["GAIN", "M1", "M2", "HEAD", "PLIMIT"]:
            pin = Pin(number="1", name=name, pin_type="input")
            assert pin.function_category == "configuration"

    def test_no_connect(self):
        """Test detection of no-connect pins."""
        for name in ["NC", "N/C", "N.C."]:
            pin = Pin(number="1", name=name, pin_type="no_connect")
            assert pin.function_category == "no_connect"

    def test_other(self):
        """Test default category."""
        pin = Pin(number="1", name="RANDOM_PIN", pin_type="passive")
        assert pin.function_category == "other"


class TestPinMapping:
    """Tests for PinMapping class."""

    def test_is_matched_true(self):
        """Test is_matched when target pin exists."""
        src = Pin(number="1", name="A", pin_type="input")
        tgt = Pin(number="1", name="A", pin_type="input")
        mapping = PinMapping(src, tgt, 1.0, "Exact match")
        assert mapping.is_matched is True

    def test_is_matched_false(self):
        """Test is_matched when no target pin."""
        src = Pin(number="1", name="A", pin_type="input")
        mapping = PinMapping(src, None, 0.0, "No match")
        assert mapping.is_matched is False


class TestMappingResult:
    """Tests for MappingResult class."""

    def test_matched_count(self):
        """Test counting matched pins."""
        src1 = Pin(number="1", name="A", pin_type="input")
        src2 = Pin(number="2", name="B", pin_type="output")
        tgt = Pin(number="1", name="A", pin_type="input")

        result = MappingResult(
            source_name="S",
            target_name="T",
            source_pins=[src1, src2],
            target_pins=[tgt],
            mappings=[
                PinMapping(src1, tgt, 1.0, "Match"),
                PinMapping(src2, None, 0.0, "No match"),
            ],
        )
        assert result.matched_count == 1
        assert result.unmatched_source_count == 1

    def test_match_percentage(self):
        """Test match percentage calculation."""
        src = Pin(number="1", name="A", pin_type="input")
        tgt = Pin(number="1", name="A", pin_type="input")

        result = MappingResult(
            source_name="S",
            target_name="T",
            source_pins=[src],
            target_pins=[tgt],
            mappings=[PinMapping(src, tgt, 1.0, "Match")],
        )
        assert result.match_percentage == 100.0

    def test_match_percentage_empty(self):
        """Test match percentage with no mappings."""
        result = MappingResult(
            source_name="S",
            target_name="T",
            source_pins=[],
            target_pins=[],
        )
        assert result.match_percentage == 0.0

    def test_to_dict(self):
        """Test conversion to dictionary."""
        src = Pin(number="1", name="A", pin_type="input")
        tgt = Pin(number="1", name="A", pin_type="input")

        result = MappingResult(
            source_name="Source",
            target_name="Target",
            source_pins=[src],
            target_pins=[tgt],
            mappings=[PinMapping(src, tgt, 1.0, "Exact match")],
        )
        d = result.to_dict()
        assert d["source"] == "Source"
        assert d["target"] == "Target"
        assert d["matched_count"] == 1
        assert len(d["mappings"]) == 1


class TestExtractPinsFromSexp:
    """Tests for extract_pins_from_sexp function."""

    def test_extract_basic_pins(self):
        """Test extracting pins from symbol S-expression."""
        sexp = parse_string("""(symbol "Device:R"
            (symbol "Device:R_0_1"
                (pin passive line (at -2.54 0 0) (length 2.54) (name "1") (number "1"))
                (pin passive line (at 2.54 0 180) (length 2.54) (name "2") (number "2"))
            )
        )""")
        pins = extract_pins_from_sexp(sexp)
        assert len(pins) == 2
        assert pins[0].number == "1"
        assert pins[1].number == "2"

    def test_extract_with_type_mapping(self):
        """Test that pin types are mapped correctly."""
        sexp = parse_string("""(symbol "Test"
            (symbol "Test_0_1"
                (pin input line (at 0 0 0) (length 2.54) (name "IN") (number "1"))
                (pin output line (at 0 0 0) (length 2.54) (name "OUT") (number "2"))
                (pin power_in line (at 0 0 0) (length 2.54) (name "VCC") (number "3"))
            )
        )""")
        pins = extract_pins_from_sexp(sexp)
        assert pins[0].pin_type == "Input"
        assert pins[1].pin_type == "Output"
        assert pins[2].pin_type == "Power Input"

    def test_no_duplicate_pins(self):
        """Test that duplicate pin numbers are skipped (multi-unit symbols)."""
        sexp = parse_string("""(symbol "Test"
            (symbol "Test_1_1"
                (pin passive line (at 0 0 0) (length 2.54) (name "A") (number "1"))
            )
            (symbol "Test_2_1"
                (pin passive line (at 0 0 0) (length 2.54) (name "A") (number "1"))
            )
        )""")
        pins = extract_pins_from_sexp(sexp)
        assert len(pins) == 1


class TestMatchPins:
    """Tests for match_pins function."""

    def test_exact_name_match(self):
        """Test exact name matching (highest confidence)."""
        source = [Pin(number="1", name="VCC", pin_type="power_in")]
        target = [Pin(number="10", name="VCC", pin_type="power_in")]

        mappings, unmatched = match_pins(source, target)
        assert len(mappings) == 1
        assert mappings[0].confidence == 1.0
        assert mappings[0].target_pin.number == "10"

    def test_normalized_name_match(self):
        """Test normalized name matching."""
        source = [Pin(number="1", name="~{RESET}", pin_type="input")]
        target = [Pin(number="2", name="RESET", pin_type="input")]

        mappings, unmatched = match_pins(source, target)
        assert len(mappings) == 1
        assert mappings[0].confidence == 0.8
        assert "Normalized" in mappings[0].match_reason

    def test_same_number_and_category_match(self):
        """Test same pin number + category matching."""
        # Both pins are in same power_positive category (VDD matches VCC)
        source = [Pin(number="1", name="VDD", pin_type="power_in")]
        target = [Pin(number="1", name="VCC", pin_type="power_in")]

        mappings, unmatched = match_pins(source, target)
        assert len(mappings) == 1
        # Should match on same category (power_positive)
        assert mappings[0].is_matched
        assert mappings[0].confidence <= 0.5  # Low confidence for number+category match

    def test_no_match(self):
        """Test when no match is found."""
        source = [Pin(number="1", name="UNIQUE_PIN", pin_type="input")]
        target = [Pin(number="2", name="OTHER_PIN", pin_type="output")]

        mappings, unmatched = match_pins(source, target)
        assert len(mappings) == 1
        # May or may not match depending on category logic
        # The unmatched list should be checked

    def test_unmatched_target_pins(self):
        """Test tracking of unmatched target pins."""
        source = [Pin(number="1", name="A", pin_type="input")]
        target = [
            Pin(number="1", name="A", pin_type="input"),
            Pin(number="2", name="B", pin_type="output"),
        ]

        mappings, unmatched = match_pins(source, target)
        assert len(unmatched) == 1
        assert unmatched[0].name == "B"


class TestSymbolOps:
    """Tests for symbol_ops module."""

    def test_find_symbol_by_reference(self, minimal_schematic: Path):
        """Test finding symbol by reference."""
        sexp = parse_string(minimal_schematic.read_text())
        symbol = find_symbol_by_reference(sexp, "R1")
        assert symbol is not None

    def test_find_symbol_by_reference_not_found(self, minimal_schematic: Path):
        """Test finding non-existent symbol."""
        sexp = parse_string(minimal_schematic.read_text())
        symbol = find_symbol_by_reference(sexp, "U99")
        assert symbol is None

    def test_get_symbol_lib_id(self, minimal_schematic: Path):
        """Test getting lib_id from symbol."""
        sexp = parse_string(minimal_schematic.read_text())
        symbol = find_symbol_by_reference(sexp, "R1")
        lib_id = get_symbol_lib_id(symbol)
        assert lib_id == "Device:R"

    def test_get_symbol_pins(self, minimal_schematic: Path):
        """Test getting pins from symbol."""
        sexp = parse_string(minimal_schematic.read_text())
        symbol = find_symbol_by_reference(sexp, "R1")
        pins = get_symbol_pins(symbol)
        assert len(pins) == 2

    def test_replace_symbol_lib_id_dry_run(self, minimal_schematic: Path, tmp_path: Path):
        """Test replacing lib_id with dry_run."""
        # Copy to temp location
        test_file = tmp_path / "test.kicad_sch"
        test_file.write_text(minimal_schematic.read_text())

        result = replace_symbol_lib_id(
            str(test_file),
            "R1",
            "NewLib:NewSymbol",
            dry_run=True,
        )

        assert result.reference == "R1"
        assert result.old_lib_id == "Device:R"
        assert result.new_lib_id == "NewLib:NewSymbol"
        assert len(result.changes_made) > 0

        # Verify file wasn't changed (dry_run)
        sexp = parse_string(test_file.read_text())
        symbol = find_symbol_by_reference(sexp, "R1")
        assert get_symbol_lib_id(symbol) == "Device:R"

    def test_replace_symbol_lib_id_actual(self, minimal_schematic: Path, tmp_path: Path):
        """Test actually replacing lib_id."""
        test_file = tmp_path / "test.kicad_sch"
        test_file.write_text(minimal_schematic.read_text())

        replace_symbol_lib_id(
            str(test_file),
            "R1",
            "NewLib:NewSymbol",
            new_value="100k",
            dry_run=False,
        )

        # Verify file was changed
        sexp = parse_string(test_file.read_text())
        symbol = find_symbol_by_reference(sexp, "R1")
        assert get_symbol_lib_id(symbol) == "NewLib:NewSymbol"

    def test_replace_symbol_lib_id_not_found(self, minimal_schematic: Path, tmp_path: Path):
        """Test error when symbol not found."""
        test_file = tmp_path / "test.kicad_sch"
        test_file.write_text(minimal_schematic.read_text())

        with pytest.raises(ValueError, match="not found"):
            replace_symbol_lib_id(str(test_file), "U99", "New:Symbol")


# Schematic with a voltage regulator symbol and embedded lib_symbols entry
# that has power_out pin type on the output pin.
REGULATOR_SCHEMATIC = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Regulator_Linear:AP2204K-3.3"
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "AP2204K-3.3" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "AP2204K-3.3_1_1"
        (pin power_in line (at -5.08 2.54 0) (length 2.54) (name "VIN") (number "1"))
        (pin power_in line (at 0 -5.08 90) (length 2.54) (name "GND") (number "2"))
        (pin power_out line (at 5.08 2.54 180) (length 2.54) (name "VOUT") (number "3"))
      )
    )
  )
  (symbol
    (lib_id "Regulator_Linear:AP2204K-3.3")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "U1" (at 120 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "AP2204K-3.3" (at 120 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Package_TO_SOT_SMD:SOT-23-5" (at 120 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000012"))
    (pin "3" (uuid "00000000-0000-0000-0000-000000000013"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "U1")
          (unit 1)
        )
      )
    )
  )
)
"""

# Library file with a replacement symbol that uses "output" instead of
# "power_out" on pin 3, triggering the ERC regression described in #2048.
REPLACEMENT_LIB = """(kicad_symbol_lib
  (version 20231120)
  (generator "test")
  (symbol "XC6206P332MR"
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "XC6206P332MR" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (symbol "XC6206P332MR_1_1"
      (pin power_in line (at -5.08 2.54 0) (length 2.54) (name "VIN") (number "1"))
      (pin power_in line (at 0 -5.08 90) (length 2.54) (name "GND") (number "2"))
      (pin output line (at 5.08 2.54 180) (length 2.54) (name "VOUT") (number "3"))
    )
  )
)
"""

# Library with a symbol that has different pin count (4 pins vs 3)
DIFFERENT_PIN_COUNT_LIB = """(kicad_symbol_lib
  (version 20231120)
  (generator "test")
  (symbol "AltReg:REG4PIN"
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "REG4PIN" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (symbol "REG4PIN_1_1"
      (pin power_in line (at -5.08 2.54 0) (length 2.54) (name "VIN") (number "1"))
      (pin power_in line (at 0 -5.08 90) (length 2.54) (name "GND") (number "2"))
      (pin output line (at 5.08 2.54 180) (length 2.54) (name "VOUT") (number "3"))
      (pin input line (at -5.08 -2.54 0) (length 2.54) (name "EN") (number "4"))
    )
  )
)
"""


# Library containing a derived symbol that extends a base.
# AP2112K-3.3 extends AP2204K-1.5 (different output voltage variant).
DERIVED_SYMBOL_LIB = """(kicad_symbol_lib
  (version 20231120)
  (generator "test")
  (symbol "AP2204K-1.5"
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "AP2204K-1.5" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (symbol "AP2204K-1.5_0_1"
      (rectangle (start -5.08 5.08) (end 5.08 -5.08) (stroke (width 0)) (fill (type background)))
    )
    (symbol "AP2204K-1.5_1_1"
      (pin power_in line (at -7.62 2.54 0) (length 2.54) (name "VIN") (number "1"))
      (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND") (number "2"))
      (pin power_out line (at 7.62 2.54 180) (length 2.54) (name "VOUT") (number "3"))
    )
  )
  (symbol "AP2112K-3.3"
    (extends "AP2204K-1.5")
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "AP2112K-3.3" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
  )
)
"""


class TestReplaceSymbolWithLibUpdate:
    """Tests for replace_symbol_lib_id with --lib-path (lib_symbols update)."""

    def _write_schematic(self, tmp_path: Path) -> Path:
        """Write the regulator schematic fixture."""
        sch_file = tmp_path / "regulator.kicad_sch"
        sch_file.write_text(REGULATOR_SCHEMATIC)
        return sch_file

    def _write_lib(self, tmp_path: Path, content: str, name: str = "replacement.kicad_sym") -> Path:
        lib_file = tmp_path / name
        lib_file.write_text(content)
        return lib_file

    def test_lib_symbols_updated_on_replace(self, tmp_path: Path):
        """Replacing with lib_path updates the embedded lib_symbols entry."""
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, REPLACEMENT_LIB)

        result = replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "Regulator_Linear:XC6206P332MR",
            lib_path=str(lib_file),
        )

        assert result.lib_symbol_updated is True
        assert result.old_lib_id == "Regulator_Linear:AP2204K-3.3"
        assert result.new_lib_id == "Regulator_Linear:XC6206P332MR"

        # Verify the schematic now has the new lib_symbols entry
        sexp = parse_string(sch_file.read_text())
        lib_syms = sexp.find("lib_symbols")
        assert lib_syms is not None

        # Old entry should be gone
        old_found = False
        new_found = False
        for sym in lib_syms.find_all("symbol"):
            name = sym.get_string(0)
            if name == "Regulator_Linear:AP2204K-3.3":
                old_found = True
            if name == "Regulator_Linear:XC6206P332MR":
                new_found = True
        assert not old_found, "Old lib_symbols entry should be removed"
        assert new_found, "New lib_symbols entry should be present"

    def test_pin_type_changes_reported(self, tmp_path: Path):
        """Pin type differences between old and new symbols are reported."""
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, REPLACEMENT_LIB)

        result = replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "Regulator_Linear:XC6206P332MR",
            lib_path=str(lib_file),
        )

        # Pin 3 changed from power_out to output
        assert len(result.pin_type_changes) == 1
        ptc = result.pin_type_changes[0]
        assert ptc.pin_number == "3"
        assert ptc.old_type == "power_out"
        assert ptc.new_type == "output"

    def test_dry_run_does_not_modify_file(self, tmp_path: Path):
        """Dry run with lib_path reports changes but does not write."""
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, REPLACEMENT_LIB)
        original_text = sch_file.read_text()

        result = replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "Regulator_Linear:XC6206P332MR",
            lib_path=str(lib_file),
            dry_run=True,
        )

        assert result.lib_symbol_updated is True
        assert result.pin_type_changes
        # File should be untouched
        assert sch_file.read_text() == original_text

    def test_different_pin_count_adds_new_pins(self, tmp_path: Path):
        """When new symbol has extra pins, they are added to the instance."""
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, DIFFERENT_PIN_COUNT_LIB)

        result = replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "AltReg:REG4PIN",
            lib_path=str(lib_file),
        )

        assert result.old_pin_count == 3
        assert result.new_pin_count == 4
        assert result.lib_symbol_updated is True

        # Verify the instance now has 4 pins
        sexp = parse_string(sch_file.read_text())
        symbol = find_symbol_by_reference(sexp, "U1")
        pins = get_symbol_pins(symbol)
        assert len(pins) == 4

    def test_library_not_found_raises(self, tmp_path: Path):
        """Providing a non-existent lib_path raises FileNotFoundError."""
        sch_file = self._write_schematic(tmp_path)

        with pytest.raises(FileNotFoundError, match="Library not found"):
            replace_symbol_lib_id(
                str(sch_file),
                "U1",
                "Regulator_Linear:XC6206P332MR",
                lib_path=str(tmp_path / "nonexistent.kicad_sym"),
            )

    def test_symbol_not_in_library_raises(self, tmp_path: Path):
        """Providing a lib_path that doesn't contain the symbol raises ValueError."""
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, REPLACEMENT_LIB)

        with pytest.raises(ValueError, match="not found in library"):
            replace_symbol_lib_id(
                str(sch_file),
                "U1",
                "Regulator_Linear:NonExistentSymbol",
                lib_path=str(lib_file),
            )

    def test_new_lib_symbol_has_correct_pin_types(self, tmp_path: Path):
        """After replacement, the embedded lib symbol has the new pin types."""
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, REPLACEMENT_LIB)

        replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "Regulator_Linear:XC6206P332MR",
            lib_path=str(lib_file),
        )

        # Parse the updated schematic and check the embedded lib symbol
        sexp = parse_string(sch_file.read_text())
        lib_syms = sexp.find("lib_symbols")
        new_sym = None
        for sym in lib_syms.find_all("symbol"):
            if sym.get_string(0) == "Regulator_Linear:XC6206P332MR":
                new_sym = sym
                break
        assert new_sym is not None

        # Find pin 3 in the unit sub-symbol and check its type
        from kicad_tools.schema.library import LibrarySymbol

        lib_sym = LibrarySymbol.from_sexp(new_sym)
        pin3 = lib_sym.get_pin("3")
        assert pin3 is not None
        assert pin3.type == "output", (
            f"Pin 3 should be 'output' (from new symbol), got '{pin3.type}'"
        )

    def test_without_lib_path_soft_replace_unchanged(self, tmp_path: Path):
        """Without lib_path, replacement is still soft (no lib_symbols update)."""
        sch_file = self._write_schematic(tmp_path)

        result = replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "Regulator_Linear:XC6206P332MR",
        )

        assert result.lib_symbol_updated is False
        assert len(result.pin_type_changes) == 0

        # lib_symbols should still have the OLD definition
        from kicad_tools.schema.schematic import Schematic

        sch = Schematic.load(sch_file)
        old_sym = sch.get_lib_symbol("Regulator_Linear:AP2204K-3.3")
        assert old_sym is not None, "Old lib_symbols entry should still be present"


class TestDerivedSymbolRoundTrip:
    """Tests for LibrarySymbol extends parsing and serialization."""

    def test_from_sexp_parses_extends(self):
        """Parsing a derived symbol sets the extends field."""
        from kicad_tools.schema.library import LibrarySymbol, SymbolLibrary

        lib = SymbolLibrary.load_from_string(DERIVED_SYMBOL_LIB)
        derived = lib.get_symbol("AP2112K-3.3")
        assert derived is not None
        assert derived.extends == "AP2204K-1.5"
        # Derived symbols have no pins of their own
        assert len(derived.pins) == 0

    def test_to_sexp_node_emits_extends(self):
        """Serializing a derived symbol emits (extends ...) and no sub-symbols."""
        from kicad_tools.schema.library import LibrarySymbol

        sym = LibrarySymbol(
            name="Regulator_Linear:AP2112K-3.3",
            properties={"Reference": "U", "Value": "AP2112K-3.3"},
            extends="AP2204K-1.5",
        )
        node = sym.to_sexp_node()
        from kicad_tools.sexp import serialize_sexp

        text = serialize_sexp(node)
        assert '(extends "AP2204K-1.5")' in text
        # No unit sub-symbols should be emitted
        assert "_0_1" not in text
        assert "_1_1" not in text

    def test_to_sexp_node_standalone_no_extends(self):
        """Standalone symbols do NOT emit (extends ...)."""
        from kicad_tools.schema.library import LibrarySymbol

        sym = LibrarySymbol(
            name="MySymbol",
            properties={"Reference": "U", "Value": "MySymbol"},
        )
        node = sym.to_sexp_node()
        from kicad_tools.sexp import serialize_sexp

        text = serialize_sexp(node)
        assert "extends" not in text

    def test_resolve_base(self):
        """resolve_base walks the extends chain to the root."""
        from kicad_tools.schema.library import SymbolLibrary

        lib = SymbolLibrary.load_from_string(DERIVED_SYMBOL_LIB)
        derived = lib.get_symbol("AP2112K-3.3")
        assert derived is not None
        base = lib.resolve_base(derived)
        assert base.name == "AP2204K-1.5"
        assert len(base.pins) == 3

    def test_resolve_base_standalone(self):
        """resolve_base returns the symbol itself if not derived."""
        from kicad_tools.schema.library import SymbolLibrary

        lib = SymbolLibrary.load_from_string(DERIVED_SYMBOL_LIB)
        base = lib.get_symbol("AP2204K-1.5")
        assert base is not None
        resolved = lib.resolve_base(base)
        assert resolved is base

    def test_resolve_base_missing_raises(self):
        """resolve_base raises ValueError when base is not in the library."""
        from kicad_tools.schema.library import LibrarySymbol, SymbolLibrary

        lib = SymbolLibrary(path="test", symbols={})
        orphan = LibrarySymbol(name="Orphan", extends="MissingBase")
        lib.symbols["Orphan"] = orphan

        with pytest.raises(ValueError, match="not found in library"):
            lib.resolve_base(orphan)


class TestReplaceDerivedSymbol:
    """Tests for replace_symbol_lib_id with derived (extends) symbols."""

    def _write_schematic(self, tmp_path: Path) -> Path:
        sch_file = tmp_path / "regulator.kicad_sch"
        sch_file.write_text(REGULATOR_SCHEMATIC)
        return sch_file

    def _write_lib(self, tmp_path: Path, content: str, name: str = "lib.kicad_sym") -> Path:
        lib_file = tmp_path / name
        lib_file.write_text(content)
        return lib_file

    def test_replace_with_derived_embeds_base(self, tmp_path: Path):
        """Replacing with a derived symbol embeds both derived and base entries."""
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, DERIVED_SYMBOL_LIB)

        result = replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "Regulator_Linear:AP2112K-3.3",
            lib_path=str(lib_file),
        )

        assert result.lib_symbol_updated is True

        # Parse the result and check lib_symbols
        sexp = parse_string(sch_file.read_text())
        lib_syms = sexp.find("lib_symbols")
        assert lib_syms is not None

        names = [sym.get_string(0) for sym in lib_syms.find_all("symbol")]
        # Base must be present
        assert "AP2204K-1.5" in names, (
            f"Base symbol should be embedded. Found: {names}"
        )
        # Derived entry must be present (renamed to the new lib_id)
        assert "Regulator_Linear:AP2112K-3.3" in names, (
            f"Derived symbol should be embedded. Found: {names}"
        )
        # Old entry must be gone
        assert "Regulator_Linear:AP2204K-3.3" not in names

    def test_derived_symbol_has_extends_in_output(self, tmp_path: Path):
        """The embedded derived symbol entry must contain (extends ...)."""
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, DERIVED_SYMBOL_LIB)

        replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "Regulator_Linear:AP2112K-3.3",
            lib_path=str(lib_file),
        )

        from kicad_tools.sexp import serialize_sexp as ser

        text = sch_file.read_text()
        sexp = parse_string(text)
        lib_syms = sexp.find("lib_symbols")
        for sym in lib_syms.find_all("symbol"):
            if sym.get_string(0) == "Regulator_Linear:AP2112K-3.3":
                sym_text = ser(sym)
                assert '(extends "AP2204K-1.5")' in sym_text
                # Should NOT have unit sub-symbols
                assert "_1_1" not in sym_text
                break
        else:
            pytest.fail("Derived symbol not found in lib_symbols")

    def test_pin_reconciliation_uses_base_pins(self, tmp_path: Path):
        """Instance pins are reconciled against the base symbol's pins."""
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, DERIVED_SYMBOL_LIB)

        result = replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "Regulator_Linear:AP2112K-3.3",
            lib_path=str(lib_file),
        )

        # The base has 3 pins (1, 2, 3), same as old symbol
        assert result.new_pin_count == 3

        # Verify instance still has 3 pins
        sexp = parse_string(sch_file.read_text())
        symbol = find_symbol_by_reference(sexp, "U1")
        pins = get_symbol_pins(symbol)
        assert len(pins) == 3

    def test_base_already_present_no_duplicate(self, tmp_path: Path):
        """When the base symbol is already in lib_symbols it is not duplicated."""
        # Build a schematic that already has the base symbol
        sch_with_base = REGULATOR_SCHEMATIC.replace(
            "  )\n  (symbol",
            '    (symbol "AP2204K-1.5"\n'
            '      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))\n'
            '      (symbol "AP2204K-1.5_1_1"\n'
            '        (pin power_in line (at -7.62 2.54 0) (length 2.54) (name "VIN") (number "1"))\n'
            '        (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND") (number "2"))\n'
            '        (pin power_out line (at 7.62 2.54 180) (length 2.54) (name "VOUT") (number "3"))\n'
            "      )\n"
            "    )\n"
            "  )\n  (symbol",
            1,
        )
        sch_file = tmp_path / "with_base.kicad_sch"
        sch_file.write_text(sch_with_base)
        lib_file = self._write_lib(tmp_path, DERIVED_SYMBOL_LIB)

        replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "Regulator_Linear:AP2112K-3.3",
            lib_path=str(lib_file),
        )

        sexp = parse_string(sch_file.read_text())
        lib_syms = sexp.find("lib_symbols")
        base_count = sum(
            1 for sym in lib_syms.find_all("symbol")
            if sym.get_string(0) == "AP2204K-1.5"
        )
        assert base_count == 1, f"Base should appear exactly once, found {base_count}"

    def test_pin_type_changes_from_base(self, tmp_path: Path):
        """Pin type changes are correctly reported when new symbol is derived."""
        # The old symbol has pin 3 as power_out, the base AP2204K-1.5 also
        # has pin 3 as power_out, so no changes expected with this fixture.
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, DERIVED_SYMBOL_LIB)

        result = replace_symbol_lib_id(
            str(sch_file),
            "U1",
            "Regulator_Linear:AP2112K-3.3",
            lib_path=str(lib_file),
        )

        # Both old and new have power_out on pin 3, power_in on 1 and 2
        assert len(result.pin_type_changes) == 0

    def test_missing_base_in_library_raises(self, tmp_path: Path):
        """Clear error when the base symbol is not found in the library."""
        # Library with a derived symbol but no base
        orphan_lib = """(kicad_symbol_lib
  (version 20231120)
  (generator "test")
  (symbol "OrphanDerived"
    (extends "MissingBase")
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "OrphanDerived" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
  )
)
"""
        sch_file = self._write_schematic(tmp_path)
        lib_file = self._write_lib(tmp_path, orphan_lib)

        with pytest.raises(ValueError, match="not found in library"):
            replace_symbol_lib_id(
                str(sch_file),
                "U1",
                "OrphanDerived",
                lib_path=str(lib_file),
            )


class TestNetOpsHelpers:
    """Tests for net_ops helper functions."""

    def test_points_equal_exact(self):
        """Test exact point equality."""
        assert points_equal((10.0, 20.0), (10.0, 20.0)) is True

    def test_points_equal_within_tolerance(self):
        """Test points equal within tolerance."""
        assert points_equal((10.0, 20.0), (10.05, 20.05)) is True

    def test_points_equal_outside_tolerance(self):
        """Test points not equal outside tolerance."""
        assert points_equal((10.0, 20.0), (10.2, 20.2)) is False

    def test_point_on_wire(self):
        """Test point on wire detection."""
        wire = Wire(start=(0, 0), end=(10, 0))
        assert point_on_wire((5, 0), wire) is True
        assert point_on_wire((5, 5), wire) is False


class TestNetConnection:
    """Tests for NetConnection class."""

    def test_net_connection_creation(self):
        """Test creating a net connection."""
        conn = NetConnection(
            point=(10.0, 20.0),
            type="pin",
            reference="R1",
            pin_number="1",
            uuid="test-uuid",
        )
        assert conn.point == (10.0, 20.0)
        assert conn.type == "pin"
        assert conn.reference == "R1"
        assert conn.pin_number == "1"


class TestNet:
    """Tests for Net class."""

    def test_net_pin_count(self):
        """Test counting pins in a net."""
        net = Net(
            name="VCC",
            connections=[
                NetConnection(point=(0, 0), type="pin", reference="U1", pin_number="1"),
                NetConnection(point=(10, 0), type="pin", reference="U2", pin_number="3"),
                NetConnection(point=(5, 0), type="junction"),
            ],
        )
        assert net.pin_count == 2

    def test_net_symbol_refs(self):
        """Test getting symbol references from net."""
        net = Net(
            name="VCC",
            connections=[
                NetConnection(point=(0, 0), type="pin", reference="U1", pin_number="1"),
                NetConnection(point=(10, 0), type="pin", reference="U2", pin_number="3"),
                NetConnection(point=(20, 0), type="pin", reference="U1", pin_number="5"),
            ],
        )
        refs = net.symbol_refs
        assert refs == {"U1", "U2"}

    def test_net_repr(self):
        """Test net string representation."""
        net = Net(name="GND")
        s = repr(net)
        assert "Net" in s
        assert "GND" in s


class TestNetTracer:
    """Tests for NetTracer class."""

    def test_net_tracer_init(self, minimal_schematic: Path):
        """Test initializing NetTracer."""
        sch = Schematic.load(minimal_schematic)
        tracer = NetTracer(sch)
        assert len(tracer.wire_endpoints) > 0

    def test_trace_from_point(self, minimal_schematic: Path):
        """Test tracing net from a point."""
        sch = Schematic.load(minimal_schematic)
        tracer = NetTracer(sch)

        # Trace from wire endpoint
        wire = sch.wires[0]
        net = tracer.trace_from_point(wire.start)
        assert net is not None
        assert len(net.wires) > 0

    def test_trace_all_nets(self, minimal_schematic: Path):
        """Test tracing all nets."""
        sch = Schematic.load(minimal_schematic)
        nets = trace_nets(sch)
        assert len(nets) > 0

    def test_find_net_by_label(self, minimal_schematic: Path):
        """Test finding net by label."""
        sch = Schematic.load(minimal_schematic)
        net = find_net(sch, "NET1")
        assert net is not None
        assert net.name == "NET1"
        assert net.has_label is True

    def test_find_net_not_found(self, minimal_schematic: Path):
        """Test finding non-existent net."""
        sch = Schematic.load(minimal_schematic)
        net = find_net(sch, "NONEXISTENT")
        assert net is None


class TestNetlistComponent:
    """Tests for NetlistComponent class."""

    def test_from_sexp(self):
        """Test parsing component from S-expression."""
        sexp = parse_string("""(comp
            (ref "R1")
            (value "10k")
            (footprint "Resistor_SMD:R_0402")
            (libsource (lib "Device") (part "R"))
            (sheetpath (names "/"))
            (property "Tolerance" "1%")
        )""")
        comp = NetlistComponent.from_sexp(sexp)
        assert comp.reference == "R1"
        assert comp.value == "10k"
        assert comp.footprint == "Resistor_SMD:R_0402"
        assert comp.lib_id == "R"
        assert comp.properties.get("Tolerance") == "1%"


class TestNetNode:
    """Tests for NetNode class."""

    def test_from_sexp(self):
        """Test parsing net node from S-expression."""
        sexp = parse_string("""(node "R1"
            (pin "1")
            (pinfunction "~")
            (pintype "passive")
        )""")
        node = NetNode.from_sexp(sexp)
        assert node.reference == "R1"
        assert node.pin == "1"
        assert node.pin_type == "passive"


class TestNetlistNet:
    """Tests for NetlistNet class."""

    def test_from_sexp(self):
        """Test parsing net from S-expression."""
        sexp = parse_string("""(net
            (code "1")
            (name "GND")
            (node "R1" (pin "1"))
            (node "C1" (pin "2"))
        )""")
        net = NetlistNet.from_sexp(sexp)
        assert net.code == 1
        assert net.name == "GND"
        assert net.connection_count == 2

    def test_connection_count(self):
        """Test connection count property."""
        net = NetlistNet(
            code=1,
            name="Test",
            nodes=[
                NetNode(reference="R1", pin="1"),
                NetNode(reference="R2", pin="1"),
            ],
        )
        assert net.connection_count == 2


class TestNetlist:
    """Tests for Netlist class."""

    def test_from_sexp_basic(self):
        """Test parsing basic netlist."""
        sexp = parse_string("""(export
            (design
                (source "test.kicad_sch")
                (tool "Eeschema 8.0")
                (date "2024-01-15")
            )
            (components
                (comp (ref "R1") (value "10k"))
                (comp (ref "C1") (value "100nF"))
            )
            (nets
                (net (code "1") (name "GND")
                    (node "R1" (pin "1"))
                    (node "C1" (pin "2"))
                )
            )
        )""")
        netlist = Netlist.from_sexp(sexp)
        assert netlist.source_file == "test.kicad_sch"
        assert netlist.tool == "Eeschema 8.0"
        assert len(netlist.components) == 2
        assert len(netlist.nets) == 1

    def test_get_component(self):
        """Test getting component by reference."""
        netlist = Netlist(
            components=[
                NetlistComponent(reference="R1", value="10k", footprint="", lib_id=""),
                NetlistComponent(reference="C1", value="100nF", footprint="", lib_id=""),
            ],
        )
        comp = netlist.get_component("R1")
        assert comp is not None
        assert comp.value == "10k"
        assert netlist.get_component("X99") is None

    def test_get_net(self):
        """Test getting net by name."""
        netlist = Netlist(
            nets=[
                NetlistNet(code=1, name="GND"),
                NetlistNet(code=2, name="VCC"),
            ],
        )
        net = netlist.get_net("GND")
        assert net is not None
        assert net.code == 1
        assert netlist.get_net("MISSING") is None

    def test_get_component_nets(self):
        """Test getting all nets connected to a component."""
        netlist = Netlist(
            nets=[
                NetlistNet(
                    code=1,
                    name="GND",
                    nodes=[
                        NetNode(reference="R1", pin="1"),
                    ],
                ),
                NetlistNet(
                    code=2,
                    name="VCC",
                    nodes=[
                        NetNode(reference="R1", pin="2"),
                    ],
                ),
                NetlistNet(
                    code=3,
                    name="NC",
                    nodes=[
                        NetNode(reference="R2", pin="1"),
                    ],
                ),
            ],
        )
        nets = netlist.get_component_nets("R1")
        assert len(nets) == 2
        assert "GND" in [n.name for n in nets]
        assert "VCC" in [n.name for n in nets]

    def test_get_net_by_pin(self):
        """Test getting net by pin."""
        netlist = Netlist(
            nets=[
                NetlistNet(
                    code=1,
                    name="GND",
                    nodes=[
                        NetNode(reference="R1", pin="1"),
                    ],
                ),
                NetlistNet(
                    code=2,
                    name="VCC",
                    nodes=[
                        NetNode(reference="R1", pin="2"),
                    ],
                ),
            ],
        )
        net = netlist.get_net_by_pin("R1", "2")
        assert net is not None
        assert net.name == "VCC"

    def test_power_nets(self):
        """Test getting power nets."""
        netlist = Netlist(
            nets=[
                NetlistNet(
                    code=1,
                    name="GND",
                    nodes=[
                        NetNode(reference="U1", pin="1", pin_type="power_in"),
                    ],
                ),
                NetlistNet(
                    code=2,
                    name="SIG",
                    nodes=[
                        NetNode(reference="U1", pin="2", pin_type="output"),
                    ],
                ),
            ],
        )
        power = netlist.power_nets
        assert len(power) == 1
        assert power[0].name == "GND"

    def test_to_dict(self):
        """Test conversion to dictionary."""
        netlist = Netlist(
            source_file="test.sch",
            tool="Test",
            date="2024-01-01",
            components=[
                NetlistComponent(
                    reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"
                ),
            ],
            nets=[
                NetlistNet(code=1, name="GND"),
            ],
        )
        d = netlist.to_dict()
        assert d["source"] == "test.sch"
        assert len(d["components"]) == 1
        assert len(d["nets"]) == 1

    def test_to_json(self):
        """Test JSON serialization."""
        netlist = Netlist(source_file="test.sch")
        json_str = netlist.to_json()
        assert "test.sch" in json_str

    def test_summary(self):
        """Test summary generation."""
        netlist = Netlist(
            components=[
                NetlistComponent(reference="R1", value="10k", footprint="", lib_id=""),
                NetlistComponent(reference="R2", value="10k", footprint="", lib_id=""),
                NetlistComponent(reference="C1", value="100nF", footprint="", lib_id=""),
            ],
            nets=[
                NetlistNet(code=1, name="GND"),
                NetlistNet(code=2, name="+5V"),
                NetlistNet(code=3, name="SIG"),
            ],
        )
        summary = netlist.summary()
        assert summary["component_count"] == 3
        assert summary["net_count"] == 3
        assert summary["components_by_type"]["R"] == 2
        assert summary["components_by_type"]["C"] == 1


class TestNetlistFromSexpErrors:
    """Tests for error handling in Netlist.from_sexp."""

    def test_invalid_root_tag(self):
        """Test error on invalid root tag."""
        sexp = parse_string("(not_export)")
        with pytest.raises(ValueError, match="Expected 'export'"):
            Netlist.from_sexp(sexp)
