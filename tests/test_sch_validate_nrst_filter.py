"""Tests for STM32 NRST filter capacitor detection."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.sch_validate import (
    _is_capacitor,
    _is_nrst_pin,
    _is_stm32,
    _parse_capacitance,
    check_nrst_filter_cap,
)

# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestIsCapacitor:
    """Test _is_capacitor helper."""

    def test_bare_c(self):
        assert _is_capacitor("C") is True

    def test_c_small(self):
        assert _is_capacitor("C_Small") is True

    def test_c_polarized(self):
        assert _is_capacitor("C_Polarized") is True

    def test_device_c(self):
        assert _is_capacitor("Device:C") is True

    def test_device_c_small(self):
        assert _is_capacitor("Device:C_Small") is True

    def test_device_c_polarized(self):
        assert _is_capacitor("Device:C_Polarized") is True

    def test_resistor_not_cap(self):
        assert _is_capacitor("R") is False

    def test_inductor_not_cap(self):
        assert _is_capacitor("L") is False

    def test_ic_not_cap(self):
        assert _is_capacitor("MCU_ST_STM32C0:STM32C011F4Px") is False

    def test_connector_not_cap(self):
        assert _is_capacitor("Connector:Conn_01x02") is False


class TestIsNrstPin:
    """Test _is_nrst_pin helper."""

    def test_nrst(self):
        assert _is_nrst_pin("NRST") is True

    def test_active_low_nrst(self):
        assert _is_nrst_pin("~{NRST}") is True

    def test_nrst_mixed_case(self):
        assert _is_nrst_pin("nRST") is True

    def test_rst_not_nrst(self):
        assert _is_nrst_pin("RST") is False

    def test_boot0_not_nrst(self):
        assert _is_nrst_pin("BOOT0") is False

    def test_pa0_not_nrst(self):
        assert _is_nrst_pin("PA0") is False

    def test_reset_not_nrst(self):
        assert _is_nrst_pin("RESET") is False


class TestIsStm32:
    """Test _is_stm32 helper."""

    def test_stm32c0(self):
        assert _is_stm32("MCU_ST_STM32C0:STM32C011F4Px") is True

    def test_stm32f1(self):
        assert _is_stm32("MCU_ST_STM32F1:STM32F103C8Tx") is True

    def test_stm32h7(self):
        assert _is_stm32("MCU_ST_STM32H7:STM32H743ZITx") is True

    def test_non_stm32_mcu(self):
        assert _is_stm32("MCU_NXP_LPC:LPC1768") is False

    def test_passive(self):
        assert _is_stm32("Device:R") is False

    def test_connector(self):
        assert _is_stm32("Connector:Conn_01x02") is False


class TestParseCapacitance:
    """Test _parse_capacitance helper."""

    def test_100nf(self):
        val = _parse_capacitance("100n")
        assert val is not None
        assert abs(val - 100e-9) < 1e-15

    def test_100nf_with_unit(self):
        val = _parse_capacitance("100nF")
        assert val is not None
        assert abs(val - 100e-9) < 1e-15

    def test_1uf(self):
        val = _parse_capacitance("1u")
        assert val is not None
        assert abs(val - 1e-6) < 1e-15

    def test_0_1uf(self):
        val = _parse_capacitance("0.1uF")
        assert val is not None
        assert abs(val - 100e-9) < 1e-15

    def test_10pf(self):
        val = _parse_capacitance("10p")
        assert val is not None
        assert abs(val - 10e-12) < 1e-20

    def test_1pf(self):
        val = _parse_capacitance("1pF")
        assert val is not None
        assert abs(val - 1e-12) < 1e-20

    def test_empty(self):
        assert _parse_capacitance("") is None

    def test_unparseable(self):
        assert _parse_capacitance("abc") is None


# ---------------------------------------------------------------------------
# Helpers to generate synthetic KiCad schematics
# ---------------------------------------------------------------------------


def _make_lib_symbol(
    lib_id: str,
    pins: list[tuple[str, str, str]],
) -> str:
    """Generate a lib_symbols entry.

    Args:
        lib_id: e.g. "MCU_ST_STM32C0:STM32C011F4Px"
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
    value: str | None = None,
) -> str:
    """Generate a symbol instance S-expression."""
    pin_entries = "\n".join(f'(pin "{num}" (uuid "pin-{ref.lower()}-{num}"))' for num, _, _ in pins)
    val = value if value is not None else lib_id.split(":")[-1]
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
        (property "Value" "{val}"
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


def _make_nrst_schematic(
    *,
    cap_present: bool = False,
    cap_value: str = "100nF",
    cap_lib_id: str = "Device:C",
    mcu_lib_id: str = "MCU_ST_STM32C0:STM32C011F4Px",
    nrst_net: str = "NRST",
    gnd_net: str = "GND",
    cap_other_net: str | None = None,
) -> str:
    """Build a synthetic schematic with an STM32 MCU and optional NRST cap.

    Args:
        cap_present: Whether to include a filter capacitor.
        cap_value: Value string for the capacitor.
        cap_lib_id: lib_id for the capacitor symbol.
        mcu_lib_id: lib_id for the MCU symbol.
        nrst_net: Net name for the NRST pin.
        gnd_net: Net name for the capacitor ground side.
        cap_other_net: Override the capacitor's second-pin net (default: gnd_net).
    """
    mcu_pins = [
        ("1", "VDD", "power_in"),
        ("2", "VSS", "power_in"),
        ("3", "NRST", "input"),
        ("4", "PA0", "bidirectional"),
    ]
    lib_syms = [_make_lib_symbol(mcu_lib_id, mcu_pins)]
    sym_insts = [_make_symbol_instance("U1", mcu_lib_id, mcu_pins, 100.0, 50.0)]

    wires = []
    labels = []

    # Wire NRST pin (pin 3, y = 50.0 - 2*2.54 = 44.92)
    nrst_y = 50.0 - 2 * 2.54
    wires.append(
        f"""(wire
        (pts (xy 100.00 {nrst_y:.2f}) (xy 120.00 {nrst_y:.2f}))
        (stroke (width 0) (type default))
        (uuid "wire-nrst")
    )"""
    )
    labels.append(
        f"""(label "{nrst_net}"
        (at 120.00 {nrst_y:.2f} 0)
        (effects (font (size 1.27 1.27)) (justify left bottom))
        (uuid "lbl-nrst")
    )"""
    )

    if cap_present:
        cap_pins = [
            ("1", "~", "passive"),
            ("2", "~", "passive"),
        ]
        lib_syms.append(_make_lib_symbol(cap_lib_id, cap_pins))
        sym_insts.append(
            _make_symbol_instance("C1", cap_lib_id, cap_pins, 130.0, 50.0, value=cap_value)
        )

        # Wire cap pin 1 to NRST net
        wires.append(
            """(wire
        (pts (xy 130.00 50.00) (xy 140.00 50.00))
        (stroke (width 0) (type default))
        (uuid "wire-cap-1")
    )"""
        )
        labels.append(
            f"""(label "{nrst_net}"
        (at 140.00 50.00 0)
        (effects (font (size 1.27 1.27)) (justify left bottom))
        (uuid "lbl-cap-nrst")
    )"""
        )

        # Wire cap pin 2 to GND net
        second_net = cap_other_net if cap_other_net is not None else gnd_net
        cap_pin2_y = 50.0 - 2.54
        wires.append(
            f"""(wire
        (pts (xy 130.00 {cap_pin2_y:.2f}) (xy 140.00 {cap_pin2_y:.2f}))
        (stroke (width 0) (type default))
        (uuid "wire-cap-2")
    )"""
        )
        labels.append(
            f"""(label "{second_net}"
        (at 140.00 {cap_pin2_y:.2f} 0)
        (effects (font (size 1.27 1.27)) (justify left bottom))
        (uuid "lbl-cap-gnd")
    )"""
        )

    lib_sym_block = "\n".join(lib_syms)
    sym_block = "\n".join(sym_insts)
    wire_block = "\n".join(wires)
    label_block = "\n".join(labels)

    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-nrst-filter-uuid")
    (paper "A4")
    (lib_symbols
        {lib_sym_block}
    )
    {sym_block}
    {wire_block}
    {label_block}
)
"""


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestCheckNrstFilterCap:
    """Test check_nrst_filter_cap against synthetic schematics."""

    def test_missing_cap_flagged(self, tmp_path: Path):
        """STM32 with no cap on NRST should produce a warning."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(_make_nrst_schematic(cap_present=False))

        issues = check_nrst_filter_cap(str(sch_file))
        warnings = [i for i in issues if i.severity == "warning" and i.category == "nrst_filter"]
        assert len(warnings) == 1
        assert "NRST" in warnings[0].message
        assert "U1" in warnings[0].message

    def test_cap_present_no_warning(self, tmp_path: Path):
        """STM32 with 100nF cap from NRST to GND should pass."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(_make_nrst_schematic(cap_present=True, cap_value="100nF"))

        issues = check_nrst_filter_cap(str(sch_file))
        warnings = [i for i in issues if i.severity == "warning" and i.category == "nrst_filter"]
        assert len(warnings) == 0

    def test_cap_out_of_range_flagged(self, tmp_path: Path):
        """STM32 with 1pF cap on NRST should warn about value range."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(_make_nrst_schematic(cap_present=True, cap_value="1pF"))

        issues = check_nrst_filter_cap(str(sch_file))
        warnings = [i for i in issues if i.severity == "warning" and i.category == "nrst_filter"]
        assert len(warnings) == 1
        assert "outside" in warnings[0].message.lower() or "range" in warnings[0].message.lower()

    def test_cap_not_to_gnd_flagged(self, tmp_path: Path):
        """Cap from NRST to a non-GND net should not count as a filter cap."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            _make_nrst_schematic(cap_present=True, cap_value="100nF", cap_other_net="+3V3")
        )

        issues = check_nrst_filter_cap(str(sch_file))
        warnings = [i for i in issues if i.severity == "warning" and i.category == "nrst_filter"]
        assert len(warnings) == 1
        assert "no filter capacitor" in warnings[0].message.lower()

    def test_non_stm32_mcu_no_warning(self, tmp_path: Path):
        """Non-STM32 MCU should not trigger the NRST check."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            _make_nrst_schematic(
                cap_present=False,
                mcu_lib_id="MCU_NXP_LPC:LPC1768FBD100",
            )
        )

        issues = check_nrst_filter_cap(str(sch_file))
        warnings = [i for i in issues if i.severity == "warning" and i.category == "nrst_filter"]
        assert len(warnings) == 0

    def test_c_small_counts(self, tmp_path: Path):
        """C_Small symbol should be recognized as a capacitor."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(
            _make_nrst_schematic(cap_present=True, cap_value="100nF", cap_lib_id="Device:C_Small")
        )

        issues = check_nrst_filter_cap(str(sch_file))
        warnings = [i for i in issues if i.severity == "warning" and i.category == "nrst_filter"]
        assert len(warnings) == 0

    def test_active_low_nrst_notation(self, tmp_path: Path):
        """Verify check works even when NRST net is present but MCU uses standard pin name."""
        # This is implicitly tested via the standard schematic builder which
        # uses pin name "NRST" -- the _is_nrst_pin function handles ~{NRST}.
        # We test the helper directly above; this integration test just ensures
        # the happy path still works with the default setup.
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(_make_nrst_schematic(cap_present=True, cap_value="100nF"))

        issues = check_nrst_filter_cap(str(sch_file))
        warnings = [i for i in issues if i.severity == "warning" and i.category == "nrst_filter"]
        assert len(warnings) == 0

    def test_cap_at_boundary_10nf(self, tmp_path: Path):
        """10nF cap should be within the accepted range (no warning)."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(_make_nrst_schematic(cap_present=True, cap_value="10nF"))

        issues = check_nrst_filter_cap(str(sch_file))
        warnings = [i for i in issues if i.severity == "warning" and i.category == "nrst_filter"]
        assert len(warnings) == 0

    def test_cap_at_boundary_1uf(self, tmp_path: Path):
        """1uF cap should be within the accepted range (no warning)."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.write_text(_make_nrst_schematic(cap_present=True, cap_value="1uF"))

        issues = check_nrst_filter_cap(str(sch_file))
        warnings = [i for i in issues if i.severity == "warning" and i.category == "nrst_filter"]
        assert len(warnings) == 0
