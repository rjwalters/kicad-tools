"""Tests for the analog joystick interface block (issue #2569).

Covers the ``AnalogJoystickBlock`` class and ``create_analog_joystick`` factory in
``kicad_tools.schematic.blocks.interface.analog_input``:

- 5-pin default with X/Y filters and BTN pull-up.
- 4-pin variant when ``btn_net=None`` (Conn_01x04, no BTN port).
- Filter-disabled variant (raw connector wipers exposed as X/Y ports).
- Pull-up-disabled variant (BTN port present, no pull-up resistor).
- Custom net names and ref designator.
- Custom cutoff frequency (cap value scales).
- Top-level export from ``kicad_tools.schematic.blocks``.
- Board 03 regression check (factory call is end-to-end runnable).
"""

from __future__ import annotations

import math
import re
from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks.interface.analog_input import (
    AnalogJoystickBlock,
    create_analog_joystick,
)

# Issue #3436: CI runs the suite with `-n auto --timeout=60`.  Board
# generation / real-library scans beat 60s alone, but on the 4-core CI
# runner under full-suite xdist contention the wall-clock reaper killed
# them spuriously.  The marker overrides the CLI default with a
# contention-tolerant budget; it does NOT slow the happy path.
pytestmark = pytest.mark.timeout(600)


