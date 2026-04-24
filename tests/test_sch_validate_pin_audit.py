"""Tests for pin-net semantic mismatch detection (pin assignment audit)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_validate import (
    ValidationIssue,
    _find_protocol,
    _is_generic_pin_name,
    _is_passive_component,
    _tokenize_name,
    check_pin_net_semantic_mismatch,
)


# ---------------------------------------------------------------------------
# Helpers to generate synthetic KiCad schematics
# ---------------------------------------------------------------------------


def _make_ic_lib_symbol(
    lib_id: str,
    pins: list[tuple[str, str, str]],
) -> str:
    """Generate a lib_symbols entry for an IC.

    Args:
        lib_id: e.g. "Audio_Codec:DAC1234"
        pins: list of (pin_number, pin_name, pin_type) tuples
              pin_type: "input", "output", "passive", "power_in", etc.
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
    ref: str, lib_id: str, pins: list[tuple[str, str, str]], x: float, y: float,
    dnp: str = "no",
) -> str:
    """Generate a symbol instance S-expression."""
    pin_entries = "\n".join(
        f'(pin "{num}" (uuid "pin-{ref.lower()}-{num}"))' for num, _, _ in pins
    )
    return f"""(symbol
        (lib_id "{lib_id}")
        (at {x} {y} 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp {dnp})
        (uuid "uuid-{ref.lower()}")
        (property "Reference" "{ref}"
            (at {x + 2} {y - 2} 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Value" "{lib_id.split(':')[-1]}"
            (at {x + 2} {y} 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Footprint" ""
            (at {x} {y} 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        (property "Datasheet" "~"
            (at {x} {y} 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        {pin_entries}
    )"""


