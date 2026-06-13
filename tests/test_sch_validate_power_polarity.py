"""Tests for power pin polarity error detection (VDD/GND swap)."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.sch_validate import (
    check_power_pin_polarity,
)

# ---------------------------------------------------------------------------
# Helpers to generate synthetic KiCad schematics
# ---------------------------------------------------------------------------
# Reuse the same pattern as test_sch_validate_power_short.py


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
    (uuid "test-power-polarity-uuid")
    (paper "A4")
    (lib_symbols
        {lib_sym}
    )
    {sym_inst}
    {wire_block}
    {label_block}
)
"""


def _make_power_symbol_schematic(
    net_name: str,
) -> str:
    """Build a schematic with a power symbol (lib_id power:...)."""
    lib_id = "power:GND"
    pins = [("1", "GND", "power_in")]
    lib_sym = _make_ic_lib_symbol(lib_id, pins)
    sym_inst = _make_symbol_instance("#PWR01", lib_id, pins, 100.0, 50.0)

    wire = """(wire
        (pts (xy 100.00 50.00) (xy 110.00 50.00))
        (stroke (width 0) (type default))
        (uuid "wire-pwr-1")
    )"""
    label = f"""(label "{net_name}"
        (at 110.00 50.00 0)
        (effects (font (size 1.27 1.27)) (justify left bottom))
        (uuid "lbl-pwr-1")
    )"""

    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-power-sym-uuid")
    (paper "A4")
    (lib_symbols
        {lib_sym}
    )
    {sym_inst}
    {wire}
    {label}
)
"""


def _make_power_symbol_entry(
    lib_id: str,
    value: str,
    ref: str,
    x: float,
    y: float,
) -> tuple[str, str]:
    """Generate a power symbol's lib_symbols entry and symbol instance.

    Returns (lib_sym_sexp, symbol_instance_sexp).
    """
    part_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id
    lib_sym = f"""(symbol "{lib_id}"
            (pin_names (offset 0))
            (symbol "{part_name}_0_1"
                (polyline
                    (pts (xy 0 0) (xy 0 -1.27))
                    (stroke (width 0))
                    (fill (type none))
                )
            )
            (symbol "{part_name}_1_1"
                (pin power_in line
                    (at 0 0 0)
                    (length 0)
                    (name "{value}")
                    (number "1")
                )
            )
        )"""
    sym_inst = f"""(symbol
        (lib_id "{lib_id}")
        (at {x} {y} 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp no)
        (uuid "uuid-{ref.lower()}")
        (property "Reference" "{ref}"
            (at {x + 2} {y - 2} 0)
            (effects (font (size 1.27 1.27)) (justify left) hide)
        )
        (property "Value" "{value}"
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
        (pin "1" (uuid "pin-{ref.lower()}-1"))
    )"""
    return lib_sym, sym_inst


def _make_ic_with_power_symbols_schematic(
    ic_lib_id: str,
    ic_pins: list[tuple[str, str, str]],
    pin_power_symbols: dict[str, tuple[str, str]],
    ic_ref: str = "U1",
) -> str:
    """Build a schematic with an IC and power symbols providing nets.

    Instead of labels, each IC power pin is connected via a wire to a
    power symbol (e.g., ``power:+3.3V``).

    Args:
        ic_lib_id: e.g. "IC:SomeChip"
        ic_pins: list of (pin_number, pin_name, pin_type) tuples
        pin_power_symbols: Maps IC pin_number -> (power_lib_id, power_value)
            e.g. {"1": ("power:+3V3", "+3.3V"), "2": ("power:GND", "GND")}
        ic_ref: reference designator for the IC
    """
    ic_lib_sym = _make_ic_lib_symbol(ic_lib_id, ic_pins)
    ic_sym_inst = _make_symbol_instance(
        ic_ref,
        ic_lib_id,
        ic_pins,
        100.0,
        50.0,
    )

    power_lib_syms = []
    power_sym_insts = []
    wires = []

    pwr_idx = 0
    for pin_idx, (pin_num, _, _) in enumerate(ic_pins):
        if pin_num not in pin_power_symbols:
            continue
        pwr_lib_id, pwr_value = pin_power_symbols[pin_num]
        pwr_ref = f"#PWR{pwr_idx:02d}"
        pwr_idx += 1

        pin_y = 50.0 - pin_idx * 2.54
        pin_x = 100.0
        pwr_x = pin_x + 10.0

        pwr_lib, pwr_inst = _make_power_symbol_entry(
            pwr_lib_id,
            pwr_value,
            pwr_ref,
            pwr_x,
            pin_y,
        )
        power_lib_syms.append(pwr_lib)
        power_sym_insts.append(pwr_inst)

        wires.append(
            f"""(wire
            (pts (xy {pin_x:.2f} {pin_y:.2f}) (xy {pwr_x:.2f} {pin_y:.2f}))
            (stroke (width 0) (type default))
            (uuid "wire-pwr-{ic_ref.lower()}-{pin_num}")
        )"""
        )

    all_lib_syms = ic_lib_sym + "\n" + "\n".join(power_lib_syms)
    all_sym_insts = ic_sym_inst + "\n" + "\n".join(power_sym_insts)
    wire_block = "\n".join(wires)

    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-power-polarity-pwrsym-uuid")
    (paper "A4")
    (lib_symbols
        {all_lib_syms}
    )
    {all_sym_insts}
    {wire_block}
)
"""


def _make_ic_with_unresolved_pins_schematic(
    ic_lib_id: str,
    ic_pins: list[tuple[str, str, str]],
    ic_ref: str = "U1",
) -> str:
    """Build a schematic with an IC whose pins have no wires/labels.

    This simulates the case where ``resolve_pin_map`` returns ``net=None``
    for power pins because nothing connects them to a net name.
    """
    ic_lib_sym = _make_ic_lib_symbol(ic_lib_id, ic_pins)
    ic_sym_inst = _make_symbol_instance(
        ic_ref,
        ic_lib_id,
        ic_pins,
        100.0,
        50.0,
    )

    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-power-polarity-unresolved-uuid")
    (paper "A4")
    (lib_symbols
        {ic_lib_sym}
    )
    {ic_sym_inst}
)
"""


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestCheckPowerPinPolarity:
    """Test check_power_pin_polarity against synthetic schematics."""

    def test_vdd_pin_on_gnd_net_flagged(self, tmp_path: Path):
        """VDD pin connected to a GND net should be flagged as error."""
        pins = [
            ("1", "VDD", "power_in"),
            ("2", "GND", "power_in"),
            ("3", "SDA", "bidirectional"),
        ]
        pin_nets = {
            "1": "GNDD",  # VDD pin on a ground net -- polarity error
            "2": "GND",  # GND pin on GND net -- correct
            "3": "I2C_SDA",
        }
        sch_text = _make_single_ic_schematic("IC:Oscillator", pins, pin_nets)
        sch_path = tmp_path / "vdd_on_gnd.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert len(polarity_errors) == 1
        assert "VDD" in polarity_errors[0].message
        assert "GNDD" in polarity_errors[0].message
        assert "positive supply pin" in polarity_errors[0].message

    def test_gnd_pin_on_positive_net_flagged(self, tmp_path: Path):
        """GND pin connected to a positive supply net should be flagged."""
        pins = [
            ("1", "VDD", "power_in"),
            ("2", "GND", "power_in"),
        ]
        pin_nets = {
            "1": "+3.3V",  # Correct
            "2": "+3.3V",  # GND pin on positive net -- polarity error
        }
        sch_text = _make_single_ic_schematic("IC:Chip", pins, pin_nets)
        sch_path = tmp_path / "gnd_on_vcc.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert len(polarity_errors) == 1
        assert "GND" in polarity_errors[0].message
        assert "+3.3V" in polarity_errors[0].message
        assert "ground pin" in polarity_errors[0].message

    def test_correct_wiring_no_issues(self, tmp_path: Path):
        """VDD on positive net and GND on negative net should not be flagged."""
        pins = [
            ("1", "VDD", "power_in"),
            ("2", "GND", "power_in"),
            ("3", "OUT", "output"),
        ]
        pin_nets = {
            "1": "+3.3V",
            "2": "GND",
            "3": "SIG_OUT",
        }
        sch_text = _make_single_ic_schematic("IC:NormalChip", pins, pin_nets)
        sch_path = tmp_path / "correct_wiring.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert polarity_errors == []

    def test_ambiguous_pin_name_skipped(self, tmp_path: Path):
        """Pins with ambiguous names (EP, V+) should not produce false positives."""
        pins = [
            ("1", "EP", "power_in"),
            ("2", "NC", "power_in"),
        ]
        pin_nets = {
            "1": "GND",
            "2": "+3.3V",
        }
        sch_text = _make_single_ic_schematic("IC:QFN", pins, pin_nets)
        sch_path = tmp_path / "ambiguous_pin.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert polarity_errors == []

    def test_power_symbol_skipped(self, tmp_path: Path):
        """Power symbols (lib_id power:*) should be skipped entirely."""
        sch_text = _make_power_symbol_schematic("+3.3V")
        sch_path = tmp_path / "power_sym.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert polarity_errors == []

    def test_multiple_pins_only_swapped_reported(self, tmp_path: Path):
        """Multiple power pins on one symbol: only swapped ones reported."""
        pins = [
            ("1", "AVDD", "power_in"),
            ("2", "DVDD", "power_in"),
            ("3", "AGND", "power_in"),
            ("4", "DGND", "power_in"),
        ]
        pin_nets = {
            "1": "+3.3V",  # Correct
            "2": "GNDD",  # Swapped -- positive pin on negative net
            "3": "GND",  # Correct
            "4": "+5V",  # Swapped -- negative pin on positive net
        }
        sch_text = _make_single_ic_schematic("IC:MultiPower", pins, pin_nets)
        sch_path = tmp_path / "multi_power.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert len(polarity_errors) == 2
        messages = [e.message for e in polarity_errors]
        # DVDD on GNDD
        assert any("DVDD" in m and "GNDD" in m for m in messages)
        # DGND on +5V
        assert any("DGND" in m and "+5V" in m for m in messages)

    def test_vcc_on_vss_net_flagged(self, tmp_path: Path):
        """VCC pin on VSS net should be flagged."""
        pins = [("1", "VCC", "power_in")]
        pin_nets = {"1": "VSS"}
        sch_text = _make_single_ic_schematic("IC:Chip", pins, pin_nets)
        sch_path = tmp_path / "vcc_on_vss.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert len(polarity_errors) == 1
        assert "VCC" in polarity_errors[0].message
        assert "VSS" in polarity_errors[0].message

    def test_vss_on_vdd_net_flagged(self, tmp_path: Path):
        """VSS pin on VDD net should be flagged."""
        pins = [("1", "VSS", "power_in")]
        pin_nets = {"1": "VDD"}
        sch_text = _make_single_ic_schematic("IC:Chip", pins, pin_nets)
        sch_path = tmp_path / "vss_on_vdd.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert len(polarity_errors) == 1
        assert "VSS" in polarity_errors[0].message
        assert "ground pin" in polarity_errors[0].message

    def test_non_power_pin_type_ignored(self, tmp_path: Path):
        """Non-power pin types should not be checked even if names match."""
        pins = [
            ("1", "VCC", "input"),  # Not power_in
            ("2", "GND", "passive"),  # Not power_in
        ]
        pin_nets = {
            "1": "GND",  # Would be a polarity error if power pin
            "2": "+3.3V",  # Would be a polarity error if power pin
        }
        sch_text = _make_single_ic_schematic("IC:Weird", pins, pin_nets)
        sch_path = tmp_path / "non_power_type.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert polarity_errors == []

    def test_pin_on_non_power_net_skipped(self, tmp_path: Path):
        """Power pin on a non-power net (e.g. signal net) should be skipped."""
        pins = [("1", "VDD", "power_in")]
        pin_nets = {"1": "SOME_SIGNAL"}
        sch_text = _make_single_ic_schematic("IC:Chip", pins, pin_nets)
        sch_path = tmp_path / "signal_net.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert polarity_errors == []

    def test_voltage_pattern_net_positive(self, tmp_path: Path):
        """Net names like +5V, +1.8V should be classified as positive."""
        pins = [("1", "GND", "power_in")]
        pin_nets = {"1": "+5V"}
        sch_text = _make_single_ic_schematic("IC:Chip", pins, pin_nets)
        sch_path = tmp_path / "voltage_net.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert len(polarity_errors) == 1
        assert "GND" in polarity_errors[0].message
        assert "+5V" in polarity_errors[0].message


class TestPowerSymbolFixtures:
    """Tests using power symbols (power:+3.3V etc.) instead of labels."""

    def test_correct_wiring_with_power_symbols(self, tmp_path: Path):
        """VDD on +3.3V power symbol, GND on GND power symbol -- no errors."""
        ic_pins = [
            ("1", "VDD", "power_in"),
            ("2", "GND", "power_in"),
            ("3", "OUT", "output"),
        ]
        pin_power = {
            "1": ("power:+3V3", "+3.3V"),
            "2": ("power:GND", "GND"),
        }
        sch_text = _make_ic_with_power_symbols_schematic(
            "IC:NormalChip",
            ic_pins,
            pin_power,
        )
        sch_path = tmp_path / "correct_power_sym.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert polarity_errors == []

    def test_reversed_vdd_gnd_with_power_symbols(self, tmp_path: Path):
        """VDD pin wired to GND power symbol should be flagged."""
        ic_pins = [
            ("1", "VDD", "power_in"),
            ("2", "GND", "power_in"),
        ]
        pin_power = {
            "1": ("power:GND", "GND"),  # Reversed -- VDD on GND
            "2": ("power:+3V3", "+3.3V"),  # Reversed -- GND on +3.3V
        }
        sch_text = _make_ic_with_power_symbols_schematic(
            "IC:Oscillator",
            ic_pins,
            pin_power,
        )
        sch_path = tmp_path / "reversed_power_sym.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert len(polarity_errors) == 2
        messages = [e.message for e in polarity_errors]
        assert any("VDD" in m and "GND" in m for m in messages)
        assert any("GND" in m and "+3.3V" in m for m in messages)

    def test_single_reversed_vcc_with_power_symbol(self, tmp_path: Path):
        """VCC pin wired to GNDD power symbol -- only that pin flagged."""
        ic_pins = [
            ("1", "VCC", "power_in"),
            ("2", "GND", "power_in"),
            ("3", "SDA", "bidirectional"),
        ]
        pin_power = {
            "1": ("power:GNDD", "GNDD"),  # Reversed -- VCC on GNDD
            "2": ("power:GND", "GND"),  # Correct
        }
        sch_text = _make_ic_with_power_symbols_schematic(
            "IC:I2CChip",
            ic_pins,
            pin_power,
        )
        sch_path = tmp_path / "single_reversed_power_sym.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert len(polarity_errors) == 1
        assert "VCC" in polarity_errors[0].message
        assert "GNDD" in polarity_errors[0].message
        assert "positive supply pin" in polarity_errors[0].message

    def test_multi_power_pins_with_power_symbols(self, tmp_path: Path):
        """Multiple power pins -- only swapped ones reported via power symbols."""
        ic_pins = [
            ("1", "AVDD", "power_in"),
            ("2", "DVDD", "power_in"),
            ("3", "AGND", "power_in"),
            ("4", "DGND", "power_in"),
        ]
        pin_power = {
            "1": ("power:+3V3", "+3.3V"),  # Correct
            "2": ("power:GNDD", "GNDD"),  # Swapped
            "3": ("power:GND", "GND"),  # Correct
            "4": ("power:+5V", "+5V"),  # Swapped
        }
        sch_text = _make_ic_with_power_symbols_schematic(
            "IC:MultiPower",
            ic_pins,
            pin_power,
        )
        sch_path = tmp_path / "multi_power_sym.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        polarity_errors = [
            i for i in issues if i.category == "power_polarity" and i.severity == "error"
        ]
        assert len(polarity_errors) == 2
        messages = [e.message for e in polarity_errors]
        assert any("DVDD" in m and "GNDD" in m for m in messages)
        assert any("DGND" in m and "+5V" in m for m in messages)


class TestUnresolvedPowerPinWarning:
    """Tests for the warning emitted when power pins have net=None."""

    def test_unresolved_power_pin_emits_warning(self, tmp_path: Path):
        """Power pin with no wire/label should emit a warning."""
        ic_pins = [
            ("1", "VDD", "power_in"),
            ("2", "GND", "power_in"),
            ("3", "OUT", "output"),
        ]
        sch_text = _make_ic_with_unresolved_pins_schematic(
            "IC:Floating",
            ic_pins,
        )
        sch_path = tmp_path / "unresolved_power.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        warnings = [
            i
            for i in issues
            if i.category == "power_polarity"
            and i.severity == "warning"
            and "no resolved net" in i.message
        ]
        # Both VDD and GND should get warnings
        assert len(warnings) == 2
        warning_msgs = [w.message for w in warnings]
        assert any("VDD" in m for m in warning_msgs)
        assert any("GND" in m for m in warning_msgs)

    def test_unresolved_non_power_pin_no_warning(self, tmp_path: Path):
        """Non-power pins with no net should not emit warnings."""
        ic_pins = [
            ("1", "SDA", "bidirectional"),
            ("2", "SCL", "bidirectional"),
        ]
        sch_text = _make_ic_with_unresolved_pins_schematic(
            "IC:I2C",
            ic_pins,
        )
        sch_path = tmp_path / "unresolved_signal.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        warnings = [
            i
            for i in issues
            if i.category == "power_polarity"
            and i.severity == "warning"
            and "no resolved net" in i.message
        ]
        assert warnings == []

    def test_unresolved_ambiguous_power_pin_no_warning(self, tmp_path: Path):
        """Power pins with ambiguous names (EP, NC) should not warn."""
        ic_pins = [
            ("1", "EP", "power_in"),
            ("2", "NC", "power_in"),
        ]
        sch_text = _make_ic_with_unresolved_pins_schematic(
            "IC:QFN",
            ic_pins,
        )
        sch_path = tmp_path / "unresolved_ambiguous.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_power_pin_polarity(str(sch_path))
        warnings = [
            i
            for i in issues
            if i.category == "power_polarity"
            and i.severity == "warning"
            and "no resolved net" in i.message
        ]
        assert warnings == []
