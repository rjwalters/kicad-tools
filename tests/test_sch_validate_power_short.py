"""Tests for power net short detection (VCC-to-GND on same net)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_validate import (
    ValidationIssue,
    check_power_net_shorts,
)


# ---------------------------------------------------------------------------
# Helpers to generate synthetic KiCad schematics with power pins
# ---------------------------------------------------------------------------


def _make_ic_lib_symbol(
    lib_id: str,
    pins: list[tuple[str, str, str]],
) -> str:
    """Generate a lib_symbols entry for an IC.

    Args:
        lib_id: e.g. "IC:SomeChip"
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


def _make_two_ic_schematic(
    lib_id1: str,
    pins1: list[tuple[str, str, str]],
    pin_nets1: dict[str, str],
    ref1: str,
    lib_id2: str,
    pins2: list[tuple[str, str, str]],
    pin_nets2: dict[str, str],
    ref2: str,
) -> str:
    """Build a schematic with two ICs where some pins share the same net."""
    lib_sym1 = _make_ic_lib_symbol(lib_id1, pins1)
    lib_sym2 = _make_ic_lib_symbol(lib_id2, pins2)
    sym_inst1 = _make_symbol_instance(ref1, lib_id1, pins1, 100.0, 50.0)
    sym_inst2 = _make_symbol_instance(ref2, lib_id2, pins2, 200.0, 50.0)

    wires = []
    labels = []

    # Wire up first IC
    for pin_idx, (pin_num, _, _) in enumerate(pins1):
        if pin_num not in pin_nets1:
            continue
        net_name = pin_nets1[pin_num]
        pin_y = 50.0 - pin_idx * 2.54
        pin_x = 100.0
        label_x = pin_x + 10.0

        wires.append(
            f"""(wire
            (pts (xy {pin_x:.2f} {pin_y:.2f}) (xy {label_x:.2f} {pin_y:.2f}))
            (stroke (width 0) (type default))
            (uuid "wire-{ref1.lower()}-{pin_num}")
        )"""
        )
        labels.append(
            f"""(label "{net_name}"
            (at {label_x:.2f} {pin_y:.2f} 0)
            (effects (font (size 1.27 1.27)) (justify left bottom))
            (uuid "lbl-{ref1.lower()}-{pin_num}")
        )"""
        )

    # Wire up second IC
    for pin_idx, (pin_num, _, _) in enumerate(pins2):
        if pin_num not in pin_nets2:
            continue
        net_name = pin_nets2[pin_num]
        pin_y = 50.0 - pin_idx * 2.54
        pin_x = 200.0
        label_x = pin_x + 10.0

        wires.append(
            f"""(wire
            (pts (xy {pin_x:.2f} {pin_y:.2f}) (xy {label_x:.2f} {pin_y:.2f}))
            (stroke (width 0) (type default))
            (uuid "wire-{ref2.lower()}-{pin_num}")
        )"""
        )
        labels.append(
            f"""(label "{net_name}"
            (at {label_x:.2f} {pin_y:.2f} 0)
            (effects (font (size 1.27 1.27)) (justify left bottom))
            (uuid "lbl-{ref2.lower()}-{pin_num}")
        )"""
        )

    wire_block = "\n".join(wires)
    label_block = "\n".join(labels)

    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-power-short-uuid")
    (paper "A4")
    (lib_symbols
        {lib_sym1}
        {lib_sym2}
    )
    {sym_inst1}
    {sym_inst2}
    {wire_block}
    {label_block}
)
"""


def _make_single_ic_schematic(
    lib_id: str,
    pins: list[tuple[str, str, str]],
    pin_nets: dict[str, str],
    ref: str = "U1",
) -> str:
    """Build a schematic with a single IC."""
    lib_sym = _make_ic_lib_symbol(lib_id, pins)
    sym_inst = _make_symbol_instance(ref, lib_id, pins, 100.0, 50.0)

    wires = []
    labels = []
    for pin_idx, (pin_num, _, _) in enumerate(pins):
        if pin_num not in pin_nets:
            continue
        net_name = pin_nets[pin_num]
        pin_y = 50.0 - pin_idx * 2.54
        pin_x = 100.0
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
    (uuid "test-power-short-uuid")
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
# Integration tests
# ---------------------------------------------------------------------------


class TestCheckPowerNetShorts:
    """Test check_power_net_shorts against synthetic schematics."""

    def test_vcc_gnd_same_net_flagged(self, tmp_path: Path):
        """A net connecting both VCC and GND power pins should be flagged."""
        # Two ICs: one has VCC power_in, the other has GND power_in,
        # both on the same net "BAD_NET"
        pins1 = [
            ("1", "VCC", "power_in"),
            ("2", "SDA", "bidirectional"),
        ]
        pins2 = [
            ("1", "GND", "power_in"),
            ("2", "SCL", "bidirectional"),
        ]
        pin_nets1 = {"1": "BAD_NET", "2": "I2C_SDA"}
        pin_nets2 = {"1": "BAD_NET", "2": "I2C_SCL"}

        sch_text = _make_two_ic_schematic(
            "IC:ChipA", pins1, pin_nets1, "U1",
            "IC:ChipB", pins2, pin_nets2, "U2",
        )
        sch_path = tmp_path / "vcc_gnd_short.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_net_shorts(str(sch_path))
        power_errors = [
            i for i in issues
            if i.category == "power_short" and i.severity == "error"
        ]
        assert len(power_errors) >= 1
        assert any("BAD_NET" in i.message for i in power_errors)
        assert any("positive" in i.message and "negative" in i.message for i in power_errors)

    def test_normal_power_no_issues(self, tmp_path: Path):
        """Separate VCC and GND nets should not be flagged."""
        pins = [
            ("1", "VCC", "power_in"),
            ("2", "GND", "power_in"),
            ("3", "SDA", "bidirectional"),
        ]
        pin_nets = {
            "1": "VCC_3V3",
            "2": "GND_NET",
            "3": "I2C_SDA",
        }
        sch_text = _make_single_ic_schematic("IC:NormalChip", pins, pin_nets)
        sch_path = tmp_path / "normal_power.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_net_shorts(str(sch_path))
        power_errors = [
            i for i in issues
            if i.category == "power_short" and i.severity == "error"
        ]
        assert power_errors == []

    def test_multiple_vcc_pins_same_net_ok(self, tmp_path: Path):
        """Multiple VCC pins on the same net should not be flagged."""
        pins1 = [
            ("1", "VCC", "power_in"),
            ("2", "SDA", "bidirectional"),
        ]
        pins2 = [
            ("1", "VDD", "power_in"),
            ("2", "SCL", "bidirectional"),
        ]
        pin_nets1 = {"1": "POWER_3V3", "2": "I2C_SDA"}
        pin_nets2 = {"1": "POWER_3V3", "2": "I2C_SCL"}

        sch_text = _make_two_ic_schematic(
            "IC:ChipA", pins1, pin_nets1, "U1",
            "IC:ChipB", pins2, pin_nets2, "U2",
        )
        sch_path = tmp_path / "multi_vcc.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_net_shorts(str(sch_path))
        power_errors = [
            i for i in issues
            if i.category == "power_short" and i.severity == "error"
        ]
        assert power_errors == []

    def test_non_power_pins_ignored(self, tmp_path: Path):
        """Non-power pin types should not be checked for power shorts."""
        pins = [
            ("1", "VCC", "input"),    # Not power_in -- should be ignored
            ("2", "GND", "passive"),   # Not power_in -- should be ignored
        ]
        pin_nets = {
            "1": "SHARED_NET",
            "2": "SHARED_NET",
        }
        sch_text = _make_single_ic_schematic("IC:Weird", pins, pin_nets)
        sch_path = tmp_path / "non_power.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_net_shorts(str(sch_path))
        power_errors = [
            i for i in issues
            if i.category == "power_short" and i.severity == "error"
        ]
        assert power_errors == []

    def test_avcc_agnd_same_net_flagged(self, tmp_path: Path):
        """AVCC and AGND on the same net should be flagged."""
        pins1 = [("1", "AVCC", "power_in")]
        pins2 = [("1", "AGND", "power_in")]
        pin_nets1 = {"1": "ANALOG_SHORT"}
        pin_nets2 = {"1": "ANALOG_SHORT"}

        sch_text = _make_two_ic_schematic(
            "IC:AnalogA", pins1, pin_nets1, "U1",
            "IC:AnalogB", pins2, pin_nets2, "U2",
        )
        sch_path = tmp_path / "avcc_agnd.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_net_shorts(str(sch_path))
        power_errors = [
            i for i in issues
            if i.category == "power_short" and i.severity == "error"
        ]
        assert len(power_errors) >= 1
        assert any("ANALOG_SHORT" in i.message for i in power_errors)

    def test_error_message_lists_pins(self, tmp_path: Path):
        """Error message should list the conflicting pin references."""
        pins1 = [("1", "VCC", "power_in")]
        pins2 = [("1", "GND", "power_in")]
        pin_nets1 = {"1": "SHORT_NET"}
        pin_nets2 = {"1": "SHORT_NET"}

        sch_text = _make_two_ic_schematic(
            "IC:A", pins1, pin_nets1, "U1",
            "IC:B", pins2, pin_nets2, "U2",
        )
        sch_path = tmp_path / "msg_check.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_net_shorts(str(sch_path))
        power_errors = [
            i for i in issues
            if i.category == "power_short" and i.severity == "error"
        ]
        assert len(power_errors) == 1
        msg = power_errors[0].message
        assert "U1" in msg
        assert "U2" in msg
