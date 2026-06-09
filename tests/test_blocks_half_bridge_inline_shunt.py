"""Tests for ``HalfBridge.connect_to_rails`` in-line shunt topology.

Issue #3383: ``HalfBridge.connect_to_rails`` historically always emitted
an LS-source-to-GND wire.  When a board wires a current-sense shunt in
series between the LS source and GND (the canonical low-side-sense
topology used on board 05's BLDC stage), that wire short-circuits the
shunt -- the LS source ends up tied directly to GND and the shunt's IN+
side is bridged to its IN- side via the schematic.  After ``sync-netlist``
this manifests as R10/R11/R12.1 (ISENSE_X+) appearing on the GND net.

The fix is the new optional ``inline_shunt`` parameter (``HalfBridge``)
and the corresponding ``inline_shunts`` list parameter (``ThreePhaseInverter``).
When provided, the LS-source-to-GND wire (and its rail-side junction) is
suppressed -- the caller is responsible for wiring LS source to the
shunt's IN+ side and routing the shunt to GND.

The tests below use a mocked schematic so they exercise wire emission
without producing real KiCad output (shape mirrors
``tests/test_blocks_half_bridge.py``).
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import (
    CurrentSenseShunt,
    HalfBridge,
    ThreePhaseInverter,
)


@pytest.fixture
def mock_schematic():
    """Mock Schematic returning mock symbols with deterministic pin positions.

    MOSFET pins follow ``Device:Q_NMOS`` convention; shunt resistor pins
    follow ``Device:R`` convention (pin 1 above, pin 2 below).
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
            "IN+": (_x - 5, _y - 5),
            "IN-": (_x - 5, _y + 5),
            "OUT": (_x + 5, _y),
            "VS": (_x, _y - 10),
            "GND": (_x, _y + 10),
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


class TestHalfBridgeConnectToRailsDefault:
    """Baseline: default behavior emits LS-source-to-GND wire.

    AC4 (no regression for callers that don't opt in to the new
    parameter).
    """

    def test_default_emits_ls_source_to_gnd_wire(self, mock_schematic):
        """The default code path must still emit a LS-source-to-GND wire.

        This is the historical behavior every existing consumer relies on.
        """
        hb = HalfBridge(mock_schematic, x=100, y=100)
        ls_source = hb.ports["GND"]

        mock_schematic.add_wire.reset_mock()
        mock_schematic.add_junction.reset_mock()

        hb.connect_to_rails(vin_rail_y=30, gnd_rail_y=200)

        # The LS-source-to-GND wire must appear among emitted wires.
        wire_calls = [call.args for call in mock_schematic.add_wire.call_args_list]
        expected_gnd_wire = (ls_source, (ls_source[0], 200))
        assert expected_gnd_wire in wire_calls, (
            f"expected LS-source-to-GND wire {expected_gnd_wire} not found among "
            f"emitted wires {wire_calls}"
        )

        # And the GND-side junction must be added.
        junction_calls = [call.args for call in mock_schematic.add_junction.call_args_list]
        assert (ls_source[0], 200) in junction_calls

    def test_default_emits_vin_wire_and_junction(self, mock_schematic):
        """VIN-side wire + junction are unchanged by the new parameter."""
        hb = HalfBridge(mock_schematic, x=100, y=100)
        hs_drain = hb.ports["VIN"]

        mock_schematic.add_wire.reset_mock()
        mock_schematic.add_junction.reset_mock()

        hb.connect_to_rails(vin_rail_y=30, gnd_rail_y=200)

        wire_calls = [call.args for call in mock_schematic.add_wire.call_args_list]
        assert (hs_drain, (hs_drain[0], 30)) in wire_calls

        junction_calls = [call.args for call in mock_schematic.add_junction.call_args_list]
        assert (hs_drain[0], 30) in junction_calls


