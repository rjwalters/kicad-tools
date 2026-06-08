"""Tests for ``PrechargeSubsystem`` (softstart rev B P1 — issue #3343).

The block models an inrush-limited connect: a small N-FET (AO3400)
in series with a 100Ω 5W axial resistor, driven by an MCU GPIO.
Used by softstart rev B to bound supercap bank precharge current to
~0.8 A peak before the main switching FETs close.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import PrechargeSubsystem


@pytest.fixture
def mock_schematic():
    """Mock Schematic with resistor + FET pin conventions."""
    sch = Mock()

    def create_mock_component(symbol, x, y, ref, *args, **kwargs):
        comp = Mock()
        comp.reference = ref
        comp.symbol = symbol
        # Resistor pins: 1=left, 2=right (horizontal)
        # FET pins: D=top, G=left, S=bottom
        comp.pin_position.side_effect = lambda name, _x=x, _y=y: {
            "1": (_x - 5, _y),
            "2": (_x + 5, _y),
            "D": (_x, _y - 10),
            "G": (_x - 10, _y),
            "S": (_x, _y + 10),
        }.get(name, (_x, _y))
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    return sch


class TestPrechargeSubsystem:
    def test_places_resistor_and_fet(self, mock_schematic):
        """Exactly two symbols: 100R resistor + AO3400 FET."""
        PrechargeSubsystem(mock_schematic, x=100, y=80)
        assert mock_schematic.add_symbol.call_count == 2

        # Resistor first, FET second
        calls = mock_schematic.add_symbol.call_args_list
        assert calls[0].args[0] == "Device:R"
        assert calls[1].args[0] == "Transistor_FET:AO3400A"

    def test_default_resistor_value_is_100R(self, mock_schematic):
        """Per softstart rev B BOM, default precharge resistor is 100Ω."""
        PrechargeSubsystem(mock_schematic, x=100, y=80)
        r_call = mock_schematic.add_symbol.call_args_list[0]
        assert r_call.args[4] == "100R"

    def test_default_fet_is_ao3400(self, mock_schematic):
        """Default FET part is AO3400 (rev B BOM choice)."""
        PrechargeSubsystem(mock_schematic, x=100, y=80)
        q_call = mock_schematic.add_symbol.call_args_list[1]
        assert q_call.args[4] == "AO3400"

    def test_exposes_required_ports(self, mock_schematic):
        """Block exposes MAIN_DRIVE, TARGET, MONITOR, GND ports."""
        pre = PrechargeSubsystem(mock_schematic, x=100, y=80)
        for port in ("MAIN_DRIVE", "TARGET", "MONITOR", "GND"):
            assert port in pre.ports, f"Missing port {port!r}"

    def test_monitor_label_emits_when_requested(self, mock_schematic):
        """When ``monitor_label`` is provided, one label is added at the FET gate."""
        PrechargeSubsystem(
            mock_schematic, x=100, y=80,
            monitor_label="PRECHARGE_POS",
        )
        labels = [c.args[0] for c in mock_schematic.add_label.call_args_list]
        assert labels == ["PRECHARGE_POS"]

    def test_no_label_by_default(self, mock_schematic):
        """Default construction emits no labels (back-compat)."""
        PrechargeSubsystem(mock_schematic, x=100, y=80)
        assert mock_schematic.add_label.call_count == 0

    def test_resistor_power_rating_recorded(self, mock_schematic):
        """The 5W power rating is stored as a custom resistor property."""
        PrechargeSubsystem(mock_schematic, x=100, y=80)
        r_call = mock_schematic.add_symbol.call_args_list[0]
        props = r_call.kwargs.get("properties", {})
        assert props.get("Power_Rating") == "5W"

    def test_resistor_footprint_is_axial_5w(self, mock_schematic):
        """The default resistor footprint is the axial 5W P25.40mm horizontal part."""
        PrechargeSubsystem(mock_schematic, x=100, y=80)
        r_call = mock_schematic.add_symbol.call_args_list[0]
        fp = r_call.kwargs.get("footprint", "")
        assert "Axial" in fp
        assert "5W" in r_call.kwargs.get("properties", {}).get("Power_Rating", "")

    def test_custom_refs_propagate(self, mock_schematic):
        """Override ref_q / ref_r both flow through to add_symbol."""
        PrechargeSubsystem(
            mock_schematic, x=100, y=80,
            ref_q="Q42", ref_r="R99",
        )
        calls = mock_schematic.add_symbol.call_args_list
        assert calls[0].args[3] == "R99"
        assert calls[1].args[3] == "Q42"

    def test_metadata_records_resistor_value(self, mock_schematic):
        """Block metadata stores resistor_value for downstream queries."""
        pre = PrechargeSubsystem(mock_schematic, x=100, y=80)
        assert pre.resistor_value == "100R"
        assert pre.resistor_power == "5W"
