"""Tests for SWD debug pin routing detection on STM32 MCUs."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.sch_validate import (
    _is_stm32_mcu,
    _swd_net_ok,
    _swd_override_present,
    check_swd_pin_routing,
    validate_schematic,
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
        lib_id: e.g. "MCU_ST:STM32C011F6"
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
    pins: list[tuple[str, str, str]],
    x: float,
    y: float,
) -> str:
    """Generate a symbol instance S-expression."""
    pin_entries = "\n".join(f'(pin "{num}" (uuid "pin-{ref.lower()}-{num}"))' for num, _, _ in pins)
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
        (property "Value" "{lib_id.split(":")[-1]}"
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
) -> str:
    """Build a complete schematic with one IC and wired labels.

    Args:
        lib_id: Library ID for the symbol.
        pins: List of (pin_number, pin_name, pin_type).
        pin_nets: Mapping of pin_number -> net_name for connected pins.
        ref: Reference designator.
    """
    lib_sym = _make_ic_lib_symbol(lib_id, pins)
    sym_x, sym_y = 100.0, 50.0
    sym_inst = _make_symbol_instance(ref, lib_id, pins, sym_x, sym_y)

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
    (uuid "test-swd-routing-uuid")
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
# STM32 MCU pin definitions used across tests
# ---------------------------------------------------------------------------

_STM32_PINS = [
    ("1", "VDD", "power_in"),
    ("2", "VSS", "power_in"),
    ("3", "PA0", "bidirectional"),
    ("4", "PA13", "bidirectional"),
    ("5", "PA14", "bidirectional"),
    ("6", "PA15", "bidirectional"),
    ("7", "PB3", "bidirectional"),
    ("8", "PB4", "bidirectional"),
]


# ---------------------------------------------------------------------------
# Unit tests -- pure function tests
# ---------------------------------------------------------------------------


class TestIsStm32Mcu:
    def test_standard_lib_id(self):
        assert _is_stm32_mcu("MCU_ST:STM32C011F6") is True

    def test_underscore_lib_id(self):
        assert _is_stm32_mcu("MCU_ST_STM32:STM32F401RET6") is True

    def test_custom_lib_starting_with_stm32(self):
        assert _is_stm32_mcu("STM32G071RB") is True

    def test_non_stm32(self):
        assert _is_stm32_mcu("MCU_Microchip:ATSAMD21G18A") is False

    def test_audio_codec(self):
        assert _is_stm32_mcu("Audio_Codec:PCM5122") is False


class TestSwdNetOk:
    def test_exact_match_swdio(self):
        assert _swd_net_ok("SWDIO", "SWDIO") is True

    def test_prefixed_swdio(self):
        assert _swd_net_ok("MCU_SWDIO", "SWDIO") is True

    def test_underscore_separated(self):
        assert _swd_net_ok("SWD_IO", "SWDIO") is True

    def test_swclk_match(self):
        assert _swd_net_ok("SWCLK", "SWCLK") is True

    def test_jtdi_match(self):
        assert _swd_net_ok("JTAG_TDI", "JTDI") is True  # "JTAG" token matches JT* prefix
        assert _swd_net_ok("JTDI", "JTDI") is True

    def test_mismatch(self):
        assert _swd_net_ok("I2S_SYNC", "SWDIO") is False

    def test_spi_sck_not_swclk(self):
        assert _swd_net_ok("SPI_SCK", "SWCLK") is False


class TestSwdOverridePresent:
    def test_gpio_in_name(self):
        assert _swd_override_present("GPIO_DEBUG") is True

    def test_no_gpio(self):
        assert _swd_override_present("I2S_SYNC") is False

    def test_gpio_prefix(self):
        assert _swd_override_present("GPIO13") is True


# ---------------------------------------------------------------------------
# Integration tests with synthetic schematics
# ---------------------------------------------------------------------------


class TestCheckSwdPinRouting:
    """Test check_swd_pin_routing against synthetic schematics."""

    def test_correct_swd_wiring_no_issues(self, tmp_path: Path):
        """STM32 with PA13->SWDIO and PA14->SWCLK produces no issues."""
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            "4": "SWDIO",
            "5": "SWCLK",
        }
        sch_text = _make_schematic("MCU_ST:STM32C011F6", _STM32_PINS, pin_nets)
        sch_path = tmp_path / "correct_swd.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_swd_pin_routing(str(sch_path))
        swd_issues = [i for i in issues if i.category == "swd_routing"]
        assert swd_issues == [], f"Unexpected issues: {swd_issues}"

    def test_pa13_connected_to_i2s_sync_error(self, tmp_path: Path):
        """PA13 connected to I2S_SYNC should flag an error."""
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            "4": "I2S_SYNC",
            "5": "SWCLK",
        }
        sch_text = _make_schematic("MCU_ST:STM32C011F6", _STM32_PINS, pin_nets)
        sch_path = tmp_path / "bad_pa13.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_swd_pin_routing(str(sch_path))
        swd_errors = [i for i in issues if i.category == "swd_routing" and i.severity == "error"]
        assert len(swd_errors) >= 1
        assert "PA13" in swd_errors[0].message
        assert "I2S_SYNC" in swd_errors[0].message

    def test_pa14_connected_to_spi_sck_error(self, tmp_path: Path):
        """PA14 connected to SPI_SCK should flag an error."""
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            "4": "SWDIO",
            "5": "SPI_SCK",
        }
        sch_text = _make_schematic("MCU_ST:STM32C011F6", _STM32_PINS, pin_nets)
        sch_path = tmp_path / "bad_pa14.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_swd_pin_routing(str(sch_path))
        swd_errors = [i for i in issues if i.category == "swd_routing" and i.severity == "error"]
        assert len(swd_errors) >= 1
        assert "PA14" in swd_errors[0].message
        assert "SPI_SCK" in swd_errors[0].message

    def test_pa14_correct_no_error(self, tmp_path: Path):
        """PA14 connected to SWCLK should produce no error."""
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            "4": "SWDIO",
            "5": "SWCLK",
        }
        sch_text = _make_schematic("MCU_ST:STM32C011F6", _STM32_PINS, pin_nets)
        sch_path = tmp_path / "correct_pa14.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_swd_pin_routing(str(sch_path))
        swd_errors = [i for i in issues if i.category == "swd_routing" and i.severity == "error"]
        assert swd_errors == []

    def test_gpio_override_suppresses_error(self, tmp_path: Path):
        """PA13 connected to GPIO_DEBUG should NOT flag an error."""
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            "4": "GPIO_DEBUG",
            "5": "SWCLK",
        }
        sch_text = _make_schematic("MCU_ST:STM32C011F6", _STM32_PINS, pin_nets)
        sch_path = tmp_path / "gpio_override.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_swd_pin_routing(str(sch_path))
        swd_issues = [i for i in issues if i.category == "swd_routing"]
        # PA13 is suppressed by GPIO, so no error for it
        pa13_issues = [i for i in swd_issues if "PA13" in i.message]
        assert pa13_issues == [], f"PA13 should be suppressed: {pa13_issues}"

    def test_non_stm32_skipped(self, tmp_path: Path):
        """MCU with non-STM32 lib_id should not trigger SWD check."""
        pins = [
            ("1", "VDD", "power_in"),
            ("2", "VSS", "power_in"),
            ("3", "PA13", "bidirectional"),
            ("4", "PA14", "bidirectional"),
        ]
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            "3": "SOME_SIGNAL",
            "4": "OTHER_SIGNAL",
        }
        sch_text = _make_schematic("MCU_Microchip:ATSAMD21G18A", pins, pin_nets)
        sch_path = tmp_path / "non_stm32.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_swd_pin_routing(str(sch_path))
        swd_issues = [i for i in issues if i.category == "swd_routing"]
        assert swd_issues == [], f"Non-STM32 should produce no SWD issues: {swd_issues}"

    def test_unconnected_swd_pins_warning(self, tmp_path: Path):
        """Unconnected PA13/PA14 should emit warnings."""
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            # PA13 (pin 4) and PA14 (pin 5) not connected
        }
        sch_text = _make_schematic("MCU_ST:STM32C011F6", _STM32_PINS, pin_nets)
        sch_path = tmp_path / "unconnected_swd.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_swd_pin_routing(str(sch_path))
        swd_warnings = [
            i for i in issues if i.category == "swd_routing" and i.severity == "warning"
        ]
        # Should warn about both PA13 and PA14
        warning_pins = {i.message for i in swd_warnings}
        pa13_warned = any("PA13" in m for m in warning_pins)
        pa14_warned = any("PA14" in m for m in warning_pins)
        assert pa13_warned, "Expected warning for unconnected PA13"
        assert pa14_warned, "Expected warning for unconnected PA14"

    def test_stm32f4_lib_id_detected(self, tmp_path: Path):
        """STM32F4 family lib_id is detected as STM32."""
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            "4": "I2S_SYNC",  # Wrong net for PA13
            "5": "SWCLK",
        }
        sch_text = _make_schematic("MCU_ST:STM32F401RET6", _STM32_PINS, pin_nets)
        sch_path = tmp_path / "stm32f4.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_swd_pin_routing(str(sch_path))
        swd_errors = [i for i in issues if i.category == "swd_routing" and i.severity == "error"]
        assert len(swd_errors) >= 1, "STM32F4 should be detected and checked"


class TestSkipFlag:
    """Test the --skip CLI flag integration."""

    def test_skip_swd_routing(self, tmp_path: Path):
        """validate_schematic with skip_checks={'swd_routing'} skips the check."""
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            "4": "I2S_SYNC",  # Wrong -- would normally error
            "5": "SPI_SCK",  # Wrong -- would normally error
        }
        sch_text = _make_schematic("MCU_ST:STM32C011F6", _STM32_PINS, pin_nets)
        sch_path = tmp_path / "skip_test.kicad_sch"
        sch_path.write_text(sch_text)

        result = validate_schematic(str(sch_path), skip_checks={"swd_routing"})
        assert "swd_routing" not in result.checks_run
        swd_issues = [i for i in result.issues if i.category == "swd_routing"]
        assert swd_issues == []

    def test_no_skip_runs_swd_routing(self, tmp_path: Path):
        """validate_schematic without skip runs swd_routing check."""
        pin_nets = {
            "1": "+3V3",
            "2": "GND",
            "4": "SWDIO",
            "5": "SWCLK",
        }
        sch_text = _make_schematic("MCU_ST:STM32C011F6", _STM32_PINS, pin_nets)
        sch_path = tmp_path / "no_skip.kicad_sch"
        sch_path.write_text(sch_text)

        result = validate_schematic(str(sch_path))
        assert "swd_routing" in result.checks_run