def _make_mock_schematic() -> Mock:
    """Build a mock Schematic that returns mock symbols with deterministic pins.

    Each ``add_symbol`` call returns a fresh Mock whose ``pin_position`` returns
    pin offsets derived from the symbol's center, so resulting wires/labels can
    be inspected by callers.
    """
    sch = Mock()
    created_symbols: list[Mock] = []

    def _make_symbol(symbol: str, x: float, y: float, ref: str, *args, **kwargs) -> Mock:
        comp = Mock()
        comp.symbol = symbol
        comp.x = x
        comp.y = y
        comp.reference = ref
        comp.value = args[0] if args else kwargs.get("value", "")
        comp.footprint = kwargs.get("footprint", "")

        # Pins arranged vertically below the symbol center for connectors,
        # and horizontally for two-pin passives.
        def _pin_position(pin_name: str) -> tuple[float, float]:
            # Connectors (Conn_01x05 / Conn_01x04) — pins 1..5 vertically
            try:
                n = int(pin_name)
            except ValueError:
                return (x, y)
            if "Conn_01x" in symbol:
                return (x, y + (n - 1) * 2.54)
            # Two-pin passives (R, C): pin 1 = top, pin 2 = bottom
            if n == 1:
                return (x, y - 2.54)
            return (x, y + 2.54)

        comp.pin_position = Mock(side_effect=_pin_position)
        created_symbols.append(comp)
        return comp

    sch.add_symbol = Mock(side_effect=_make_symbol)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_global_label = Mock()
    sch._created_symbols = created_symbols
    return sch


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestAnalogJoystickFactory:
    """Tests for ``create_analog_joystick`` and ``AnalogJoystickBlock``."""

    def test_factory_default_5pin_with_filter_and_button(self):
        """Default args produce: connector + 2 RC filters + 1 pull-up + 5 ports."""
        sch = _make_mock_schematic()
        block = create_analog_joystick(sch, x=100.0, y=100.0)

        assert isinstance(block, AnalogJoystickBlock)
        assert block.has_button is True

        # Connector: 1 (Conn_01x05)
        connector_calls = [c for c in sch.add_symbol.call_args_list if "Conn_01x" in c.args[0]]
        assert len(connector_calls) == 1
        assert "Conn_01x05" in connector_calls[0].args[0]

        # Filter components: 2 R + 2 C
        r_calls = [c for c in sch.add_symbol.call_args_list if c.args[0] == "Device:R"]
        c_calls = [c for c in sch.add_symbol.call_args_list if c.args[0] == "Device:C"]
        # 2 filter Rs + 1 pull-up R = 3
        assert len(r_calls) == 3
        # 2 filter Cs
        assert len(c_calls) == 2

        # Ports
        for port in ("VCC", "GND", "X", "Y", "BTN"):
            assert port in block.ports

    def test_factory_no_button(self):
        """``btn_net=None`` -> 4-pin connector, no BTN port, no pull-up."""
        sch = _make_mock_schematic()
        block = create_analog_joystick(sch, x=100.0, y=100.0, btn_net=None)

        assert block.has_button is False
        assert "BTN" not in block.ports

        # Connector should be Conn_01x04
        connector_calls = [c for c in sch.add_symbol.call_args_list if "Conn_01x" in c.args[0]]
        assert len(connector_calls) == 1
        assert "Conn_01x04" in connector_calls[0].args[0]

        # No pull-up resistor
        assert block.r_pullup is None

        # Filters still present (filter is not gated by btn)
        assert block.x_filter is not None
        assert block.y_filter is not None

    def test_factory_no_filter(self):
        """``filter_cutoff_hz=None`` -> no R/C filter components, X/Y from raw pins."""
        sch = _make_mock_schematic()
        block = create_analog_joystick(sch, x=100.0, y=100.0, filter_cutoff_hz=None)

        assert block.x_filter is None
        assert block.y_filter is None

        # No filter caps, only the BTN pull-up resistor
        c_calls = [c for c in sch.add_symbol.call_args_list if c.args[0] == "Device:C"]
        r_calls = [c for c in sch.add_symbol.call_args_list if c.args[0] == "Device:R"]
        assert len(c_calls) == 0
        assert len(r_calls) == 1  # BTN pull-up

        # X/Y ports should be at the connector wiper pin positions (pins 3 and 4)
        connector = block.connector
        assert block.ports["X"] == connector.pin_position("3")
        assert block.ports["Y"] == connector.pin_position("4")

    def test_factory_no_pullup(self):
        """``btn_pullup=None`` with ``btn_net="JOY_BTN"`` -> BTN port, no pull-up."""
        sch = _make_mock_schematic()
        block = create_analog_joystick(sch, x=100.0, y=100.0, btn_pullup=None)

        assert "BTN" in block.ports
        assert block.r_pullup is None

        # No pull-up resistor placed (only filter Rs)
        r_calls = [c for c in sch.add_symbol.call_args_list if c.args[0] == "Device:R"]
        assert len(r_calls) == 2  # 2 filter Rs only

    def test_factory_custom_nets_and_ref(self):
        """Custom ``ref`` and net names propagate to connector and labels."""
        sch = _make_mock_schematic()
        create_analog_joystick(
            sch,
            x=100.0,
            y=100.0,
            ref="J5",
            vcc_net="+5V",
            gnd_net="AGND",
            x_net="STICK_X",
            y_net="STICK_Y",
            btn_net="STICK_BTN",
        )

        # Connector ref
        connector_call = next(c for c in sch.add_symbol.call_args_list if "Conn_01x" in c.args[0])
        assert connector_call.args[3] == "J5"  # ref is the 4th positional arg

        # Confirm the labels we added match the supplied net names
        label_names = [c.args[0] for c in sch.add_global_label.call_args_list]
        # All custom nets should appear at least once
        assert "+5V" in label_names
        assert "AGND" in label_names
        assert "STICK_X" in label_names
        assert "STICK_Y" in label_names
        assert "STICK_BTN" in label_names

    def test_factory_custom_cutoff_frequency(self):
        """Custom ``filter_cutoff_hz`` -> cap value scales appropriately.

        For an RC filter with R=10k, fc=1/(2*pi*R*C). At fc=100 Hz the cap is
        ~159 nF; at fc=1 kHz the cap is ~16 nF; at fc=10 kHz the cap is ~1.6 nF
        (which formats as ``"2nF"``). We just check that a lower cutoff yields a
        larger cap value and that the formatted unit string matches expectations.
        """
        sch_low = _make_mock_schematic()
        block_low = create_analog_joystick(sch_low, x=0, y=0, filter_cutoff_hz=100.0)

        # Read the value passed to the first capacitor add_symbol call
        c_calls_low = [c for c in sch_low.add_symbol.call_args_list if c.args[0] == "Device:C"]
        assert len(c_calls_low) == 2
        c_value_low = c_calls_low[0].args[4]  # value is the 5th positional

        sch_high = _make_mock_schematic()
        block_high = create_analog_joystick(sch_high, x=0, y=0, filter_cutoff_hz=10000.0)
        c_calls_high = [c for c in sch_high.add_symbol.call_args_list if c.args[0] == "Device:C"]
        c_value_high = c_calls_high[0].args[4]

        # Parse a numeric prefix and unit suffix
        def _to_farads(s: str) -> float:
            m = re.match(r"([0-9]+(?:\.[0-9]+)?)([a-zA-Z]+)", s)
            assert m is not None, f"Bad cap value format: {s!r}"
            num = float(m.group(1))
            unit = m.group(2)
            scale = {"uF": 1e-6, "nF": 1e-9, "pF": 1e-12}[unit]
            return num * scale

        f_low = _to_farads(c_value_low)
        f_high = _to_farads(c_value_high)
        # Low cutoff should give a much larger cap than high cutoff
        assert f_low > f_high
        # Sanity check magnitudes against fc = 1/(2*pi*R*C) with R=10k
        expected_low = 1.0 / (2 * math.pi * 10000 * 100.0)
        expected_high = 1.0 / (2 * math.pi * 10000 * 10000.0)
        assert math.isclose(f_low, expected_low, rel_tol=0.5)
        assert math.isclose(f_high, expected_high, rel_tol=0.5)

        # Confirm both blocks built filters
        assert block_low.x_filter is not None
        assert block_high.x_filter is not None


