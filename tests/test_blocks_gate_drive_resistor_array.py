"""Tests for the ``create_gate_drive_resistor_array`` factory.

These tests use a mocked ``Schematic`` so they exercise the factory's
control flow without actually emitting KiCad schematic data. The shape
mirrors ``TestCurrentSenseShuntMocked`` and ``TestGateDriverBlockMocked``
in ``tests/test_schematic_blocks.py``.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import (
    CircuitBlock,
    GateDriveResistorArray,
    create_gate_drive_resistor_array,
)


@pytest.fixture
def mock_schematic():
    """Create a mock Schematic that returns mock symbols with deterministic pins.

    Pin "1" is the input (driver) side; Pin "2" is the output (MOSFET-gate)
    side. Pin coordinates are derived from the symbol's (x, y) so the
    factory can emit add_label / add_wire calls at predictable positions.
    """
    sch = Mock()

    def create_mock_component(symbol, x, y, ref, value, *args, **kwargs):
        comp = Mock()
        comp.reference = ref
        comp.value = value
        comp.symbol = symbol
        comp._properties = kwargs.get("properties", {})

        comp.pin_position.side_effect = lambda name, _x=x, _y=y: {
            "1": (_x - 5, _y),
            "2": (_x + 5, _y),
        }.get(name, (_x, _y))
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    return sch


class TestGateDriveResistorArray:
    """Tests for ``create_gate_drive_resistor_array`` factory."""

    def test_default_3_channels(self, mock_schematic):
        """Default invocation creates exactly 3 resistors with default value."""
        block = create_gate_drive_resistor_array(mock_schematic, x=100, y=100)

        assert len(block.resistors) == 3
        # Three add_symbol calls.
        assert mock_schematic.add_symbol.call_count == 3
        # Default refs R1..R3.
        for i, comp in enumerate(block.resistors):
            assert comp.reference == f"R{i + 1}"
            assert comp.value == "10"

    @pytest.mark.parametrize("channels", [1, 2, 3, 6])
    def test_channel_count_parametrized(self, mock_schematic, channels):
        """Number of resistors equals the ``channels`` argument."""
        block = create_gate_drive_resistor_array(mock_schematic, x=100, y=100, channels=channels)

        assert len(block.resistors) == channels
        assert len(block.components) == channels
        assert mock_schematic.add_symbol.call_count == channels
        # IN_i and OUT_i ports for each channel.
        for i in range(1, channels + 1):
            assert f"IN_{i}" in block.ports
            assert f"OUT_{i}" in block.ports

    def test_value_propagation(self, mock_schematic):
        """Custom value passed to every resistor."""
        block = create_gate_drive_resistor_array(
            mock_schematic, x=100, y=100, channels=3, value="33"
        )

        for comp in block.resistors:
            assert comp.value == "33"
        # Verify the value is passed to add_symbol as the 5th positional arg.
        for call in mock_schematic.add_symbol.call_args_list:
            args, _kwargs = call
            assert args[4] == "33"

    def test_ref_start_offset(self, mock_schematic):
        """``ref_start=20`` produces R20, R21, R22 for 3 channels."""
        block = create_gate_drive_resistor_array(
            mock_schematic, x=100, y=100, channels=3, ref_start=20
        )

        refs = [comp.reference for comp in block.resistors]
        assert refs == ["R20", "R21", "R22"]

    def test_ports_present(self, mock_schematic):
        """``IN_i`` / ``OUT_i`` ports exist with sane coordinates."""
        block = create_gate_drive_resistor_array(
            mock_schematic, x=100, y=100, channels=3, spacing=10
        )

        # Each resistor is at x=100 + i*10. Pin 1 is at x-5, pin 2 at x+5.
        for i in range(3):
            res_x = 100 + i * 10
            in_pos = block.ports[f"IN_{i + 1}"]
            out_pos = block.ports[f"OUT_{i + 1}"]
            assert in_pos == (res_x - 5, 100)
            assert out_pos == (res_x + 5, 100)
            # Input is to the left of output (driver-side -> MOSFET-side).
            assert in_pos[0] < out_pos[0]

    def test_input_output_net_aliases(self, mock_schematic):
        """When ``input_nets``/``output_nets`` provided, aliases and labels appear."""
        block = create_gate_drive_resistor_array(
            mock_schematic,
            x=100,
            y=100,
            channels=1,
            input_nets=["GATE_DRV_AH"],
            output_nets=["GATE_AH"],
        )

        # Alias ports keyed by the suffix.
        assert "IN_AH" in block.ports
        assert "OUT_AH" in block.ports
        # Numeric ports also still present.
        assert "IN_1" in block.ports
        assert "OUT_1" in block.ports

        # Both labels emitted at the correct pin coordinates.
        labels_called = [call.args[0] for call in mock_schematic.add_label.call_args_list]
        assert "GATE_DRV_AH" in labels_called
        assert "GATE_AH" in labels_called

        # And each label sits at the corresponding pin position.
        in_pos = block.ports["IN_1"]
        out_pos = block.ports["OUT_1"]
        for call in mock_schematic.add_label.call_args_list:
            args, _kw = call
            net_name, lx, ly = args[0], args[1], args[2]
            if net_name == "GATE_DRV_AH":
                assert (lx, ly) == in_pos
            elif net_name == "GATE_AH":
                assert (lx, ly) == out_pos

    def test_net_list_length_validation(self, mock_schematic):
        """Mismatched ``input_nets``/``output_nets`` length raises ValueError."""
        # input_nets too short
        with pytest.raises(ValueError, match="input_nets length"):
            create_gate_drive_resistor_array(
                mock_schematic,
                x=100,
                y=100,
                channels=3,
                input_nets=["A", "B"],  # only 2, expected 3
            )

        # output_nets too long
        with pytest.raises(ValueError, match="output_nets length"):
            create_gate_drive_resistor_array(
                mock_schematic,
                x=100,
                y=100,
                channels=2,
                output_nets=["A", "B", "C"],  # 3, expected 2
            )

    def test_resistor_package_property(self, mock_schematic):
        """Custom ``resistor_package`` is forwarded as a Package property."""
        block = create_gate_drive_resistor_array(
            mock_schematic, x=100, y=100, channels=2, resistor_package="0603"
        )

        assert block.resistor_package == "0603"
        # Each add_symbol call carries properties={"Package": "0603"}.
        for call in mock_schematic.add_symbol.call_args_list:
            _args, kwargs = call
            assert kwargs["properties"]["Package"] == "0603"

    def test_returns_circuit_block(self, mock_schematic):
        """Result is a CircuitBlock subclass with correct sch/x/y attrs."""
        block = create_gate_drive_resistor_array(mock_schematic, x=42, y=84, channels=2)

        assert isinstance(block, CircuitBlock)
        assert isinstance(block, GateDriveResistorArray)
        assert block.schematic is mock_schematic
        assert block.x == 42
        assert block.y == 84

    def test_components_dict_keyed(self, mock_schematic):
        """``block.components`` keyed by R_GATE_1..N."""
        block = create_gate_drive_resistor_array(mock_schematic, x=100, y=100, channels=4)

        assert set(block.components.keys()) == {
            "R_GATE_1",
            "R_GATE_2",
            "R_GATE_3",
            "R_GATE_4",
        }
        # Each value is the resistor symbol mock.
        for i, comp in enumerate(block.resistors):
            assert block.components[f"R_GATE_{i + 1}"] is comp
