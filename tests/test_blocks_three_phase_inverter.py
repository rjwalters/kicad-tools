"""Tests for ``ThreePhaseInverter`` gate-net label/stub-wire emission.

These tests use a mocked ``Schematic`` to exercise the new optional
``gate_hs_nets`` / ``gate_ls_nets`` list kwargs without producing real
KiCad output.  Shape mirrors ``tests/test_blocks_gate_drive_resistor_array.py``
and ``tests/test_blocks_half_bridge.py``; the load-bearing assertion for
issue #2980 is ``test_labels_anchored_to_wire_endpoints``.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import ThreePhaseInverter


@pytest.fixture
def mock_schematic():
    """Mock Schematic returning mock symbols with deterministic pin positions.

    MOSFET pins follow ``Device:Q_NMOS`` convention.
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


class TestThreePhaseInverterGateNets:
    """Tests for the new ``gate_hs_nets`` / ``gate_ls_nets`` kwargs."""

    def test_default_emits_only_phase_labels(self, mock_schematic):
        """Without gate-net kwargs, only the three ``PHASE_*`` labels appear.

        Boards that don't opt in to the new feature must see unchanged
        behavior (AC4).
        """
        ThreePhaseInverter(mock_schematic, x=100, y=100)

        labels = sorted(call.args[0] for call in mock_schematic.add_label.call_args_list)
        # Three phase-output labels (PHASE_A/B/C); zero gate-net labels.
        assert labels == ["PHASE_A", "PHASE_B", "PHASE_C"]

    def test_gate_hs_nets_emits_three_hs_labels(self, mock_schematic):
        """``gate_hs_nets`` adds one label per phase at the HS gate stub."""
        ThreePhaseInverter(
            mock_schematic,
            x=100,
            y=100,
            gate_hs_nets=["GATE_AH", "GATE_BH", "GATE_CH"],
        )
        labels = sorted(call.args[0] for call in mock_schematic.add_label.call_args_list)
        assert "GATE_AH" in labels
        assert "GATE_BH" in labels
        assert "GATE_CH" in labels
        # PHASE_* still emitted (the new feature is additive).
        assert "PHASE_A" in labels
        assert "PHASE_B" in labels
        assert "PHASE_C" in labels

    def test_gate_ls_nets_emits_three_ls_labels(self, mock_schematic):
        """``gate_ls_nets`` adds one label per phase at the LS gate stub."""
        ThreePhaseInverter(
            mock_schematic,
            x=100,
            y=100,
            gate_ls_nets=["GATE_AL", "GATE_BL", "GATE_CL"],
        )
        labels = sorted(call.args[0] for call in mock_schematic.add_label.call_args_list)
        for ls in ("GATE_AL", "GATE_BL", "GATE_CL"):
            assert ls in labels

    def test_both_gate_lists_emit_six_gate_labels(self, mock_schematic):
        """All six gate labels emitted when both kwargs provided."""
        ThreePhaseInverter(
            mock_schematic,
            x=100,
            y=100,
            gate_hs_nets=["GATE_AH", "GATE_BH", "GATE_CH"],
            gate_ls_nets=["GATE_AL", "GATE_BL", "GATE_CL"],
        )
        labels = sorted(call.args[0] for call in mock_schematic.add_label.call_args_list)
        # 3 phase + 3 HS gate + 3 LS gate = 9 labels.
        assert len(labels) == 9
        for gate in ("GATE_AH", "GATE_BH", "GATE_CH", "GATE_AL", "GATE_BL", "GATE_CL"):
            assert gate in labels

    def test_length_mismatch_raises(self, mock_schematic):
        """Lists shorter/longer than ``phase_labels`` raise ValueError."""
        with pytest.raises(ValueError, match="gate_hs_nets length"):
            ThreePhaseInverter(
                mock_schematic,
                x=100,
                y=100,
                gate_hs_nets=["GATE_AH", "GATE_BH"],  # 2 != 3
            )
        with pytest.raises(ValueError, match="gate_ls_nets length"):
            ThreePhaseInverter(
                mock_schematic,
                x=100,
                y=100,
                gate_ls_nets=["A", "B", "C", "D"],  # 4 != 3
            )

    def test_custom_phase_labels_with_gate_nets(self, mock_schematic):
        """Custom phase labels (e.g. U/V/W) compose with gate-net lists."""
        ThreePhaseInverter(
            mock_schematic,
            x=100,
            y=100,
            phase_labels=["U", "V", "W"],
            gate_hs_nets=["GATE_UH", "GATE_VH", "GATE_WH"],
        )
        labels = sorted(call.args[0] for call in mock_schematic.add_label.call_args_list)
        for tag in ("PHASE_U", "PHASE_V", "PHASE_W", "GATE_UH", "GATE_VH", "GATE_WH"):
            assert tag in labels

    def test_labels_anchored_to_wire_endpoints(self, mock_schematic):
        """Regression test for #2980.

        Every emitted gate-net label must sit on a wire endpoint, otherwise
        KiCad's label-only connectivity treats it as floating.
        """
        ThreePhaseInverter(
            mock_schematic,
            x=100,
            y=100,
            gate_hs_nets=["GATE_AH", "GATE_BH", "GATE_CH"],
            gate_ls_nets=["GATE_AL", "GATE_BL", "GATE_CL"],
        )

        # Collect every wire endpoint.
        wire_endpoints: set[tuple[float, float]] = set()
        for call in mock_schematic.add_wire.call_args_list:
            a, b = call.args[0], call.args[1]
            wire_endpoints.add((a[0], a[1]))
            wire_endpoints.add((b[0], b[1]))

        # Every gate label must coincide with a wire endpoint.  PHASE_*
        # labels were already wired prior to #2980 and aren't load-bearing
        # here, but we still check them for symmetry.
        gate_nets = {
            "GATE_AH",
            "GATE_BH",
            "GATE_CH",
            "GATE_AL",
            "GATE_BL",
            "GATE_CL",
        }
        for call in mock_schematic.add_label.call_args_list:
            net_name, lx, ly = call.args[0], call.args[1], call.args[2]
            if net_name in gate_nets:
                assert (lx, ly) in wire_endpoints, (
                    f"gate label {net_name!r} at ({lx}, {ly}) has no matching "
                    f"wire endpoint; KiCad will treat it as floating "
                    f"(regression of #2980)"
                )
