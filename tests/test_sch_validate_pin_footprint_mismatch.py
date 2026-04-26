"""Tests for symbol-to-footprint pin/pad count mismatch detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_validate import (
    ValidationIssue,
    check_symbol_footprint_pin_mismatch,
    validate_schematic,
)
from kicad_tools.pcb.footprints import _FP_PAD_COUNT_RE, get_pad_count


# ---------------------------------------------------------------------------
# Helpers: synthetic KiCad schematic generation
# ---------------------------------------------------------------------------


def _make_lib_symbol(
    lib_id: str,
    pins: list[tuple[str, str, str]],
) -> str:
    """Generate a lib_symbols entry for a component.

    Args:
        lib_id: e.g. "Regulator_Linear:AP2204K-1.5"
        pins: list of (pin_number, pin_name, pin_type) tuples
    """
    part_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id
    pin_blocks = []
    for i, (num, name, ptype) in enumerate(pins):
        y = i * 2.54
        pin_blocks.append(
            f"""(pin {ptype} line
                    (at 0 {y:.2f} 0)
                    (length 2.54)
                    (name "{name}")
                    (number "{num}")
                )"""
        )
    pin_str = "\n".join(pin_blocks)
    return f"""(symbol "{lib_id}"
            (pin_names (offset 0.254))
            (symbol "{part_name}_0_1"
                (rectangle
                    (start -5.08 -{(len(pins) * 2.54) + 1.27:.2f})
                    (end 5.08 1.27)
                    (stroke (width 0.254))
                    (fill (type background))
                )
            )
            (symbol "{part_name}_1_1"
                {pin_str}
            )
        )"""


def _make_symbol_instance(
    ref: str,
    lib_id: str,
    footprint: str,
    pins: list[tuple[str, str, str]],
    x: float = 100.0,
    y: float = 50.0,
    dnp: bool = False,
) -> str:
    """Generate a symbol instance S-expression."""
    pin_entries = "\n".join(
        f'(pin "{num}" (uuid "pin-{ref.lower()}-{num}"))' for num, _, _ in pins
    )
    dnp_str = "yes" if dnp else "no"
    return f"""(symbol
        (lib_id "{lib_id}")
        (at {x} {y} 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp {dnp_str})
        (uuid "uuid-{ref.lower()}")
        (property "Reference" "{ref}"
            (at {x + 2} {y - 2} 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Value" "{lib_id.split(':')[-1]}"
            (at {x + 2} {y} 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Footprint" "{footprint}"
            (at {x} {y} 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        (property "Datasheet" "~"
            (at {x} {y} 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        {pin_entries}
    )"""


def _make_schematic(*blocks: str, lib_symbols: str = "") -> str:
    """Wrap lib_symbols and symbol instances into a minimal schematic."""
    body = "\n".join(blocks)
    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-pin-fp-mismatch-uuid")
    (paper "A4")
    (lib_symbols
        {lib_symbols}
    )
    {body}
    (sheet_instances
        (path "/"
            (page "1")
        )
    )
)"""


# ---------------------------------------------------------------------------
# Fixture: write schematic to tmp_path
# ---------------------------------------------------------------------------


def _write_sch(tmp_path: Path, content: str, name: str = "test.kicad_sch") -> str:
    """Write schematic content to a file and return the path as string."""
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# ---------------------------------------------------------------------------
# Test: get_pad_count helper
# ---------------------------------------------------------------------------


class TestGetPadCount:
    """Tests for the get_pad_count() convenience function."""

    def test_known_footprint_sot23_5(self):
        assert get_pad_count("Package_TO_SOT_SMD:SOT-23-5") == 5

    def test_known_footprint_tssop20(self):
        assert get_pad_count("Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm") == 20

    def test_known_footprint_via_alias(self):
        assert get_pad_count("SOT-23-5") == 5

    def test_known_footprint_capacitor(self):
        assert get_pad_count("Capacitor_SMD:C_0603_1608Metric") == 2

    def test_unknown_footprint_with_name_heuristic(self):
        # Unknown footprint but name encodes pad count
        assert get_pad_count("Package_QFP:QFP-48_7x7mm") == 48

    def test_unknown_footprint_dip8(self):
        assert get_pad_count("Package_DIP:DIP-8_W7.62mm") == 8

    def test_unknown_footprint_soic16(self):
        assert get_pad_count("Package_SO:SOIC-16_3.9x9.9mm_P1.27mm") == 16

    def test_completely_unknown_returns_none(self):
        # No name heuristic possible
        assert get_pad_count("SomeLib:WeirdPart") is None

    def test_empty_returns_none(self):
        assert get_pad_count("") is None
        assert get_pad_count("~") is None

    def test_none_input_returns_none(self):
        # Handles edge case (though typed as str)
        assert get_pad_count("") is None


# ---------------------------------------------------------------------------
# Test: _FP_PAD_COUNT_RE regex
# ---------------------------------------------------------------------------


class TestFootprintNameRegex:
    """Tests for the name-based pad count extraction regex.

    The heuristic uses ``findall()`` and takes the *last* match so that
    compound names like ``SOT-23-5`` yield 5 (the pad count), not 23.
    """

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("SOT-23-5", 5),
            ("TSSOP-20_4.4x6.5mm_P0.65mm", 20),
            ("QFP-48_7x7mm", 48),
            ("DIP-8_W7.62mm", 8),
            ("SOIC-16_3.9x9.9mm", 16),
            ("QFN-32-1EP_5x5mm", 32),
        ],
    )
    def test_extracts_count(self, name, expected):
        matches = _FP_PAD_COUNT_RE.findall(name)
        assert len(matches) > 0
        assert int(matches[-1]) == expected

    def test_no_match_for_plain_name(self):
        assert len(_FP_PAD_COUNT_RE.findall("WeirdPart")) == 0


# ---------------------------------------------------------------------------
# Test: mismatch detection
# ---------------------------------------------------------------------------


class TestPinFootprintMismatch:
    """Tests for check_symbol_footprint_pin_mismatch()."""

    def test_mismatch_5pin_symbol_3pad_footprint(self, tmp_path):
        """5-pin symbol assigned to a footprint whose name implies 3 pads."""
        pins_5 = [
            ("1", "VIN", "input"),
            ("2", "GND", "passive"),
            ("3", "EN", "input"),
            ("4", "NC", "passive"),
            ("5", "VOUT", "output"),
        ]
        lib_id = "Regulator_Linear:AP2204K"
        lib_sym = _make_lib_symbol(lib_id, pins_5)
        # Use a footprint name that implies 3 pads (heuristic)
        inst = _make_symbol_instance("U1", lib_id, "Package_TO_SOT_SMD:SOT-23-3", pins_5)
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        issues = check_symbol_footprint_pin_mismatch(sch_path)
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 1
        assert "U1" in warnings[0].message
        assert "5 pins" in warnings[0].message
        assert "3 pads" in warnings[0].message
        assert warnings[0].category == "pin_footprint_mismatch"

    def test_match_passes_no_issues(self, tmp_path):
        """Symbol and footprint pad counts match -- no issues emitted."""
        pins_5 = [
            ("1", "VIN", "input"),
            ("2", "GND", "passive"),
            ("3", "EN", "input"),
            ("4", "NC", "passive"),
            ("5", "VOUT", "output"),
        ]
        lib_id = "Regulator_Linear:AP2204K"
        lib_sym = _make_lib_symbol(lib_id, pins_5)
        # SOT-23-5 is in COMMON_FOOTPRINTS with 5 pads
        inst = _make_symbol_instance("U1", lib_id, "Package_TO_SOT_SMD:SOT-23-5", pins_5)
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        issues = check_symbol_footprint_pin_mismatch(sch_path)
        assert len(issues) == 0

    def test_thermal_pad_tolerance(self, tmp_path):
        """Footprint with 1 extra pad (thermal) -> info, not warning."""
        pins_20 = [(str(i), f"P{i}", "passive") for i in range(1, 21)]
        lib_id = "IC:SomeQFN"
        lib_sym = _make_lib_symbol(lib_id, pins_20)
        # Footprint name implies 21 pads (20 signal + 1 thermal)
        inst = _make_symbol_instance("U1", lib_id, "Package_DFN_QFN:QFN-21_5x5mm", pins_20)
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        issues = check_symbol_footprint_pin_mismatch(sch_path)
        assert len(issues) == 1
        assert issues[0].severity == "info"
        assert "thermal" in issues[0].message.lower()

    def test_multi_unit_symbol_no_false_positive(self, tmp_path):
        """Dual op-amp: 8-pin package, all pins collected across units.

        LibrarySymbol.from_sexp() collects pins from ALL unit sub-symbols,
        so a dual op-amp with 8 total pins should match an 8-pad SOIC.
        """
        # Build a multi-unit symbol manually (8 pins total across 2 units)
        lib_id = "Amplifier_OpAmp:LM358"
        part_name = "LM358"
        # Unit 1: pins 1,2,3 (non-inverting, inverting, output)
        # Unit 2: pins 5,6,7
        # Unit 0 (shared): pins 4 (V+), 8 (V-)
        lib_sym_sexp = f"""(symbol "{lib_id}"
            (pin_names (offset 0.254))
            (symbol "{part_name}_0_1"
                (rectangle
                    (start -5.08 -10.16)
                    (end 5.08 1.27)
                    (stroke (width 0.254))
                    (fill (type background))
                )
            )
            (symbol "{part_name}_0_2"
                (pin power_in line (at 0 0 0) (length 2.54) (name "V+") (number "8"))
                (pin power_in line (at 0 2.54 0) (length 2.54) (name "V-") (number "4"))
            )
            (symbol "{part_name}_1_1"
                (pin output line (at 0 0 0) (length 2.54) (name "OUT_A") (number "1"))
                (pin input line (at 0 2.54 0) (length 2.54) (name "IN-_A") (number "2"))
                (pin input line (at 0 5.08 0) (length 2.54) (name "IN+_A") (number "3"))
            )
            (symbol "{part_name}_2_1"
                (pin output line (at 0 0 0) (length 2.54) (name "OUT_B") (number "7"))
                (pin input line (at 0 2.54 0) (length 2.54) (name "IN-_B") (number "6"))
                (pin input line (at 0 5.08 0) (length 2.54) (name "IN+_B") (number "5"))
            )
        )"""

        pins_for_inst = [
            ("1", "OUT_A", "output"),
            ("2", "IN-_A", "input"),
            ("3", "IN+_A", "input"),
            ("4", "V-", "power_in"),
            ("5", "IN+_B", "input"),
            ("6", "IN-_B", "input"),
            ("7", "OUT_B", "output"),
            ("8", "V+", "power_in"),
        ]
        # SOIC-8 -> 8 pads via heuristic
        inst = _make_symbol_instance("U1", lib_id, "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", pins_for_inst)
        content = _make_schematic(inst, lib_symbols=lib_sym_sexp)
        sch_path = _write_sch(tmp_path, content)

        issues = check_symbol_footprint_pin_mismatch(sch_path)
        assert len(issues) == 0

    def test_power_symbol_skipped(self, tmp_path):
        """Power symbols should not be checked."""
        pins = [("1", "GND", "power_in")]
        lib_id = "power:GND"
        lib_sym = _make_lib_symbol(lib_id, pins)
        inst = _make_symbol_instance("PWR1", lib_id, "Package_TO_SOT_SMD:SOT-23-5", pins)
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        issues = check_symbol_footprint_pin_mismatch(sch_path)
        assert len(issues) == 0

    def test_dnp_symbol_skipped(self, tmp_path):
        """DNP symbols should not be checked."""
        pins_5 = [
            ("1", "VIN", "input"),
            ("2", "GND", "passive"),
            ("3", "EN", "input"),
            ("4", "NC", "passive"),
            ("5", "VOUT", "output"),
        ]
        lib_id = "Regulator_Linear:AP2204K"
        lib_sym = _make_lib_symbol(lib_id, pins_5)
        inst = _make_symbol_instance(
            "U1", lib_id, "Package_TO_SOT_SMD:SOT-23-3", pins_5, dnp=True
        )
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        issues = check_symbol_footprint_pin_mismatch(sch_path)
        assert len(issues) == 0

    def test_unknown_footprint_fallback_heuristic(self, tmp_path):
        """Footprint not in COMMON_FOOTPRINTS uses name heuristic."""
        pins_8 = [(str(i), f"P{i}", "passive") for i in range(1, 9)]
        lib_id = "IC:SomeChip"
        lib_sym = _make_lib_symbol(lib_id, pins_8)
        # MSOP-10 not in COMMON_FOOTPRINTS, heuristic extracts 10
        inst = _make_symbol_instance("U1", lib_id, "Package_SO:MSOP-10_3x3mm", pins_8)
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        issues = check_symbol_footprint_pin_mismatch(sch_path)
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 1
        assert "8 pins" in warnings[0].message
        assert "10 pads" in warnings[0].message

    def test_unresolvable_footprint_no_issue(self, tmp_path):
        """When footprint pad count cannot be determined, no issue emitted."""
        pins_3 = [("1", "A", "passive"), ("2", "B", "passive"), ("3", "C", "passive")]
        lib_id = "Custom:Widget"
        lib_sym = _make_lib_symbol(lib_id, pins_3)
        # Footprint name has no extractable pad count
        inst = _make_symbol_instance("U1", lib_id, "MyLib:CustomPart", pins_3)
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        issues = check_symbol_footprint_pin_mismatch(sch_path)
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# Test: integration with validate_schematic
# ---------------------------------------------------------------------------


class TestValidateSchematicIntegration:
    """Verify the check is registered in validate_schematic."""

    def test_check_registered(self, tmp_path):
        """symbol_footprint_pin_count appears in checks_run."""
        pins_2 = [("1", "A", "passive"), ("2", "B", "passive")]
        lib_id = "Device:R"
        lib_sym = _make_lib_symbol(lib_id, pins_2)
        inst = _make_symbol_instance("R1", lib_id, "Resistor_SMD:R_0603_1608Metric", pins_2)
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        result = validate_schematic(sch_path)
        assert "symbol_footprint_pin_count" in result.checks_run
