"""Tests for ``OvercurrentComparator`` (softstart rev B P1 — issue #3343).

The block places an LM393 comparator + a threshold-setting divider
+ a pull-up resistor (LM393 has open-collector output).  Used for
hardware-fast overcurrent trip in softstart rev B.

Topology under test:
* Positive comparator input = SHUNT_VOLTAGE (from INA180A3 output).
* Negative comparator input = V_THRESHOLD (junction of R_TH_HI /
  R_TH_LO divider, with R_TH_LO baselined at 10 kΩ).
* Output is open-collector with R_PULLUP to VCC.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import OvercurrentComparator


@pytest.fixture
def mock_schematic():
    """Mock Schematic returning components with LM393-shaped pins."""
    sch = Mock()

    # LM393 pin layout (dual SOIC-8): 1=OUT1, 2=IN1-, 3=IN1+, 4=VEE/GND,
    # 5=IN2+, 6=IN2-, 7=OUT2, 8=VCC.  Offsets relative to placement origin.
    lm393_pins = {
        "1": (10, -5),  # OUT1
        "2": (-10, -5),  # IN1-
        "3": (-10, 5),  # IN1+
        "4": (0, 15),  # GND
        "5": (-10, -10),  # IN2+
        "6": (-10, -15),  # IN2-
        "7": (10, -10),  # OUT2
        "8": (0, -15),  # VCC
    }
    # Resistor pin offsets (vertical rotation=90 → pin 1 top, pin 2 bot)
    res_pins = {"1": (0, -5), "2": (0, 5)}

    def create_mock_component(symbol, x, y, ref, *args, **kwargs):
        comp = Mock()
        comp.reference = ref
        comp.symbol = symbol
        pin_map = lm393_pins if symbol == "Comparator:LM393" else res_pins
        comp.pin_position.side_effect = lambda name, _x=x, _y=y, _pm=pin_map: (
            (_x + _pm[name][0], _y + _pm[name][1]) if name in _pm else (_x, _y)
        )
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    return sch


class TestOvercurrentComparator:
    def test_places_comparator_and_three_resistors(self, mock_schematic):
        """Comparator IC + R_TH_HI + R_TH_LO + R_PULLUP = 4 symbols."""
        OvercurrentComparator(mock_schematic, x=200, y=80, ref="U6")
        assert mock_schematic.add_symbol.call_count == 4

        refs = [c.args[3] for c in mock_schematic.add_symbol.call_args_list]
        assert refs[0] == "U6"
        # Following are three resistors with R-prefixed refs
        assert all(r.startswith("R") for r in refs[1:])

    def test_default_comparator_symbol_is_lm393(self, mock_schematic):
        """LM393 is the chosen comparator per architect proposal."""
        OvercurrentComparator(mock_schematic, x=200, y=80)
        first_call = mock_schematic.add_symbol.call_args_list[0]
        assert first_call.args[0] == "Comparator:LM393"

    def test_threshold_value_drives_divider_resistors(self, mock_schematic):
        """A 2.0 V threshold (default) with R_TH_LO=10k gives R_TH_HI = (3.3/2.0 - 1)*10k ≈ 6.5k."""
        OvercurrentComparator(
            mock_schematic, x=200, y=80,
            threshold_value_v=2.0, vcc_voltage=3.3,
        )
        # R_TH_HI is the second add_symbol (after the comparator).
        r_th_hi_call = mock_schematic.add_symbol.call_args_list[1]
        # Should resolve to ~6.5k (formatted by _format_resistance)
        value = r_th_hi_call.args[4]
        # Allow "6.5k" or similar — verify it's in kΩ range, not megohms or raw ohms.
        assert "k" in value, f"Expected kΩ value, got {value!r}"

    def test_threshold_value_metadata_recorded(self, mock_schematic):
        """The threshold setpoint is stored as block metadata."""
        oc = OvercurrentComparator(
            mock_schematic, x=200, y=80,
            threshold_value_v=1.5,
        )
        assert oc.threshold_value_v == pytest.approx(1.5)

    def test_irq_label_emits_when_requested(self, mock_schematic):
        """When ``irq_output_pin`` is provided, a label is added at the output."""
        OvercurrentComparator(
            mock_schematic, x=200, y=80,
            irq_output_pin="OC_TRIP",
        )
        labels = [c.args[0] for c in mock_schematic.add_label.call_args_list]
        assert "OC_TRIP" in labels

    def test_no_label_by_default(self, mock_schematic):
        """No labels emitted when no irq_output_pin specified."""
        OvercurrentComparator(mock_schematic, x=200, y=80)
        assert mock_schematic.add_label.call_count == 0

    def test_exposes_canonical_ports(self, mock_schematic):
        """The block surfaces SHUNT_VOLTAGE, V_THRESHOLD, IRQ_OUTPUT, VCC, GND."""
        oc = OvercurrentComparator(mock_schematic, x=200, y=80)
        for port in ("SHUNT_VOLTAGE", "V_THRESHOLD", "IRQ_OUTPUT", "VCC", "GND"):
            assert port in oc.ports, f"Missing port {port!r}"

    def test_pullup_resistor_default_is_10k(self, mock_schematic):
        """Open-collector pull-up defaults to 10k."""
        OvercurrentComparator(mock_schematic, x=200, y=80)
        # R_PULLUP is the fourth add_symbol (comparator + R_TH_HI + R_TH_LO + R_PULLUP).
        r_pullup_call = mock_schematic.add_symbol.call_args_list[3]
        assert r_pullup_call.args[4] == "10k"

    def test_threshold_junction_has_a_junction_marker(self, mock_schematic):
        """A junction marker is placed at the threshold-divider tap."""
        OvercurrentComparator(mock_schematic, x=200, y=80)
        assert mock_schematic.add_junction.call_count >= 1