def _make_schematic(
    lib_id: str,
    pins: list[tuple[str, str, str]],
    pin_nets: dict[str, str],
    ref: str = "U1",
    dnp: str = "no",
) -> str:
    """Build a complete schematic with one IC and wired labels.

    Args:
        lib_id: Library ID for the symbol.
        pins: List of (pin_number, pin_name, pin_type).
        pin_nets: Mapping of pin_number -> net_name for connected pins.
        ref: Reference designator.
        dnp: "yes" or "no" for Do Not Populate.
    """
    lib_sym = _make_ic_lib_symbol(lib_id, pins)
    sym_x, sym_y = 100.0, 50.0
    sym_inst = _make_symbol_instance(ref, lib_id, pins, sym_x, sym_y, dnp=dnp)

    wires = []
    labels = []
    for pin_idx, (pin_num, _, _) in enumerate(pins):
        if pin_num not in pin_nets:
            continue
        net_name = pin_nets[pin_num]
        pin_y = sym_y - pin_idx * 2.54
        pin_x = sym_x
        label_x = pin_x + 10.0

        wires.append(
            f"""(wire
            (pts (xy {pin_x:.2f} {pin_y:.2f}) (xy {label_x:.2f} {pin_y:.2f}))
            (stroke (width 0) (type default))
            (uuid "wire-{ref.lower()}-{pin_num}")
        )"""
        )

        labels.append(
            f"""(label "{net_name}"
            (at {label_x:.2f} {pin_y:.2f} 0)
            (effects (font (size 1.27 1.27)) (justify left bottom))
            (uuid "lbl-{ref.lower()}-{pin_num}")
        )"""
        )

    wire_block = "\n".join(wires)
    label_block = "\n".join(labels)

    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-pin-audit-uuid")
    (paper "A4")
    (lib_symbols
        {lib_sym}
    )
    {sym_inst}
    {wire_block}
    {label_block}
)
"""


# ---------------------------------------------------------------------------
# Unit tests -- pure function tests
# ---------------------------------------------------------------------------


class TestTokenizeName:
    def test_underscore_split(self):
        assert _tokenize_name("I2S_BCLK") == {"I2S", "BCLK"}

    def test_slash_split(self):
        assert _tokenize_name("SPI0/MOSI") == {"SPI0", "MOSI"}

    def test_camelcase_split(self):
        assert _tokenize_name("SdaPin") == {"SDA", "PIN"}

    def test_digit_alpha_boundary(self):
        tokens = _tokenize_name("I2C1_SDA")
        assert "I2C1" in tokens
        assert "SDA" in tokens

    def test_empty_returns_empty(self):
        assert _tokenize_name("") == set()
        assert _tokenize_name("~") == set()

    def test_simple_name(self):
        assert _tokenize_name("BCLK") == {"BCLK"}


class TestFindProtocol:
    def test_i2c_detected(self):
        assert _find_protocol({"SDA", "I2C"}) == "I2C"

    def test_spi_detected(self):
        assert _find_protocol({"MOSI"}) == "SPI"

    def test_i2s_detected(self):
        assert _find_protocol({"BCLK"}) == "I2S"

    def test_uart_detected(self):
        assert _find_protocol({"TX"}) == "UART"

    def test_no_protocol(self):
        assert _find_protocol({"MODE", "EN"}) is None


class TestIsGenericPinName:
    def test_numeric(self):
        assert _is_generic_pin_name("1") is True
        assert _is_generic_pin_name("42") is True

    def test_tilde(self):
        assert _is_generic_pin_name("~") is True

    def test_empty(self):
        assert _is_generic_pin_name("") is True

    def test_named(self):
        assert _is_generic_pin_name("BCLK") is False
        assert _is_generic_pin_name("SDA") is False

    def test_connector_prefix(self):
        assert _is_generic_pin_name("P1") is True
        assert _is_generic_pin_name("P12") is True


class TestIsPassiveComponent:
    def test_resistor(self):
        assert _is_passive_component("Device:R") is True
        assert _is_passive_component("Device:R_Small") is True

    def test_capacitor(self):
        assert _is_passive_component("Device:C") is True
        assert _is_passive_component("Device:C_Polarized") is True

    def test_inductor(self):
        assert _is_passive_component("Device:L") is True

    def test_ferrite(self):
        assert _is_passive_component("Device:Ferrite_Bead") is True

    def test_ic_not_passive(self):
        assert _is_passive_component("Audio_Codec:DAC1234") is False
        assert _is_passive_component("MCU:STM32F4") is False


# ---------------------------------------------------------------------------
# Integration tests with synthetic schematics
# ---------------------------------------------------------------------------


class TestCheckPinNetSemanticMismatch:
    """Test check_pin_net_semantic_mismatch against synthetic schematics."""

    def test_correct_wiring_no_issues(self, tmp_path: Path):
        """An IC with correctly matched pin-to-net names produces no issues."""
        pins = [
            ("1", "BCLK", "input"),
            ("2", "LRCLK", "input"),
            ("3", "DIN", "input"),
            ("4", "DOUT", "output"),
        ]
        pin_nets = {
            "1": "I2S_BCLK",
            "2": "I2S_LRCLK",
            "3": "I2S_DIN",
            "4": "I2S_DOUT",
        }
        sch_text = _make_schematic("Audio_Codec:DAC1234", pins, pin_nets)
        sch_path = tmp_path / "correct.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_pin_net_semantic_mismatch(str(sch_path))
        pin_issues = [i for i in issues if i.category == "pin_assignment"]
        assert pin_issues == [], f"Unexpected issues: {pin_issues}"

    def test_individual_mismatch_warning(self, tmp_path: Path):
        """A single pin with a protocol mismatch produces a warning."""
        pins = [
            ("1", "BCLK", "input"),
            ("2", "SDA", "bidirectional"),
        ]
        # BCLK (I2S) connected to an I2C net
        pin_nets = {
            "1": "I2C_SDA",
            "2": "I2S_BCLK",
        }
        sch_text = _make_schematic("IC:MixedIC", pins, pin_nets)
        sch_path = tmp_path / "mismatch.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_pin_net_semantic_mismatch(str(sch_path))
        pin_issues = [
            i for i in issues
            if i.category == "pin_assignment" and i.severity == "warning"
        ]
        assert len(pin_issues) >= 1
        # Should mention the ref and pin name
        assert any("BCLK" in i.message or "SDA" in i.message for i in pin_issues)

    def test_systematic_offset_error(self, tmp_path: Path):
        """Multiple consecutive pins offset by the same amount produce an error."""
        # Simulate a DAC where I2S signals are shifted by +2 positions
        # Pin layout: 1=BCLK, 2=LRCLK, 3=DIN, 4=DOUT, 5=MODE1, 6=MODE2
        pins = [
            ("1", "BCLK", "input"),
            ("2", "LRCLK", "input"),
            ("3", "DIN", "input"),
            ("4", "DOUT", "output"),
            ("5", "MODE1", "input"),
            ("6", "MODE2", "input"),
        ]
        # Offset by +2: net for pin 1 (BCLK) is on pin 3, etc.
        pin_nets = {
            "1": "MODE1_NET",
            "2": "MODE2_NET",
            "3": "I2S_BCLK",   # should be on pin 1
            "4": "I2S_LRCLK",  # should be on pin 2
            "5": "I2S_DIN",    # should be on pin 3
            "6": "I2S_DOUT",   # should be on pin 4
        }
        sch_text = _make_schematic("Audio_Codec:DAC5678", pins, pin_nets)
        sch_path = tmp_path / "offset.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_pin_net_semantic_mismatch(str(sch_path))
        error_issues = [
            i for i in issues
            if i.category == "pin_assignment" and i.severity == "error"
        ]
        assert len(error_issues) >= 1
        assert any("systematic wiring offset" in i.message for i in error_issues)
        # Should mention the offset magnitude
        assert any("+2" in i.message for i in error_issues)

    def test_passive_components_no_false_positive(self, tmp_path: Path):
        """Passive components with generic pin names should not be flagged."""
        pins = [
            ("1", "1", "passive"),
            ("2", "2", "passive"),
        ]
        pin_nets = {
            "1": "I2S_BCLK",
            "2": "GND",
        }
        sch_text = _make_schematic("Device:R", pins, pin_nets)
        sch_path = tmp_path / "passive.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_pin_net_semantic_mismatch(str(sch_path))
        pin_issues = [i for i in issues if i.category == "pin_assignment"]
        assert pin_issues == []

    def test_power_pins_skipped(self, tmp_path: Path):
        """Power pins should not be checked by this function."""
        pins = [
            ("1", "VCC", "power_in"),
            ("2", "GND", "power_in"),
            ("3", "SDA", "bidirectional"),
        ]
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            "3": "I2C_SDA",
        }
        sch_text = _make_schematic("IC:SomeIC", pins, pin_nets)
        sch_path = tmp_path / "power_pins.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_pin_net_semantic_mismatch(str(sch_path))
        pin_issues = [i for i in issues if i.category == "pin_assignment"]
        assert pin_issues == []

    def test_unconnected_pins_skipped(self, tmp_path: Path):
        """Pins with no net connection should not be flagged."""
        pins = [
            ("1", "BCLK", "input"),
            ("2", "LRCLK", "input"),
        ]
        # Only connect pin 1; pin 2 left unconnected
        pin_nets = {
            "1": "I2S_BCLK",
        }
        sch_text = _make_schematic("Audio_Codec:DAC1234", pins, pin_nets)
        sch_path = tmp_path / "unconnected.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_pin_net_semantic_mismatch(str(sch_path))
        pin_issues = [i for i in issues if i.category == "pin_assignment"]
        assert pin_issues == []

    def test_tilde_pin_name_skipped(self, tmp_path: Path):
        """Pins with tilde names (unnamed) should not be flagged."""
        pins = [
            ("1", "~", "input"),
            ("2", "SDA", "bidirectional"),
        ]
        pin_nets = {
            "1": "I2S_BCLK",
            "2": "I2C_SDA",
        }
        sch_text = _make_schematic("IC:SomeIC", pins, pin_nets)
        sch_path = tmp_path / "tilde.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_pin_net_semantic_mismatch(str(sch_path))
        pin_issues = [i for i in issues if i.category == "pin_assignment"]
        # Only the SDA pin should be checked (and it matches), so no issues
        assert pin_issues == []

    def test_same_protocol_different_signals_flagged(self, tmp_path: Path):
        """Pins within the same protocol but swapped signals should be flagged."""
        pins = [
            ("1", "MOSI", "output"),
            ("2", "MISO", "input"),
        ]
        # Swap: MOSI pin gets MISO net and vice versa
        pin_nets = {
            "1": "SPI_MISO",
            "2": "SPI_MOSI",
        }
        sch_text = _make_schematic("IC:SPIC", pins, pin_nets)
        sch_path = tmp_path / "same_proto_swap.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_pin_net_semantic_mismatch(str(sch_path))
        pin_issues = [
            i for i in issues
            if i.category == "pin_assignment"
        ]
        # Both have SPI keywords but MOSI != MISO -- should flag
        assert len(pin_issues) >= 1
