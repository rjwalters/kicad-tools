"""Tests for ``BuckConverter`` pin-net label/stub-wire emission.

These tests use a mocked ``Schematic`` to exercise the new optional
``pin_nets`` dict kwarg without producing real KiCad output.  Shape
mirrors ``tests/test_blocks_gate_driver_block.py``; the load-bearing
assertion for issue #2994 is ``test_labels_anchored_to_wire_endpoints``.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import BuckConverter


@pytest.fixture
def mock_schematic():
    """Mock Schematic returning mock symbols with deterministic pin positions.

    The buck-regulator mock advertises a small subset of LM2596-shaped
    pins: VIN / OUT (switch) / GND / FB / ~{ON}/OFF.  Pin positions are
    chosen so we can exercise both stub directions:

      - Left-edge pins (x - 10): VIN, ~{ON}/OFF
      - Right-edge pins (x + 10): OUT, FB
      - Center / bottom: GND
    """
    sch = Mock()

    buck_pin_map_template = {
        "VIN": (-10, -5),
        "~{ON}/OFF": (-10, 0),
        "OUT": (10, -5),
        "SW": (10, -5),
        "FB": (10, 5),
        "GND": (0, 10),
        "1": (-10, -5),  # alias for VIN by number
        "3": (0, 10),  # alias for GND by number
        "4": (10, 5),  # alias for FB by number
    }

    diode_pin_map = {
        "K": (-2, 0),  # cathode
        "A": (2, 0),  # anode
    }

    inductor_pin_map = {
        "1": (-2, 0),
        "2": (2, 0),
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

        if "Schottky" in symbol:
            pin_map = diode_pin_map
        elif "Device:L" in symbol:
            pin_map = inductor_pin_map
        elif "Device:C" in symbol or "Device:R" in symbol:
            pin_map = cap_pin_map
        else:
            pin_map = buck_pin_map_template

        comp.pin_position.side_effect = lambda name, _x=x, _y=y, _pm=pin_map: (
            (_x + _pm[name][0], _y + _pm[name][1]) if name in _pm else (_x, _y)
        )
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    sch.wire_decoupling_cap = Mock()
    return sch


class TestBuckConverterPinNets:
    """Tests for the new ``pin_nets`` kwarg on ``BuckConverter``."""

    def test_default_emits_no_pin_labels(self, mock_schematic):
        """Without ``pin_nets``, no pin-net labels are emitted."""
        BuckConverter(mock_schematic, x=100, y=100)
        assert mock_schematic.add_label.call_count == 0

    def test_pin_nets_left_edge_pin_stubs_left(self, mock_schematic):
        """Pins to the left of the symbol center stub leftward."""
        block = BuckConverter(
            mock_schematic,
            x=100,
            y=100,
            pin_nets={"VIN": "VMOTOR"},
        )

        STUB = 2.54
        # VIN is at (90, 95) in our mock (x - 10, y - 5).
        # Stub should extend left to (90 - STUB, 95).
        labels = mock_schematic.add_label.call_args_list
        # Exactly one pin_nets label
        net_labels = [c for c in labels if c.args[0] == "VMOTOR"]
        assert len(net_labels) == 1
        _, lx, ly = net_labels[0].args[0], net_labels[0].args[1], net_labels[0].args[2]
        assert lx == 90 - STUB
        assert ly == 95
        # Alias port also exposed.
        assert block.ports["VMOTOR"] == (90, 95)

    def test_pin_nets_right_edge_pin_stubs_right(self, mock_schematic):
        """Pins to the right of the symbol center stub rightward."""
        BuckConverter(
            mock_schematic,
            x=100,
            y=100,
            pin_nets={"FB": "+5V"},
        )

        STUB = 2.54
        # FB is at (110, 105) -- right of center x=100.
        labels = mock_schematic.add_label.call_args_list
        net_labels = [c for c in labels if c.args[0] == "+5V"]
        assert len(net_labels) == 1
        _, lx, ly = net_labels[0].args[0], net_labels[0].args[1], net_labels[0].args[2]
        assert lx == 110 + STUB
        assert ly == 105

    def test_pin_nets_accepts_pin_numbers(self, mock_schematic):
        """``pin_nets`` keys may be pin numbers, not just pin names."""
        # Our mock advertises "1" as an alias for VIN.
        BuckConverter(
            mock_schematic,
            x=100,
            y=100,
            pin_nets={"1": "VMOTOR"},
        )
        labels = [c.args[0] for c in mock_schematic.add_label.call_args_list]
        assert "VMOTOR" in labels

    def test_pin_nets_multiple_entries(self, mock_schematic):
        """Mapping with multiple entries emits one label per entry."""
        pin_nets = {
            "VIN": "VMOTOR",
            "GND": "GND",
            "FB": "+5V",
        }
        BuckConverter(
            mock_schematic,
            x=100,
            y=100,
            pin_nets=pin_nets,
        )

        emitted = {c.args[0] for c in mock_schematic.add_label.call_args_list}
        # Each value should be present in the emitted labels.
        for v in pin_nets.values():
            assert v in emitted

    def test_alias_ports_added_for_each_pin_net(self, mock_schematic):
        """Each ``pin_nets`` entry adds a port keyed by the net name."""
        block = BuckConverter(
            mock_schematic,
            x=100,
            y=100,
            pin_nets={"VIN": "VMOTOR", "FB": "+5V"},
        )
        assert "VMOTOR" in block.ports
        assert "+5V" in block.ports

    def test_alias_port_does_not_clobber_existing(self, mock_schematic):
        """A net name colliding with an existing port (e.g. ``GND``) keeps the old port."""
        # The block already has a "GND" port at the GND pin position.
        block_no_nets = BuckConverter(mock_schematic, x=100, y=100)
        original_gnd = block_no_nets.ports["GND"]

        block = BuckConverter(
            mock_schematic,
            x=100,
            y=100,
            pin_nets={"GND": "GND"},  # try to overwrite
        )
        # Original port survives.
        assert block.ports["GND"] == original_gnd

    def test_labels_anchored_to_wire_endpoints(self, mock_schematic):
        """Regression test for #2980/#2994.

        Every emitted pin-net label must sit on a wire endpoint, otherwise
        KiCad's label-only connectivity treats it as floating.
        """
        BuckConverter(
            mock_schematic,
            x=100,
            y=100,
            pin_nets={
                "VIN": "VMOTOR",
                "GND": "GND_NET",
                "FB": "+5V",
            },
        )

        wire_endpoints: set[tuple[float, float]] = set()
        for call in mock_schematic.add_wire.call_args_list:
            a, b = call.args[0], call.args[1]
            wire_endpoints.add((a[0], a[1]))
            wire_endpoints.add((b[0], b[1]))

        # Filter labels to only the pin_nets ones (named by the values).
        pin_net_values = {"VMOTOR", "GND_NET", "+5V"}
        for call in mock_schematic.add_label.call_args_list:
            net_name, lx, ly = call.args[0], call.args[1], call.args[2]
            if net_name in pin_net_values:
                assert (lx, ly) in wire_endpoints, (
                    f"label {net_name!r} at ({lx}, {ly}) has no matching wire endpoint; "
                    f"KiCad will treat it as floating (regression of #2980/#2994)"
                )

    def test_external_ports_unchanged_without_pin_nets(self, mock_schematic):
        """Block ports unchanged when ``pin_nets`` is ``None`` (back-compat)."""
        b1 = BuckConverter(mock_schematic, x=100, y=100)
        b2 = BuckConverter(
            mock_schematic,
            x=100,
            y=100,
            pin_nets={"FB": "+5V"},
        )
        # Original placeholder ports survive in both blocks.
        for key in ("VIN", "VOUT", "GND", "SW"):
            assert b1.ports[key] == b2.ports[key]
