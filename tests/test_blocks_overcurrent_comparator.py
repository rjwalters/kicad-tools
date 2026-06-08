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
from kicad_tools.schematic.models.schematic import Schematic


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
        """Comparator IC (1 unit in the mock) + R_TH_HI + R_TH_LO + R_PULLUP.

        The mock schematic returns a bare ``Mock`` from ``add_symbol`` so the
        block treats the LM393 as single-unit and does not place units 2/3.
        Real LM393 placement against a live ``Schematic`` produces three
        unit instances; that's covered by ``TestLM393MultiUnitPlacement``
        below.
        """
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


class TestLM393MultiUnitPlacement:
    """Regression tests for the multi-unit LM393 pin-position bug (issue #3346).

    LM393 packs three logical units in one library entry:
      * unit 1 -- comparator channel A (pins 1=OUT, 2=IN-, 3=IN+)
      * unit 2 -- comparator channel B (pins 5=IN+, 6=IN-, 7=OUT)
      * unit 3 -- power               (pins 4=V-, 8=V+)

    The previous ``OvercurrentComparator`` placed only unit 1, then
    asked the unit-1 ``SymbolInstance`` for pin 4 / pin 8.  Because
    ``pin_position`` walked every pin in the parsed ``SymbolDef``
    regardless of unit, it returned a position derived by applying the
    unit-3 library offset to the unit-1 placement origin -- a phantom
    point that landed in empty space and broke ERC + caller wires.

    These tests exercise the real ``Schematic`` / KiCad-library code
    path (no mocks) so they catch any regression of the unit dispatch.
    """

    def test_pin_unit_metadata_is_propagated_through_symbol_def(self):
        """Every LM393 pin is tagged with the unit number that owns it."""
        sch = Schematic(title="test")
        u = sch.add_symbol("Comparator:LM393", 100, 100, "U1", "LM393")
        unit_by_pin = {p.number: p.unit for p in u.symbol_def.pins}
        # Channel A
        assert unit_by_pin["1"] == 1
        assert unit_by_pin["2"] == 1
        assert unit_by_pin["3"] == 1
        # Channel B
        assert unit_by_pin["5"] == 2
        assert unit_by_pin["6"] == 2
        assert unit_by_pin["7"] == 2
        # Power
        assert unit_by_pin["4"] == 3
        assert unit_by_pin["8"] == 3

    def test_unit_count_reports_three_for_lm393(self):
        """``SymbolDef.unit_count`` reports the highest unit observed."""
        sch = Schematic(title="test")
        u = sch.add_symbol("Comparator:LM393", 100, 100, "U1", "LM393")
        assert u.symbol_def.unit_count() == 3

    def test_block_places_three_unit_instances_for_lm393(self):
        """The block emits one ``SymbolInstance`` per LM393 unit."""
        sch = Schematic(title="test")
        block = OvercurrentComparator(sch, x=200, y=100, ref="U7")
        lm_units = sorted(
            inst.unit
            for inst in sch.symbols
            if inst.symbol_def.lib_id == "Comparator:LM393"
            and inst.reference == "U7"
        )
        assert lm_units == [1, 2, 3], (
            f"Expected channel A + channel B + power units; got {lm_units}"
        )
        # The block's _unit_instances map matches.
        assert set(block._unit_instances.keys()) == {1, 2, 3}

    def test_power_pin_position_is_resolved_against_unit_3(self):
        """Pin 4 (V-) and pin 8 (V+) resolve via the unit-3 instance.

        Without the fix this used to return ``unit_1.x + unit_3_offset``,
        a non-grid phantom point.  After the fix it returns the unit-3
        instance's own placement plus the (-2.54, +/-7.62) library
        offset -- a real schematic pin on the unit-3 outline.
        """
        sch = Schematic(title="test")
        block = OvercurrentComparator(sch, x=200, y=100, ref="U7")
        unit3 = block._unit_instances[3]
        # Pin 8 (V+) is at library offset (-2.54, +7.62) on unit 3.
        # Library Y is up, schematic Y is down, so the rendered offset
        # is (-2.54, -7.62) relative to the unit-3 placement origin.
        expected_vcc = (unit3.x - 2.54, unit3.y - 7.62)
        expected_gnd = (unit3.x - 2.54, unit3.y + 7.62)
        assert block.pin_position("8") == expected_vcc
        assert block.pin_position("4") == expected_gnd
        # And ports surface the same coordinates.
        assert block.ports["VCC"] == expected_vcc
        assert block.ports["GND"] == expected_gnd

    def test_channel_a_pins_resolve_against_unit_1(self):
        """Pins 1/2/3 still resolve against the unit-1 placement origin."""
        sch = Schematic(title="test")
        block = OvercurrentComparator(sch, x=200, y=100, ref="U7")
        unit1 = block._unit_instances[1]
        # Library offset for pin 1 (OUT) is (+7.62, 0) on unit 1.
        expected_out = (unit1.x + 7.62, unit1.y - 0.0)
        assert block.pin_position("1") == expected_out

    def test_channel_b_pins_marked_no_connect(self):
        """Unit 2 pins (5/6/7) are auto-tagged ``no_connect`` to satisfy ERC."""
        sch = Schematic(title="test")
        OvercurrentComparator(sch, x=200, y=100, ref="U7")
        # ``Schematic`` stores no-connects in ``self.no_connects``.
        nc_count = len(getattr(sch, "no_connects", []))
        # 3 NCs from channel B, plus any others the block may add.
        assert nc_count >= 3

    def test_pin_position_no_longer_returns_phantom_unit1_for_pin_4(self):
        """Direct evidence the bug is gone.

        Before the fix: ``unit1_instance.pin_position("4")`` returned a
        position computed from the unit-1 placement origin plus unit
        3's library offset -- mathematically valid coordinates, but
        nowhere near where pin 4 is actually drawn.  We confirm the
        block-level dispatch returns the unit-3 coordinate instead.
        """
        sch = Schematic(title="test")
        block = OvercurrentComparator(sch, x=200, y=100, ref="U7")
        unit1 = block._unit_instances[1]
        unit3 = block._unit_instances[3]
        # The unit-1 instance and unit-3 instance are placed at
        # different coordinates by the block, so applying the same
        # library offset to each gives distinguishable results.
        assert unit1.x != unit3.x or unit1.y != unit3.y
        pin4_via_block = block.pin_position("4")
        pin4_via_unit3 = unit3.pin_position("4")
        assert pin4_via_block == pin4_via_unit3