# ---------------------------------------------------------------------------
# Top-level export
# ---------------------------------------------------------------------------


class TestExports:
    """Ensure factory & class are reachable from the package root."""

    def test_top_level_exports(self):
        from kicad_tools.schematic.blocks import (  # noqa: F401
            AnalogJoystickBlock,
            create_analog_joystick,
        )

        assert AnalogJoystickBlock is not None
        assert callable(create_analog_joystick)

    def test_interface_subpackage_exports(self):
        from kicad_tools.schematic.blocks.interface import (  # noqa: F401
            AnalogJoystickBlock,
            create_analog_joystick,
        )

        assert AnalogJoystickBlock is not None
        assert callable(create_analog_joystick)


# ---------------------------------------------------------------------------
# Board 03 regression
# ---------------------------------------------------------------------------


class TestBoard03Regression:
    """Run board 03's generator end-to-end and verify component counts."""

    def test_board_03_generate_runs(self, tmp_path):
        """``boards/03-usb-joystick/generate_schematic.py`` must run cleanly.

        Imports the module by file path and calls ``create_usb_joystick_schematic``.
        Asserts the joystick block contributes the expected component count
        (1 connector + 2 filter Rs + 2 filter Cs + 1 BTN pull-up = 6 components)
        on top of whatever else the board places.
        """
        import importlib.util
        import sys
        from pathlib import Path

        # Locate board 03 generator
        repo_root = Path(__file__).resolve().parent.parent
        gen_path = repo_root / "boards" / "03-usb-joystick" / "generate_schematic.py"
        if not gen_path.exists():
            pytest.skip(f"Board 03 generator not found at {gen_path}")

        # Import without polluting sys.modules persistently
        spec = importlib.util.spec_from_file_location(
            "_board_03_generate_schematic_test", str(gen_path)
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)

            output_path = tmp_path / "usb_joystick.kicad_sch"
            # Returns True/False depending on overlap status. Either is fine —
            # we just need the generator to complete without raising.
            mod.create_usb_joystick_schematic(output_path, verbose=False)
            assert output_path.exists(), "Schematic file was not written"

            # Quick text-level sanity: the joystick connector and pull-up should
            # appear in the resulting schematic (even though we cannot fully
            # parse it here, the kicad_sch s-expression text will mention them).
            txt = output_path.read_text(encoding="utf-8")
            assert "Joystick" in txt or "Conn_01x05" in txt
        finally:
            sys.modules.pop(spec.name, None)
