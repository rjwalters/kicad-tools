"""Tests for ``LDOBlock`` pin-net label/stub-wire emission.

These tests use a mocked ``Schematic`` to exercise the new optional
``pin_nets`` dict kwarg without producing real KiCad output.  Shape
mirrors ``tests/test_blocks_gate_driver_block.py``; the load-bearing
assertion for issue #2994 is ``test_labels_anchored_to_wire_endpoints``.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import LDOBlock
from kicad_tools.schematic.exceptions import PinNotFoundError


@pytest.fixture
def mock_schematic():
    """Mock Schematic returning mock symbols with deterministic pin positions.

    The LDO mock advertises pins for the AMS1117-shaped pinout used on
    board 05: VI (input), VO (output), GND.  Pin positions are chosen so
    we exercise both stub directions:

      - Left-edge pin (x - 10): VI
      - Right-edge pin (x + 10): VO
      - Center / bottom: GND
    """
    sch = Mock()

    ldo_pin_map_template = {
        "VI": (-10, -5),
        "VIN": (-10, -5),  # alias for XC6206 / generic
        "IN": (-10, -5),
        "VO": (10, -5),
        "VOUT": (10, -5),
        "OUT": (10, -5),
        "GND": (0, 10),
        "1": (0, 10),  # GND by number (AMS1117)
        "2": (10, -5),  # VO by number
        "3": (-10, -5),  # VI by number
    }

    cap_pin_map = {
        "1": (0, -2),
        "2": (0, 2),
    }

    def create_mock_component(symbol, x, y, ref, *args, **kwargs):
        comp = Mock()
        comp.reference = ref
        comp.symbol = symbol
        comp.footprint = ""

        if "Device:C" in symbol:
            pin_map = cap_pin_map
        else:
            pin_map = ldo_pin_map_template

        def pin_position_fn(name, _x=x, _y=y, _pm=pin_map):
            if name == "EN":
                # XC6206 / AMS1117 lack EN -- raise like real symbols.
                raise PinNotFoundError(name, "mock_ldo", [])
            if name in _pm:
                return (_x + _pm[name][0], _y + _pm[name][1])
            raise PinNotFoundError(name, "mock_ldo", list(_pm.keys()))

        comp.pin_position.side_effect = pin_position_fn
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    sch.wire_decoupling_cap = Mock()
    return sch


class TestLDOBlockPinNets:
    """Tests for the new ``pin_nets`` kwarg on ``LDOBlock``."""

    def test_default_emits_no_pin_labels(self, mock_schematic):
        """Without ``pin_nets``, no pin-net labels are emitted."""
        LDOBlock(mock_schematic, x=100, y=100)
        assert mock_schematic.add_label.call_count == 0

    def test_pin_nets_left_edge_pin_stubs_left(self, mock_schematic):
        """Pins to the left of the symbol center stub leftward."""
        block = LDOBlock(
            mock_schematic, x=100, y=100,
            pin_nets={"VI": "+5V"},
        )

        STUB = 2.54
        # VI is at (90, 95) in our mock (x - 10, y - 5).
        labels = mock_schematic.add_label.call_args_list
        net_labels = [c for c in labels if c.args[0] == "+5V"]
        assert len(net_labels) == 1
        _, lx, ly = net_labels[0].args[0], net_labels[0].args[1], net_labels[0].args[2]
        assert lx == 90 - STUB
        assert ly == 95
        # Alias port also exposed.
        assert block.ports["+5V"] == (90, 95)

    def test_pin_nets_right_edge_pin_stubs_right(self, mock_schematic):
        """Pins to the right of the symbol center stub rightward."""
        LDOBlock(
            mock_schematic, x=100, y=100,
            pin_nets={"VO": "+3.3V"},
        )

        STUB = 2.54
        # VO is at (110, 95) -- right of center x=100.
        labels = mock_schematic.add_label.call_args_list
        net_labels = [c for c in labels if c.args[0] == "+3.3V"]
        assert len(net_labels) == 1
        _, lx, ly = net_labels[0].args[0], net_labels[0].args[1], net_labels[0].args[2]
        assert lx == 110 + STUB
        assert ly == 95

    def test_pin_nets_accepts_pin_numbers(self, mock_schematic):
        """``pin_nets`` keys may be pin numbers, not just pin names."""
        # Our mock advertises "2" as an alias for VO.
        LDOBlock(
            mock_schematic, x=100, y=100,
            pin_nets={"2": "+3.3V"},
        )
        labels = [c.args[0] for c in mock_schematic.add_label.call_args_list]
        assert "+3.3V" in labels

    def test_pin_nets_multiple_entries(self, mock_schematic):
        """Mapping with multiple entries emits one label per entry."""
        pin_nets = {
            "VI": "+5V",
            "VO": "+3.3V",
            "GND": "GND_NET",
        }
        LDOBlock(
            mock_schematic, x=100, y=100,
            pin_nets=pin_nets,
        )

        emitted = {c.args[0] for c in mock_schematic.add_label.call_args_list}
        for v in pin_nets.values():
            assert v in emitted

    def test_alias_ports_added_for_each_pin_net(self, mock_schematic):
        """Each ``pin_nets`` entry adds a port keyed by the net name."""
        block = LDOBlock(
            mock_schematic, x=100, y=100,
            pin_nets={"VI": "+5V", "VO": "+3.3V"},
        )
        assert "+5V" in block.ports
        assert "+3.3V" in block.ports

    def test_alias_port_does_not_clobber_existing(self, mock_schematic):
        """A net name colliding with an existing port (e.g. ``GND``) keeps the old port."""
        block_no_nets = LDOBlock(mock_schematic, x=100, y=100)
        original_gnd = block_no_nets.ports["GND"]

        block = LDOBlock(
            mock_schematic, x=100, y=100,
            pin_nets={"GND": "GND"},  # try to overwrite
        )
        # Original port survives.
        assert block.ports["GND"] == original_gnd

    def test_labels_anchored_to_wire_endpoints(self, mock_schematic):
        """Regression test for #2980/#2994.

        Every emitted pin-net label must sit on a wire endpoint, otherwise
        KiCad's label-only connectivity treats it as floating.
        """
        LDOBlock(
            mock_schematic, x=100, y=100,
            pin_nets={
                "VI": "+5V",
                "VO": "+3.3V",
                "GND": "GND_NET",
            },
        )

        wire_endpoints: set[tuple[float, float]] = set()
        for call in mock_schematic.add_wire.call_args_list:
            a, b = call.args[0], call.args[1]
            wire_endpoints.add((a[0], a[1]))
            wire_endpoints.add((b[0], b[1]))

        pin_net_values = {"+5V", "+3.3V", "GND_NET"}
        for call in mock_schematic.add_label.call_args_list:
            net_name, lx, ly = call.args[0], call.args[1], call.args[2]
            if net_name in pin_net_values:
                assert (lx, ly) in wire_endpoints, (
                    f"label {net_name!r} at ({lx}, {ly}) has no matching wire endpoint; "
                    f"KiCad will treat it as floating (regression of #2980/#2994)"
                )

    def test_external_ports_unchanged_without_pin_nets(self, mock_schematic):
        """Block ports unchanged when ``pin_nets`` is ``None`` (back-compat)."""
        b1 = LDOBlock(mock_schematic, x=100, y=100)
        b2 = LDOBlock(
            mock_schematic, x=100, y=100,
            pin_nets={"VI": "+5V"},
        )
        # Original placeholder ports survive in both blocks.
        for key in ("VIN", "VOUT", "GND"):
            assert b1.ports[key] == b2.ports[key]
