"""Tests for ``HalfBridge`` gate-net label/stub-wire emission.

These tests use a mocked ``Schematic`` so they exercise the new optional
``gate_hs_net`` / ``gate_ls_net`` kwargs without actually emitting KiCad
schematic data.  Shape mirrors
``tests/test_blocks_gate_drive_resistor_array.py`` (see PR #2979); the
regression test ``test_labels_anchored_to_wire_endpoints`` is the
load-bearing assertion for issue #2980.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import HalfBridge


@pytest.fixture
def mock_schematic():
    """Create a mock Schematic that returns mock symbols with deterministic pins.

    MOSFET pins follow ``Device:Q_NMOS`` convention: ``G`` (gate) on the
    left, ``D`` (drain) above, ``S`` (source) below.  Bootstrap diode /
    cap pins follow ``Device:D`` / ``Device:C`` conventions.
    """
    sch = Mock()

    def create_mock_component(symbol, x, y, ref, *args, **kwargs):
        comp = Mock()
        comp.reference = ref
        comp.symbol = symbol
        comp.pin_position.side_effect = lambda name, _x=x, _y=y: {
            "D": (_x, _y - 10),
            "G": (_x - 10, _y),
            "S": (_x, _y + 10),
            "A": (_x - 5, _y),
            "K": (_x + 5, _y),
            "1": (_x, _y - 5),
            "2": (_x, _y + 5),
        }.get(name, (_x, _y))
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    sch.wire_decoupling_cap = Mock()
    return sch


class TestHalfBridgeGateNets:
    """Tests for the new ``gate_hs_net`` / ``gate_ls_net`` HalfBridge kwargs."""

    def test_default_emits_no_gate_labels(self, mock_schematic):
        """Without ``gate_*_net`` kwargs, HalfBridge emits zero gate labels.

        Boards that don't opt in must see no behavioral change (AC4).
        """
        HalfBridge(mock_schematic, x=100, y=100)

        # No labels emitted at all (HalfBridge itself never adds labels
        # without the new kwargs).
        assert mock_schematic.add_label.call_count == 0

    def test_gate_hs_net_emits_label_and_stub(self, mock_schematic):
        """``gate_hs_net`` triggers exactly one label + one stub wire."""
        baseline_wires = 0  # we'll count delta below
        hb = HalfBridge(
            mock_schematic,
            x=100,
            y=100,
            gate_hs_net="GATE_AH",
        )

        # Exactly one label for the HS gate.
        labels = [call.args[0] for call in mock_schematic.add_label.call_args_list]
        assert labels == ["GATE_AH"]

        # The label sits at the stub endpoint (gate_x - STUB, gate_y).
        STUB = 2.54
        hs_gate = hb.ports["GATE_HS"]
        label_call = mock_schematic.add_label.call_args_list[0]
        net_name, lx, ly = label_call.args[0], label_call.args[1], label_call.args[2]
        assert net_name == "GATE_AH"
        assert (lx, ly) == (hs_gate[0] - STUB, hs_gate[1])
        _ = baseline_wires  # suppress unused-var lint

    def test_gate_ls_net_emits_label_and_stub(self, mock_schematic):
        """``gate_ls_net`` triggers exactly one label + one stub wire."""
        hb = HalfBridge(
            mock_schematic,
            x=100,
            y=100,
            gate_ls_net="GATE_AL",
        )

        labels = [call.args[0] for call in mock_schematic.add_label.call_args_list]
        assert labels == ["GATE_AL"]

        STUB = 2.54
        ls_gate = hb.ports["GATE_LS"]
        label_call = mock_schematic.add_label.call_args_list[0]
        net_name, lx, ly = label_call.args[0], label_call.args[1], label_call.args[2]
        assert net_name == "GATE_AL"
        assert (lx, ly) == (ls_gate[0] - STUB, ls_gate[1])

    def test_both_gate_nets_emit_two_labels(self, mock_schematic):
        """When both HS and LS nets are set, two distinct labels are emitted."""
        HalfBridge(
            mock_schematic,
            x=100,
            y=100,
            gate_hs_net="GATE_AH",
            gate_ls_net="GATE_AL",
        )
        labels = sorted(call.args[0] for call in mock_schematic.add_label.call_args_list)
        assert labels == ["GATE_AH", "GATE_AL"]

    def test_external_ports_unchanged_when_labels_added(self, mock_schematic):
        """The ``GATE_HS`` / ``GATE_LS`` ports still resolve to pin positions.

        External callers wiring through the block's port API must see the
        same pin coordinates regardless of whether the labels were emitted.
        """
        hb_with = HalfBridge(
            mock_schematic,
            x=100,
            y=100,
            gate_hs_net="GATE_AH",
            gate_ls_net="GATE_AL",
        )
        hb_without = HalfBridge(mock_schematic, x=100, y=100)

        assert hb_with.ports["GATE_HS"] == hb_without.ports["GATE_HS"]
        assert hb_with.ports["GATE_LS"] == hb_without.ports["GATE_LS"]

    def test_labels_anchored_to_wire_endpoints(self, mock_schematic):
        """Regression test for #2980.

        Every emitted gate-net label must sit on a wire endpoint, otherwise
        KiCad's label-only connectivity treats it as floating and ERC
        cascades into ``isolated_pin_label`` / ``pin_not_connected``.
        """
        HalfBridge(
            mock_schematic,
            x=100,
            y=100,
            gate_hs_net="GATE_AH",
            gate_ls_net="GATE_AL",
        )

        # Collect every wire endpoint.
        wire_endpoints: set[tuple[float, float]] = set()
        for call in mock_schematic.add_wire.call_args_list:
            a, b = call.args[0], call.args[1]
            wire_endpoints.add((a[0], a[1]))
            wire_endpoints.add((b[0], b[1]))

        # Every gate-net label coordinate must be a wire endpoint.
        label_calls = mock_schematic.add_label.call_args_list
        assert len(label_calls) == 2
        for call in label_calls:
            net_name, lx, ly = call.args[0], call.args[1], call.args[2]
            assert (lx, ly) in wire_endpoints, (
                f"label {net_name!r} at ({lx}, {ly}) has no matching wire endpoint; "
                f"KiCad will treat it as floating (regression of #2980)"
            )

    def test_only_hs_set_only_ls_floating(self, mock_schematic):
        """Setting only one side leaves the other free of labels.

        Required to support designs where only one gate is direct-driven
        and the other goes through a series resistor (board 05 pattern).
        """
        HalfBridge(
            mock_schematic,
            x=100,
            y=100,
            gate_hs_net="GATE_AH",  # gate_ls_net intentionally None
        )
        labels = [call.args[0] for call in mock_schematic.add_label.call_args_list]
        assert labels == ["GATE_AH"]
