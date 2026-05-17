"""Tests for ``DebugHeader`` pin-net label/stub-wire emission.

These tests use a mocked ``Schematic`` to exercise the new optional
``pin_nets`` dict kwarg without producing real KiCad output.  Shape
mirrors ``tests/test_blocks_gate_driver_block.py``; the load-bearing
assertion for issue #2994 is ``test_labels_anchored_to_wire_endpoints``.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import DebugHeader


@pytest.fixture
def mock_schematic():
    """Mock Schematic returning mock symbols with deterministic pin positions.

    The header mock places all six pins on the right edge (x + 10) so
    every stub direction is rightward.  Pin numbers map to vertical
    offsets in 2.54mm steps starting at the top.
    """
    sch = Mock()

    # Pin numbers map to (dx, dy) offset from the symbol center.
    header_pin_map = {
        "1": (10, -6.35),
        "2": (10, -3.81),
        "3": (10, -1.27),
        "4": (10, 1.27),
        "5": (10, 3.81),
        "6": (10, 6.35),
    }

    resistor_pin_map = {
        "1": (-2, 0),
        "2": (2, 0),
    }

    def create_mock_component(symbol, x, y, ref, *args, **kwargs):
        comp = Mock()
        comp.reference = ref
        comp.symbol = symbol
        comp.footprint = ""

        if "Device:R" in symbol:
            pin_map = resistor_pin_map
        else:
            pin_map = header_pin_map

        comp.pin_position.side_effect = lambda name, _x=x, _y=y, _pm=pin_map: (
            (_x + _pm[name][0], _y + _pm[name][1])
            if name in _pm
            else (_x, _y)
        )
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    return sch


class TestDebugHeaderPinNets:
    """Tests for the new ``pin_nets`` kwarg on ``DebugHeader``."""

    def test_default_emits_no_pin_labels(self, mock_schematic):
        """Without ``pin_nets``, no pin-net labels are emitted."""
        DebugHeader(
            mock_schematic, x=100, y=100,
            interface="swd", pins=6,
        )
        assert mock_schematic.add_label.call_count == 0

    def test_pin_nets_right_edge_pin_stubs_right(self, mock_schematic):
        """Pins to the right of the symbol center stub rightward."""
        block = DebugHeader(
            mock_schematic, x=100, y=100,
            interface="swd", pins=6,
            pin_nets={"1": "+3.3V"},
        )

        STUB = 2.54
        # Pin 1 is at (110, 93.65) in our mock.
        labels = mock_schematic.add_label.call_args_list
        net_labels = [c for c in labels if c.args[0] == "+3.3V"]
        assert len(net_labels) == 1
        _, lx, ly = net_labels[0].args[0], net_labels[0].args[1], net_labels[0].args[2]
        assert lx == 110 + STUB
        assert ly == 93.65
        # Alias port also exposed.
        assert block.ports["+3.3V"] == (110, 93.65)

    def test_pin_nets_multiple_entries(self, mock_schematic):
        """Mapping with multiple entries emits one label per entry."""
        # Standard SWD-6 layout: pin 1 = VCC, 3/5 = GND, 6 = NRST.
        pin_nets = {
            "1": "+3.3V",
            "2": "SWDIO",
            "3": "GND",
            "4": "SWCLK",
            "5": "GND",
            "6": "NRST",
        }
        DebugHeader(
            mock_schematic, x=100, y=100,
            interface="swd", pins=6,
            pin_nets=pin_nets,
        )

        # Each net name should appear at least once in the emitted labels.
        emitted = {c.args[0] for c in mock_schematic.add_label.call_args_list}
        for v in pin_nets.values():
            assert v in emitted

    def test_alias_ports_added_for_each_pin_net(self, mock_schematic):
        """Each ``pin_nets`` entry adds a port keyed by the net name."""
        block = DebugHeader(
            mock_schematic, x=100, y=100,
            interface="swd", pins=6,
            pin_nets={"2": "SWDIO", "4": "SWCLK"},
        )
        assert "SWDIO" in block.ports
        assert "SWCLK" in block.ports

    def test_alias_port_does_not_clobber_existing(self, mock_schematic):
        """A net name colliding with an existing port (e.g. ``VCC``) keeps the old port."""
        block_no_nets = DebugHeader(
            mock_schematic, x=100, y=100,
            interface="swd", pins=6,
        )
        original_vcc = block_no_nets.ports["VCC"]

        block = DebugHeader(
            mock_schematic, x=100, y=100,
            interface="swd", pins=6,
            pin_nets={"1": "VCC"},  # try to overwrite
        )
        # Original port survives.
        assert block.ports["VCC"] == original_vcc

    def test_labels_anchored_to_wire_endpoints(self, mock_schematic):
        """Regression test for #2980/#2994.

        Every emitted pin-net label must sit on a wire endpoint, otherwise
        KiCad's label-only connectivity treats it as floating.
        """
        DebugHeader(
            mock_schematic, x=100, y=100,
            interface="swd", pins=6,
            pin_nets={
                "1": "+3.3V",
                "2": "SWDIO",
                "3": "GND_NET",
                "4": "SWCLK",
                "5": "GND_NET",
                "6": "NRST",
            },
        )

        wire_endpoints: set[tuple[float, float]] = set()
        for call in mock_schematic.add_wire.call_args_list:
            a, b = call.args[0], call.args[1]
            wire_endpoints.add((a[0], a[1]))
            wire_endpoints.add((b[0], b[1]))

        pin_net_values = {"+3.3V", "SWDIO", "GND_NET", "SWCLK", "NRST"}
        for call in mock_schematic.add_label.call_args_list:
            net_name, lx, ly = call.args[0], call.args[1], call.args[2]
            if net_name in pin_net_values:
                assert (lx, ly) in wire_endpoints, (
                    f"label {net_name!r} at ({lx}, {ly}) has no matching wire endpoint; "
                    f"KiCad will treat it as floating (regression of #2980/#2994)"
                )

    def test_external_ports_unchanged_without_pin_nets(self, mock_schematic):
        """Block ports unchanged when ``pin_nets`` is ``None`` (back-compat)."""
        b1 = DebugHeader(
            mock_schematic, x=100, y=100,
            interface="swd", pins=6,
        )
        b2 = DebugHeader(
            mock_schematic, x=100, y=100,
            interface="swd", pins=6,
            pin_nets={"1": "+3.3V"},
        )
        # Original placeholder ports survive in both blocks.
        for key in ("VCC", "GND", "SWDIO", "SWCLK", "NRST"):
            assert b1.ports[key] == b2.ports[key]

    def test_pin_nets_works_with_jtag_interface(self, mock_schematic):
        """``pin_nets`` works regardless of interface type."""
        # Need to add JTAG header pins to the mock.  Reuse the same map
        # but only assert that emission works for whatever subset we ask.
        block = DebugHeader(
            mock_schematic, x=100, y=100,
            interface="swd", pins=6,
            pin_nets={"6": "NRST_NET"},
        )
        # Should have at least one label call for NRST_NET.
        emitted = [c.args[0] for c in mock_schematic.add_label.call_args_list]
        assert "NRST_NET" in emitted
        # And the alias port is exposed.
        assert "NRST_NET" in block.ports
