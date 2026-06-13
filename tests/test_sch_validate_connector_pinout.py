"""Tests for connector pinout validation against known interface standards."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.sch_validate import (
    _INTERFACE_CATALOG,
    _match_interface,
    _normalise_signal,
    check_connector_pinout,
)

# ---------------------------------------------------------------------------
# Helpers to generate synthetic KiCad schematics with connectors
# ---------------------------------------------------------------------------


def _make_connector_lib_symbol(pin_count: int) -> str:
    """Generate a lib_symbols entry for a generic N-pin connector."""
    lib_id = f"Connector_Generic:Conn_01x{pin_count:02d}"
    pins = []
    for i in range(1, pin_count + 1):
        # Pins are spaced 2.54mm apart vertically, starting at y=0
        y = (i - 1) * 2.54
        pins.append(
            f"""(pin passive line
                    (at 0 {y:.2f} 0)
                    (length 2.54)
                    (name "Pin_{i}")
                    (number "{i}")
                )"""
        )
    pin_block = "\n".join(pins)
    return f"""(symbol "{lib_id}"
            (pin_names hide)
            (symbol "{lib_id.split(":")[1]}_0_1"
                (rectangle
                    (start -1.27 -{(pin_count * 2.54) + 1.27:.2f})
                    (end 1.27 1.27)
                    (stroke (width 0.254))
                    (fill (type none))
                )
            )
            (symbol "{lib_id.split(":")[1]}_1_1"
                {pin_block}
            )
        )"""


def _make_symbol_instance(ref: str, lib_id: str, pin_count: int, x: float, y: float) -> str:
    """Generate a symbol instance S-expression."""
    pin_entries = "\n".join(
        f'(pin "{i}" (uuid "pin-{ref.lower()}-{i}"))' for i in range(1, pin_count + 1)
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
        (property "Value" "{lib_id.split(":")[1]}"
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


def _make_schematic_with_connector(
    pin_count: int,
    pin_nets: dict[str, str],
    ref: str = "J1",
    lib_id: str | None = None,
) -> str:
    """Build a complete schematic string with one connector and labels.

    Args:
        pin_count: Number of pins on the connector.
        pin_nets: Mapping of pin number (str) to net-name label.
        ref: Reference designator.
        lib_id: Library ID (defaults to Connector_Generic:Conn_01xNN).
    """
    if lib_id is None:
        lib_id = f"Connector_Generic:Conn_01x{pin_count:02d}"

    lib_sym = _make_connector_lib_symbol(pin_count)
    sym_x, sym_y = 100.0, 50.0
    sym_inst = _make_symbol_instance(ref, lib_id, pin_count, sym_x, sym_y)

    # For each pin that has a net, place a wire from the pin position to a
    # label 10mm to the right.
    wires = []
    labels = []
    for pin_num_str, net_name in pin_nets.items():
        pin_idx = int(pin_num_str) - 1
        pin_y = sym_y - pin_idx * 2.54  # Library Y-up -> schematic Y-down
        pin_x = sym_x  # pin is at (sym_x, pin_y)
        label_x = pin_x + 10.0

        wires.append(
            f"""(wire
            (pts (xy {pin_x:.2f} {pin_y:.2f}) (xy {label_x:.2f} {pin_y:.2f}))
            (stroke (width 0) (type default))
            (uuid "wire-{ref.lower()}-{pin_num_str}")
        )"""
        )

        # Use global_label for power nets, local label for signals
        if net_name in ("GND", "+3.3V", "+5V", "VCC"):
            labels.append(
                f"""(global_label "{net_name}"
                (at {label_x:.2f} {pin_y:.2f} 0)
                (shape input)
                (effects (font (size 1.27 1.27)) (justify left))
                (uuid "gl-{ref.lower()}-{pin_num_str}")
            )"""
            )
        else:
            labels.append(
                f"""(label "{net_name}"
                (at {label_x:.2f} {pin_y:.2f} 0)
                (effects (font (size 1.27 1.27)) (justify left bottom))
                (uuid "lbl-{ref.lower()}-{pin_num_str}")
            )"""
            )

    wire_block = "\n".join(wires)
    label_block = "\n".join(labels)

    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-connector-pinout-uuid")
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


class TestNormaliseSignal:
    def test_plain_signal_unchanged(self):
        assert _normalise_signal("SWDIO") == "SWDIO"

    def test_power_rail_normalised(self):
        assert _normalise_signal("+3.3V") == "VCC"
        assert _normalise_signal("+5V") == "VCC"

    def test_gnd_unchanged(self):
        assert _normalise_signal("GND") == "GND"

    def test_none_returns_none(self):
        assert _normalise_signal(None) is None


class TestMatchInterface:
    def test_swd_6pin_matched(self):
        signals = {"VCC", "SWDIO", "GND", "SWCLK", "NRST"}
        result = _match_interface(signals, 6)
        assert result is not None
        assert result["name"] == "ARM SWD 6-pin"

    def test_swd_10pin_matched(self):
        signals = {"VCC", "SWDIO", "GND", "SWCLK", "SWO", "NRST"}
        result = _match_interface(signals, 10)
        assert result is not None
        assert result["name"] == "ARM SWD 10-pin"

    def test_jtag_20pin_matched(self):
        signals = {"VCC", "GND", "TDI", "TDO", "TMS", "TCK", "TRST", "NRST", "RTCK"}
        result = _match_interface(signals, 20)
        assert result is not None
        assert result["name"] == "ARM JTAG 20-pin"

    def test_no_match_arbitrary_signals(self):
        signals = {"NET_A", "NET_B", "NET_C"}
        result = _match_interface(signals, 6)
        assert result is None

    def test_no_match_wrong_pin_count(self):
        """SWD signals on a 4-pin connector should not match the 6-pin standard."""
        signals = {"VCC", "SWDIO", "GND", "SWCLK"}
        result = _match_interface(signals, 4)
        assert result is None


# ---------------------------------------------------------------------------
# Integration-level tests using synthetic schematics
# ---------------------------------------------------------------------------


class TestCheckConnectorPinout:
    """Test check_connector_pinout against synthetic schematic fixtures."""

    def test_correct_swd_6pin_no_issues(self, tmp_path: Path):
        """A correctly wired SWD 6-pin header should produce no issues."""
        pin_nets = {
            "1": "VCC",
            "2": "SWDIO",
            "3": "GND",
            "4": "SWCLK",
            "5": "GND",
            "6": "NRST",
        }
        sch_text = _make_schematic_with_connector(6, pin_nets)
        sch_path = tmp_path / "correct_swd.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_connector_pinout(str(sch_path))
        pinout_issues = [
            i for i in issues if i.category == "connector_pinout" and i.severity == "error"
        ]
        assert pinout_issues == [], f"Unexpected issues: {pinout_issues}"

    def test_swapped_swd_pins_produce_errors(self, tmp_path: Path):
        """SWDIO and SWCLK swapped should produce exactly two errors."""
        pin_nets = {
            "1": "VCC",
            "2": "SWCLK",  # should be SWDIO
            "3": "GND",
            "4": "SWDIO",  # should be SWCLK
            "5": "GND",
            "6": "NRST",
        }
        sch_text = _make_schematic_with_connector(6, pin_nets)
        sch_path = tmp_path / "swapped_swd.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_connector_pinout(str(sch_path))
        pinout_errors = [
            i for i in issues if i.category == "connector_pinout" and i.severity == "error"
        ]

        assert len(pinout_errors) == 2

        messages = [i.message for i in pinout_errors]
        assert any("pin 2" in m and "expected SWDIO" in m and "got SWCLK" in m for m in messages)
        assert any("pin 4" in m and "expected SWCLK" in m and "got SWDIO" in m for m in messages)

    def test_unknown_interface_skipped(self, tmp_path: Path):
        """A connector with arbitrary nets should produce no issues."""
        pin_nets = {
            "1": "NET_A",
            "2": "NET_B",
            "3": "NET_C",
            "4": "NET_D",
            "5": "NET_E",
            "6": "NET_F",
        }
        sch_text = _make_schematic_with_connector(6, pin_nets)
        sch_path = tmp_path / "unknown.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_connector_pinout(str(sch_path))
        pinout_issues = [
            i for i in issues if i.category == "connector_pinout" and i.severity == "error"
        ]
        assert pinout_issues == []

    def test_correct_jtag_20pin_no_issues(self, tmp_path: Path):
        """A correctly wired JTAG 20-pin header should produce no issues."""
        from kicad_tools.schematic.blocks.interface.debug import DebugHeader

        # Build pin_nets from the standard, skipping NC/KEY
        pin_nets = {}
        for pin_num, signal in DebugHeader.JTAG_20PIN_PINOUT.items():
            if signal not in ("NC", "KEY"):
                pin_nets[pin_num] = signal

        sch_text = _make_schematic_with_connector(20, pin_nets)
        sch_path = tmp_path / "correct_jtag.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_connector_pinout(str(sch_path))
        pinout_errors = [
            i for i in issues if i.category == "connector_pinout" and i.severity == "error"
        ]
        assert pinout_errors == [], f"Unexpected issues: {pinout_errors}"

    def test_power_rail_alias_accepted(self, tmp_path: Path):
        """Using +3.3V instead of VCC should be accepted as correct."""
        pin_nets = {
            "1": "+3.3V",  # alias for VCC
            "2": "SWDIO",
            "3": "GND",
            "4": "SWCLK",
            "5": "GND",
            "6": "NRST",
        }
        sch_text = _make_schematic_with_connector(6, pin_nets)
        sch_path = tmp_path / "power_alias.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_connector_pinout(str(sch_path))
        pinout_errors = [
            i for i in issues if i.category == "connector_pinout" and i.severity == "error"
        ]
        assert pinout_errors == [], f"Unexpected issues: {pinout_errors}"

    def test_non_connector_symbols_ignored(self, tmp_path: Path):
        """Non-connector symbols (resistors etc.) should not be checked."""
        # Use the simple_rc fixture content directly -- just verify no crash
        from kicad_tools.cli.sch_validate import check_connector_pinout

        fixtures_dir = Path(__file__).parent / "fixtures"
        simple_rc = fixtures_dir / "simple_rc.kicad_sch"
        if not simple_rc.exists():
            pytest.skip("simple_rc fixture not available")

        issues = check_connector_pinout(str(simple_rc))
        pinout_issues = [
            i for i in issues if i.category == "connector_pinout" and i.severity == "error"
        ]
        assert pinout_issues == []

    def test_error_message_includes_standard_name(self, tmp_path: Path):
        """Error messages should include the standard name for context."""
        pin_nets = {
            "1": "VCC",
            "2": "SWCLK",  # wrong
            "3": "GND",
            "4": "SWDIO",  # wrong
            "5": "GND",
            "6": "NRST",
        }
        sch_text = _make_schematic_with_connector(6, pin_nets)
        sch_path = tmp_path / "msg_check.kicad_sch"
        sch_path.write_text(sch_text)

        issues = check_connector_pinout(str(sch_path))
        pinout_errors = [i for i in issues if i.category == "connector_pinout"]
        assert all("ARM SWD 6-pin" in i.message for i in pinout_errors)

    def test_catalog_is_extensible(self):
        """The catalog should be a list of dicts, allowing easy extension."""
        assert isinstance(_INTERFACE_CATALOG, list)
        for entry in _INTERFACE_CATALOG:
            assert "name" in entry
            assert "identifier_signals" in entry
            assert "pin_count" in entry
            assert "pinout" in entry
            assert isinstance(entry["identifier_signals"], set)
            assert isinstance(entry["pinout"], dict)
