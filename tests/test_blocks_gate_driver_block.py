"""Tests for ``GateDriverBlock`` pin-net label/stub-wire emission.

These tests use a mocked ``Schematic`` to exercise the new optional
``pin_nets`` dict kwarg without producing real KiCad output.  Shape
mirrors ``tests/test_blocks_gate_drive_resistor_array.py``; the
load-bearing assertion for issue #2980 is
``test_labels_anchored_to_wire_endpoints``.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import GateDriverBlock


@pytest.fixture
def mock_schematic():
    """Mock Schematic returning mock symbols with deterministic pin positions.

    The driver mock advertises a small subset of DRV8308-shaped pins:
    HSG / LSG outputs (left of symbol body) and PWM inputs (right of body).
    The exact positions don't matter for these tests as long as some pins
    are left-of-center (stub extends left) and others are right-of-center
    (stub extends right).
    """
    sch = Mock()

    # Driver pins are arranged so we can test both stub directions:
    #   - Left-edge pins (x - 10): UHSG, ULSG, VHSG, VLSG, WHSG, WLSG
    #   - Right-edge pins (x + 10): INHA, INLA, INHB, INLB, INHC, INLC
    #   - Center / power: VCC, GND
    driver_pin_map_template = {
        "UHSG": (-10, -6),
        "ULSG": (-10, -3),
        "VHSG": (-10, 0),
        "VLSG": (-10, 3),
        "WHSG": (-10, 6),
        "WLSG": (-10, 9),
        "INHA": (10, -6),
        "INLA": (10, -3),
        "INHB": (10, 0),
        "INLB": (10, 3),
        "INHC": (10, 6),
        "INLC": (10, 9),
        "VCC": (0, -10),
        "GND": (0, 10),
        # Numeric pin alias for one entry to verify pin_position accepts numbers
        "32": (-10, -6),  # same coord as UHSG
        # Capacitor pins
        "1": (-2, 0),
        "2": (2, 0),
    }

    def create_mock_component(symbol, x, y, ref, *args, **kwargs):
        comp = Mock()
        comp.reference = ref
        comp.symbol = symbol
        comp.pin_position.side_effect = lambda name, _x=x, _y=y: (
            (_x + driver_pin_map_template[name][0], _y + driver_pin_map_template[name][1])
            if name in driver_pin_map_template
            else (_x, _y)
        )
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    sch.wire_decoupling_cap = Mock()
    return sch


class TestGateDriverBlockPinNets:
    """Tests for the new ``pin_nets`` kwarg on ``GateDriverBlock``."""

    def test_default_emits_no_pin_labels(self, mock_schematic):
        """Without ``pin_nets``, no pin-net labels are emitted (AC4)."""
        GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            bootstrap_caps=None,  # disable bootstrap to keep label count low
        )
        assert mock_schematic.add_label.call_count == 0

    def test_pin_nets_left_edge_pin_stubs_left(self, mock_schematic):
        """Pins to the left of the symbol center stub leftward."""
        block = GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            bootstrap_caps=None,
            pin_nets={"UHSG": "GATE_DRV_AH"},
        )

        STUB = 2.54
        # Pin is at (90, 94) in our mock (x - 10, y - 6).
        # Stub should extend left to (90 - STUB, 94).
        labels = mock_schematic.add_label.call_args_list
        assert len(labels) == 1
        net_name, lx, ly = labels[0].args[0], labels[0].args[1], labels[0].args[2]
        assert net_name == "GATE_DRV_AH"
        assert lx == 90 - STUB
        assert ly == 94
        # Alias port also exposed.
        assert block.ports["GATE_DRV_AH"] == (90, 94)

    def test_pin_nets_right_edge_pin_stubs_right(self, mock_schematic):
        """Pins to the right of the symbol center stub rightward."""
        GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            bootstrap_caps=None,
            pin_nets={"INHA": "PWM_AH"},
        )

        STUB = 2.54
        # Pin is at (110, 94) — to the right of center x=100.
        labels = mock_schematic.add_label.call_args_list
        assert len(labels) == 1
        net_name, lx, ly = labels[0].args[0], labels[0].args[1], labels[0].args[2]
        assert net_name == "PWM_AH"
        assert lx == 110 + STUB
        assert ly == 94

    def test_pin_nets_accepts_pin_numbers(self, mock_schematic):
        """``pin_nets`` keys may be pin numbers, not just pin names."""
        # Our mock advertises "32" as an alias for UHSG.
        GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            bootstrap_caps=None,
            pin_nets={"32": "GATE_DRV_AH"},
        )
        labels = [call.args[0] for call in mock_schematic.add_label.call_args_list]
        assert labels == ["GATE_DRV_AH"]

    def test_pin_nets_multiple_entries(self, mock_schematic):
        """Mapping with multiple entries emits one label per entry."""
        pin_nets = {
            "UHSG": "GATE_DRV_AH",
            "VHSG": "GATE_DRV_BH",
            "WHSG": "GATE_DRV_CH",
            "ULSG": "GATE_AL",
            "VLSG": "GATE_BL",
            "WLSG": "GATE_CL",
            "INHA": "PWM_AH",
            "INLA": "PWM_AL",
            "INHB": "PWM_BH",
            "INLB": "PWM_BL",
            "INHC": "PWM_CH",
            "INLC": "PWM_CL",
        }
        GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            bootstrap_caps=None,
            pin_nets=pin_nets,
        )

        labels = sorted(call.args[0] for call in mock_schematic.add_label.call_args_list)
        # 12 labels total, exactly the values from pin_nets.
        assert sorted(pin_nets.values()) == labels

    def test_alias_ports_added_for_each_pin_net(self, mock_schematic):
        """Each ``pin_nets`` entry adds a port keyed by the net name."""
        block = GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            bootstrap_caps=None,
            pin_nets={"UHSG": "GATE_DRV_AH", "INHA": "PWM_AH"},
        )
        assert "GATE_DRV_AH" in block.ports
        assert "PWM_AH" in block.ports

    def test_alias_port_does_not_clobber_existing(self, mock_schematic):
        """A net name colliding with an existing port (e.g. ``GND``) keeps the old port."""
        # The block already has a "GND" placeholder port at (x, y + 20).
        original_gnd = (100, 120)
        block = GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            bootstrap_caps=None,
            pin_nets={"GND": "GND"},  # try to overwrite
        )
        # Original placeholder still wins.
        assert block.ports["GND"] == original_gnd

    def test_labels_anchored_to_wire_endpoints(self, mock_schematic):
        """Regression test for #2980.

        Every emitted pin-net label must sit on a wire endpoint, otherwise
        KiCad's label-only connectivity treats it as floating.  Mirror of
        ``test_labels_anchored_to_wire_endpoints`` from
        ``test_blocks_gate_drive_resistor_array.py``.
        """
        GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            bootstrap_caps=None,
            pin_nets={
                "UHSG": "GATE_DRV_AH",
                "VHSG": "GATE_DRV_BH",
                "WHSG": "GATE_DRV_CH",
                "ULSG": "GATE_AL",
                "VLSG": "GATE_BL",
                "WLSG": "GATE_CL",
                "INHA": "PWM_AH",
                "INLA": "PWM_AL",
                "INHB": "PWM_BH",
                "INLB": "PWM_BL",
                "INHC": "PWM_CH",
                "INLC": "PWM_CL",
            },
        )

        wire_endpoints: set[tuple[float, float]] = set()
        for call in mock_schematic.add_wire.call_args_list:
            a, b = call.args[0], call.args[1]
            wire_endpoints.add((a[0], a[1]))
            wire_endpoints.add((b[0], b[1]))

        label_calls = mock_schematic.add_label.call_args_list
        assert len(label_calls) == 12, "expected one label per pin_nets entry"
        for call in label_calls:
            net_name, lx, ly = call.args[0], call.args[1], call.args[2]
            assert (lx, ly) in wire_endpoints, (
                f"label {net_name!r} at ({lx}, {ly}) has no matching wire endpoint; "
                f"KiCad will treat it as floating (regression of #2980)"
            )

    def test_external_ports_unchanged_without_pin_nets(self, mock_schematic):
        """Block ports unchanged when ``pin_nets`` is ``None`` (back-compat)."""
        b1 = GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            bootstrap_caps=None,
        )
        b2 = GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            bootstrap_caps=None,
            pin_nets={"UHSG": "GATE_DRV_AH"},
        )
        # Original placeholder ports survive in both blocks.
        for key in ("VCC", "GND", "BOOT_A", "GATE_HS_A", "GATE_LS_A", "PWM_H_A"):
            assert b1.ports[key] == b2.ports[key]
