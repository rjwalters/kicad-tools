"""Tests for ``UCC27211GateDriver`` (softstart rev B P1 — issue #3343).

The block places a UCC27211 SOIC-8 half-bridge gate driver plus its
bootstrap capacitor and VDD supply decoupling.  Load-bearing
invariants verified here:

* The Kelvin source reference (the driver's VSS pin) is exposed as a
  named port — the recipe wires it directly to the back-to-back FET
  pair's SOURCE port, NOT to the power-GND pour.
* UVLO trip + dV/dt immunity are stored as block metadata (intrinsic
  IC properties, not separate components).
* ``failsafe_pulldown_node`` is exposed as a dict of LI/HI positions
  so P2 can wire 2N7002 drains to those nets per the Q8 resolution.

Tests use a mocked ``Schematic``; pin positions follow the
``softstart_custom:UCC27211`` symbol's numbering (pins 1-8 with HB=1,
HO=2, HS=3, VDD=4, HI=5, LI=6, VSS=7, LO=8).
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import UCC27211GateDriver


@pytest.fixture
def mock_schematic():
    """Mock Schematic returning UCC27211-shaped components."""
    sch = Mock()

    # Pin layout mirrors the custom symbol file.  Offsets are relative
    # to the symbol's placement origin (x, y).
    ucc_pins = {
        "1": (10, -7.62),  # HB
        "2": (10, -5.08),  # HO
        "3": (10, -2.54),  # HS
        "4": (0, -12.7),  # VDD
        "5": (-10, -5.08),  # HI
        "6": (-10, -2.54),  # LI
        "7": (0, 12.7),  # VSS / Kelvin
        "8": (10, 5.08),  # LO
    }
    # Cap pins
    cap_pins = {"1": (0, -5), "2": (0, 5)}

    def create_mock_component(symbol, x, y, ref, *args, **kwargs):
        comp = Mock()
        comp.reference = ref
        comp.symbol = symbol
        if "C" in symbol or symbol == "Device:C":
            pin_map = cap_pins
        else:
            pin_map = ucc_pins
        comp.pin_position.side_effect = lambda name, _x=x, _y=y, _pm=pin_map: (
            (_x + _pm[name][0], _y + _pm[name][1]) if name in _pm else (_x, _y)
        )
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    return sch


class TestUCC27211GateDriver:
    def test_places_driver_and_three_caps(self, mock_schematic):
        """Driver IC + bootstrap cap + VCC bulk + VCC bypass = 4 symbols."""
        UCC27211GateDriver(mock_schematic, x=100, y=100, ref="U5")
        assert mock_schematic.add_symbol.call_count == 4

        refs = [call.args[3] for call in mock_schematic.add_symbol.call_args_list]
        assert refs[0] == "U5"  # driver first
        # Three caps follow with default refs C1/C2/C3
        assert all(r.startswith("C") for r in refs[1:])

    def test_exposes_kelvin_source_port(self, mock_schematic):
        """``KELVIN_SOURCE`` must be present and equal to the VSS pin position."""
        drv = UCC27211GateDriver(mock_schematic, x=100, y=100)
        assert "KELVIN_SOURCE" in drv.ports
        # KELVIN_SOURCE is an alias of VSS — same position.
        assert drv.port("KELVIN_SOURCE") == drv.port("VSS")

    def test_exposes_all_required_ports(self, mock_schematic):
        """All eight driver pins are reachable via .port()."""
        drv = UCC27211GateDriver(mock_schematic, x=100, y=100)
        for port in ("VDD", "VSS", "LI", "HI", "LO", "HO", "HB", "HS", "KELVIN_SOURCE"):
            assert port in drv.ports, f"Missing port {port!r}"

    def test_metadata_records_uvlo_and_peak_drive(self, mock_schematic):
        """UVLO trip + peak drive + dV/dt immunity stored as block attrs.

        These are intrinsic IC properties — they shouldn't appear as
        separate components, but they should be queryable by downstream
        tools (ThermalAnalyzer, intent-aware audits).
        """
        drv = UCC27211GateDriver(mock_schematic, x=100, y=100)
        assert drv.uvlo_trip_v == pytest.approx(7.4)
        assert drv.peak_drive_a == pytest.approx(4.0)
        assert drv.dvdt_immunity_v_per_ns == pytest.approx(50)

    def test_failsafe_pulldown_node_exposes_li_and_hi(self, mock_schematic):
        """Q8 resolution: failsafe topology pulls LI/HI low.

        The block must expose attachment points so P2 can wire 2N7002
        drains to LI and HI when NRST is asserted.
        """
        drv = UCC27211GateDriver(mock_schematic, x=100, y=100)
        assert hasattr(drv, "failsafe_pulldown_node")
        assert "LI" in drv.failsafe_pulldown_node
        assert "HI" in drv.failsafe_pulldown_node
        # Positions match the driver's LI and HI pin positions.
        assert drv.failsafe_pulldown_node["LI"] == drv.port("LI")
        assert drv.failsafe_pulldown_node["HI"] == drv.port("HI")

    def test_no_labels_by_default(self, mock_schematic):
        """No optional net labels emitted in default configuration."""
        UCC27211GateDriver(mock_schematic, x=100, y=100)
        assert mock_schematic.add_label.call_count == 0

    def test_li_net_emits_one_label(self, mock_schematic):
        """``li_net`` triggers a single label at the LI stub endpoint."""
        UCC27211GateDriver(
            mock_schematic,
            x=100,
            y=100,
            li_net="GATE_POS_B",
        )
        labels = [c.args[0] for c in mock_schematic.add_label.call_args_list]
        assert labels == ["GATE_POS_B"]

    def test_kelvin_source_net_emits_label(self, mock_schematic):
        """``kelvin_source_net`` labels the Kelvin tie point."""
        UCC27211GateDriver(
            mock_schematic,
            x=100,
            y=100,
            kelvin_source_net="SRC_POS",
        )
        labels = [c.args[0] for c in mock_schematic.add_label.call_args_list]
        assert "SRC_POS" in labels

    def test_default_driver_symbol_is_custom(self, mock_schematic):
        """Default symbol is the project-local UCC27211, not UCC27714D.

        The Q1 resolution explicitly rejected UCC27714D as a stand-in;
        this test prevents regression to it.
        """
        UCC27211GateDriver(mock_schematic, x=100, y=100)
        first_call = mock_schematic.add_symbol.call_args_list[0]
        assert first_call.args[0] == "softstart_custom:UCC27211"
