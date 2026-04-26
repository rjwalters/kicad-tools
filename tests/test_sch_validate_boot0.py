"""Tests for BOOT0 pull-down resistor detection in sch validate."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_validate import (
    ValidationIssue,
    _is_boot0_pin,
    _is_stm32_symbol,
    _is_switch_or_button,
    _tokenize_name,
    check_boot0_pulldown,
)


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestIsStm32Symbol:
    """Test _is_stm32_symbol helper."""

    def test_full_lib_id(self):
        assert _is_stm32_symbol("MCU_ST_STM32:STM32C011F6Px") is True

    def test_short_lib_id(self):
        assert _is_stm32_symbol("STM32:STM32F401") is True

    def test_lowercase_in_lib_id(self):
        assert _is_stm32_symbol("mcu:stm32c011") is True

    def test_bare_part_name(self):
        assert _is_stm32_symbol("STM32F103C8T6") is True

    def test_esp32_not_stm32(self):
        assert _is_stm32_symbol("MCU_Espressif:ESP32-S3") is False

    def test_generic_mcu(self):
        assert _is_stm32_symbol("IC:MCU") is False

    def test_atmega(self):
        assert _is_stm32_symbol("MCU_Microchip:ATmega328P") is False

    def test_empty_string(self):
        assert _is_stm32_symbol("") is False

    def test_resistor(self):
        assert _is_stm32_symbol("Device:R_Small") is False


class TestIsBoot0Pin:
    """Test _is_boot0_pin helper."""

    def test_plain_boot0(self):
        assert _is_boot0_pin("BOOT0") is True

    def test_boot0_lowercase(self):
        # _tokenize_name uppercases tokens
        assert _is_boot0_pin("boot0") is True

    def test_boot0_with_prefix(self):
        assert _is_boot0_pin("MCU_BOOT0") is True

    def test_pa0_not_boot0(self):
        assert _is_boot0_pin("PA0") is False

    def test_boot1_not_boot0(self):
        assert _is_boot0_pin("BOOT1") is False

    def test_nboot0(self):
        # nBOOT0 tokenizes to {N, BOOT0} via camelCase split, so BOOT0 is present.
        # This is correct -- nBOOT0 is a negated BOOT0 pin and should be detected.
        assert _is_boot0_pin("nBOOT0") is True

    def test_swclk_not_boot0(self):
        assert _is_boot0_pin("SWCLK") is False

    def test_empty(self):
        assert _is_boot0_pin("") is False

    def test_tilde(self):
        assert _is_boot0_pin("~") is False


class TestIsSwitchOrButton:
    """Test _is_switch_or_button helper."""

    def test_sw_push(self):
        assert _is_switch_or_button("Switch:SW_Push") is True

    def test_sw_plain(self):
        assert _is_switch_or_button("SW") is True

    def test_sw_spdt(self):
        assert _is_switch_or_button("SW_SPDT") is True

    def test_btn(self):
        assert _is_switch_or_button("BTN_Small") is True

    def test_button(self):
        assert _is_switch_or_button("Button_Reset") is True

    def test_resistor_not_switch(self):
        assert _is_switch_or_button("Device:R_Small") is False

    def test_ic_not_switch(self):
        assert _is_switch_or_button("IC:STM32F4") is False


# ---------------------------------------------------------------------------
# Helpers to generate synthetic KiCad schematics
# ---------------------------------------------------------------------------


def _make_lib_symbol(
    lib_id: str,
    pins: list[tuple[str, str, str]],
) -> str:
    """Generate a lib_symbols entry."""
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
        (dnp no)
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


def _build_schematic(
    components: list[dict],
) -> str:
    """Build a complete schematic string from component descriptors.

    Each component dict has:
        ref: str          - reference designator (e.g. "U1", "R1")
        lib_id: str       - library identifier (e.g. "Device:R_Small")
        pins: list        - [(pin_num, pin_name, pin_type), ...]
        pin_nets: dict    - {pin_num: net_name, ...}
        x: float          - X position (optional, defaults based on index)
    """
    lib_symbols = []
    symbol_instances = []
    wires = []
    labels = []
    seen_lib_ids: set[str] = set()

    for idx, comp in enumerate(components):
        ref = comp["ref"]
        lib_id = comp["lib_id"]
        pins = comp["pins"]
        pin_nets = comp.get("pin_nets", {})
        x = comp.get("x", 100.0 + idx * 100.0)
        y = comp.get("y", 50.0)

        if lib_id not in seen_lib_ids:
            lib_symbols.append(_make_lib_symbol(lib_id, pins))
            seen_lib_ids.add(lib_id)

        symbol_instances.append(_make_symbol_instance(ref, lib_id, pins, x, y))

        for pin_idx, (pin_num, _, _) in enumerate(pins):
            if pin_num not in pin_nets:
                continue
            net_name = pin_nets[pin_num]
            pin_y = y - pin_idx * 2.54
            pin_x = x
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

    lib_block = "\n".join(lib_symbols)
    inst_block = "\n".join(symbol_instances)
    wire_block = "\n".join(wires)
    label_block = "\n".join(labels)

    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-boot0-uuid")
    (paper "A4")
    (lib_symbols
        {lib_block}
    )
    {inst_block}
    {wire_block}
    {label_block}
)
"""


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestCheckBoot0Pulldown:
    """Test check_boot0_pulldown against synthetic schematics."""

    def test_missing_pulldown_detected(self, tmp_path: Path):
        """STM32 BOOT0 without pull-down resistor should produce a warning."""
        components = [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32:STM32C011F6Px",
                "pins": [
                    ("1", "BOOT0", "input"),
                    ("2", "VDD", "power_in"),
                    ("3", "VSS", "power_in"),
                    ("4", "PA0", "bidirectional"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                    "2": "+3.3V",
                    "3": "GND",
                    "4": "GPIO_A0",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "missing_pulldown.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_boot0_pulldown(str(sch_path))
        warnings = [
            i for i in issues
            if i.category == "boot0_pulldown" and i.severity == "warning"
        ]
        assert len(warnings) == 1
        assert "BOOT0" in warnings[0].message
        assert "pull-down" in warnings[0].message
        assert "U1" in warnings[0].message

    def test_pulldown_present_no_warning(self, tmp_path: Path):
        """STM32 BOOT0 with proper pull-down to GND should not produce warnings."""
        components = [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32:STM32C011F6Px",
                "pins": [
                    ("1", "BOOT0", "input"),
                    ("2", "VDD", "power_in"),
                    ("3", "VSS", "power_in"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                    "2": "+3.3V",
                    "3": "GND",
                },
            },
            # Pull-down resistor for BOOT0
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                    "2": "GND",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "with_pulldown.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_boot0_pulldown(str(sch_path))
        warnings = [
            i for i in issues
            if i.category == "boot0_pulldown" and i.severity == "warning"
        ]
        assert warnings == []

    def test_resistor_to_vcc_not_pulldown(self, tmp_path: Path):
        """A resistor between BOOT0 and VCC is NOT a pull-down -- warning expected."""
        components = [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32:STM32C011F6Px",
                "pins": [
                    ("1", "BOOT0", "input"),
                    ("2", "VDD", "power_in"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                    "2": "+3.3V",
                },
            },
            # Resistor to VCC, NOT a pull-down
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                    "2": "VCC",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "resistor_to_vcc.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_boot0_pulldown(str(sch_path))
        warnings = [
            i for i in issues
            if i.category == "boot0_pulldown" and i.severity == "warning"
        ]
        assert len(warnings) == 1
        assert "BOOT0" in warnings[0].message

    def test_boot0_tied_to_swclk_error(self, tmp_path: Path):
        """BOOT0 tied to SWCLK pin should produce an error (signal contamination)."""
        components = [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32:STM32C011F6Px",
                "pins": [
                    ("1", "BOOT0", "input"),
                    ("2", "PA14/SWCLK", "bidirectional"),
                    ("3", "VDD", "power_in"),
                ],
                "pin_nets": {
                    "1": "SWCLK_BOOT0",
                    "2": "SWCLK_BOOT0",
                    "3": "+3.3V",
                },
            },
            # Debug header with SWCLK
            {
                "ref": "J1",
                "lib_id": "Connector:Conn_01x04",
                "pins": [
                    ("1", "SWCLK", "passive"),
                    ("2", "SWDIO", "passive"),
                ],
                "pin_nets": {
                    "1": "SWCLK_BOOT0",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "boot0_swclk.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_boot0_pulldown(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "boot0_pulldown" and i.severity == "error"
        ]
        assert len(errors) >= 1
        # Should mention SWCLK signal contamination
        assert any("SWCLK" in e.message for e in errors)

    def test_non_stm32_mcu_ignored(self, tmp_path: Path):
        """ESP32 or other MCU symbols should not trigger the BOOT0 check."""
        components = [
            {
                "ref": "U1",
                "lib_id": "MCU_Espressif:ESP32-S3",
                "pins": [
                    ("1", "GPIO0", "bidirectional"),
                    ("2", "VDD", "power_in"),
                    ("3", "GND", "power_in"),
                ],
                "pin_nets": {
                    "1": "GPIO0",
                    "2": "+3.3V",
                    "3": "GND",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "esp32.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_boot0_pulldown(str(sch_path))
        boot0_issues = [i for i in issues if i.category == "boot0_pulldown"]
        # Should have no warnings or errors (info messages about skipped sheets are OK)
        real_issues = [
            i for i in boot0_issues if i.severity in ("warning", "error")
        ]
        assert real_issues == []

    def test_boot0_with_button_no_contamination_error(self, tmp_path: Path):
        """A switch between BOOT0 and VCC should not trigger contamination error."""
        components = [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32:STM32C011F6Px",
                "pins": [
                    ("1", "BOOT0", "input"),
                    ("2", "VDD", "power_in"),
                    ("3", "VSS", "power_in"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                    "2": "+3.3V",
                    "3": "GND",
                },
            },
            # Pull-down resistor
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                    "2": "GND",
                },
            },
            # Bootloader mode button
            {
                "ref": "SW1",
                "lib_id": "Switch:SW_Push",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                    "2": "+3.3V",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "boot0_button.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_boot0_pulldown(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "boot0_pulldown" and i.severity == "error"
        ]
        assert errors == []
        warnings = [
            i for i in issues
            if i.category == "boot0_pulldown" and i.severity == "warning"
        ]
        assert warnings == []

    def test_pulldown_to_vss_accepted(self, tmp_path: Path):
        """A pull-down to VSS (alternative GND name) should satisfy the check."""
        components = [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32:STM32F401",
                "pins": [
                    ("1", "BOOT0", "input"),
                    ("2", "VDD", "power_in"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                    "2": "+3.3V",
                },
            },
            {
                "ref": "R1",
                "lib_id": "Device:R",
                "pins": [
                    ("1", "~", "passive"),
                    ("2", "~", "passive"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                    "2": "VSS",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "pulldown_vss.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_boot0_pulldown(str(sch_path))
        warnings = [
            i for i in issues
            if i.category == "boot0_pulldown" and i.severity == "warning"
        ]
        assert warnings == []

    def test_warning_includes_sheet_location(self, tmp_path: Path):
        """Warning should report the sheet(s) where the net appears."""
        components = [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32:STM32C011F6Px",
                "pins": [
                    ("1", "BOOT0", "input"),
                ],
                "pin_nets": {
                    "1": "BOOT0",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "sheet_location.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_boot0_pulldown(str(sch_path))
        warnings = [
            i for i in issues
            if i.category == "boot0_pulldown" and i.severity == "warning"
        ]
        assert len(warnings) == 1
        assert warnings[0].location != ""

    def test_boot0_tied_to_spi_signal_error(self, tmp_path: Path):
        """BOOT0 tied to SPI signal pin should produce an error."""
        components = [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32:STM32C011F6Px",
                "pins": [
                    ("1", "BOOT0", "input"),
                    ("2", "VDD", "power_in"),
                ],
                "pin_nets": {
                    "1": "SPI_MOSI",
                    "2": "+3.3V",
                },
            },
            {
                "ref": "U2",
                "lib_id": "IC:Flash",
                "pins": [
                    ("1", "MOSI", "input"),
                ],
                "pin_nets": {
                    "1": "SPI_MOSI",
                },
            },
        ]
        sch_text = _build_schematic(components)
        sch_path = tmp_path / "boot0_spi.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_boot0_pulldown(str(sch_path))
        errors = [
            i for i in issues
            if i.category == "boot0_pulldown" and i.severity == "error"
        ]
        assert len(errors) >= 1
        assert any("MOSI" in e.message for e in errors)