class TestHalfBridgeConnectToRailsInlineShunt:
    """In-line shunt topology: LS-source-to-GND wire is suppressed."""

    def test_inline_shunt_suppresses_ls_gnd_wire(self, mock_schematic):
        """When ``inline_shunt`` is provided, the LS-source-to-GND wire is omitted.

        This is the core AC for #3383: the wire that bridges the shunt
        must not be emitted.
        """
        hb = HalfBridge(mock_schematic, x=100, y=100)
        ls_source = hb.ports["GND"]

        shunt = CurrentSenseShunt(
            mock_schematic, x=120, y=180, shunt_value="5mR", ref_start=10
        )

        mock_schematic.add_wire.reset_mock()
        mock_schematic.add_junction.reset_mock()

        hb.connect_to_rails(vin_rail_y=30, gnd_rail_y=200, inline_shunt=shunt)

        # The LS-source-to-GND wire must NOT appear among emitted wires.
        wire_calls = [call.args for call in mock_schematic.add_wire.call_args_list]
        forbidden_gnd_wire = (ls_source, (ls_source[0], 200))
        assert forbidden_gnd_wire not in wire_calls, (
            f"LS-source-to-GND wire {forbidden_gnd_wire} was emitted despite "
            f"inline_shunt being provided (regression of #3383); emitted wires: "
            f"{wire_calls}"
        )

        # And the GND-side junction must also be suppressed (otherwise a
        # bare junction sits on the GND rail with nothing connected to it
        # from the half-bridge side).
        junction_calls = [call.args for call in mock_schematic.add_junction.call_args_list]
        assert (ls_source[0], 200) not in junction_calls

    def test_inline_shunt_still_emits_vin_wire(self, mock_schematic):
        """The VIN-side wire is independent of the LS-side suppression."""
        hb = HalfBridge(mock_schematic, x=100, y=100)
        hs_drain = hb.ports["VIN"]

        shunt = CurrentSenseShunt(
            mock_schematic, x=120, y=180, shunt_value="5mR", ref_start=10
        )

        mock_schematic.add_wire.reset_mock()
        mock_schematic.add_junction.reset_mock()

        hb.connect_to_rails(vin_rail_y=30, gnd_rail_y=200, inline_shunt=shunt)

        wire_calls = [call.args for call in mock_schematic.add_wire.call_args_list]
        assert (hs_drain, (hs_drain[0], 30)) in wire_calls

        junction_calls = [call.args for call in mock_schematic.add_junction.call_args_list]
        assert (hs_drain[0], 30) in junction_calls

    def test_inline_shunt_none_is_default_behavior(self, mock_schematic):
        """Explicitly passing ``inline_shunt=None`` matches the default behavior."""
        hb = HalfBridge(mock_schematic, x=100, y=100)
        ls_source = hb.ports["GND"]

        mock_schematic.add_wire.reset_mock()
        mock_schematic.add_junction.reset_mock()

        hb.connect_to_rails(vin_rail_y=30, gnd_rail_y=200, inline_shunt=None)

        # LS-source-to-GND wire still present.
        wire_calls = [call.args for call in mock_schematic.add_wire.call_args_list]
        assert (ls_source, (ls_source[0], 200)) in wire_calls


class TestThreePhaseInverterConnectToRailsInlineShunts:
    """Pass-through tests for ``ThreePhaseInverter.connect_to_rails``."""

    def test_default_emits_three_ls_gnd_wires(self, mock_schematic):
        """Without ``inline_shunts``, each phase emits its LS-source-to-GND wire."""
        inv = ThreePhaseInverter(mock_schematic, x=100, y=100)
        ls_positions = [hb.ports["GND"] for hb in inv.half_bridges]

        mock_schematic.add_wire.reset_mock()
        inv.connect_to_rails(vin_rail_y=30, gnd_rail_y=200)

        wire_calls = [call.args for call in mock_schematic.add_wire.call_args_list]
        for ls in ls_positions:
            assert (ls, (ls[0], 200)) in wire_calls

    def test_inline_shunts_suppresses_all_three_ls_gnd_wires(self, mock_schematic):
        """When every phase has an in-line shunt, no LS-source-to-GND wires are emitted."""
        inv = ThreePhaseInverter(mock_schematic, x=100, y=100)
        ls_positions = [hb.ports["GND"] for hb in inv.half_bridges]

        # Build one shunt per phase.
        shunts = [
            CurrentSenseShunt(
                mock_schematic,
                x=100 + i * 75,
                y=180,
                shunt_value="5mR",
                ref_start=10 + i,
            )
            for i in range(3)
        ]

        mock_schematic.add_wire.reset_mock()
        inv.connect_to_rails(vin_rail_y=30, gnd_rail_y=200, inline_shunts=shunts)

        wire_calls = [call.args for call in mock_schematic.add_wire.call_args_list]
        for ls in ls_positions:
            assert (ls, (ls[0], 200)) not in wire_calls, (
                f"LS-source-to-GND wire at {ls} was emitted despite an in-line "
                f"shunt being provided for that phase"
            )

    def test_inline_shunts_mixed_some_phases_inline_some_not(self, mock_schematic):
        """Per-phase opt-in: only phases with non-None shunts skip the GND wire."""
        inv = ThreePhaseInverter(mock_schematic, x=100, y=100)
        ls_positions = [hb.ports["GND"] for hb in inv.half_bridges]

        # Phase A and C get in-line shunts; phase B does not.
        shunt_a = CurrentSenseShunt(
            mock_schematic, x=100, y=180, shunt_value="5mR", ref_start=10
        )
        shunt_c = CurrentSenseShunt(
            mock_schematic, x=250, y=180, shunt_value="5mR", ref_start=12
        )

        mock_schematic.add_wire.reset_mock()
        inv.connect_to_rails(
            vin_rail_y=30,
            gnd_rail_y=200,
            inline_shunts=[shunt_a, None, shunt_c],
        )

        wire_calls = [call.args for call in mock_schematic.add_wire.call_args_list]
        # A and C: no GND wire.
        assert (ls_positions[0], (ls_positions[0][0], 200)) not in wire_calls
        assert (ls_positions[2], (ls_positions[2][0], 200)) not in wire_calls
        # B: GND wire still emitted.
        assert (ls_positions[1], (ls_positions[1][0], 200)) in wire_calls

    def test_inline_shunts_wrong_length_raises(self, mock_schematic):
        """Length mismatch with phase count must raise ``ValueError``."""
        inv = ThreePhaseInverter(mock_schematic, x=100, y=100)
        shunt = CurrentSenseShunt(
            mock_schematic, x=100, y=180, shunt_value="5mR", ref_start=10
        )

        with pytest.raises(ValueError, match="inline_shunts length"):
            inv.connect_to_rails(
                vin_rail_y=30,
                gnd_rail_y=200,
                inline_shunts=[shunt, shunt],  # 2 entries vs 3 phases
            )
