"""Tests for ``DualSupplyCascade`` and ``create_dual_supply_cascade``.

Covers the new buck → LDO cascade circuit block:

    VIN ──[BuckConverter]── V_MID ──[LDOBlock]── VOUT
          │   │   │   │            │  │  │   │
          U1  L1  D2  C3,C4        C5 U2 C6  │
    GND ──┴───┴───┴───┴────────────┴──┴──┴───┴── GND

Tests use a mocked ``Schematic`` similar to the patterns in
``tests/test_schematic_blocks.py`` so the assertions focus on
component placement, port wiring, and configuration plumbing rather
than full schematic generation.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import (
    BuckConverter,
    CircuitBlock,
    DualSupplyCascade,
    LDOBlock,
    create_dual_supply_cascade,
)


@pytest.fixture
def mock_schematic():
    """Mocked schematic supporting buck (LM2596) + AMS1117 LDO pin names.

    The mock maps pin names to deterministic positions relative to the
    component's centre (x, y). Both ``VIN/VOUT`` and the AMS1117-style
    ``VI/VO`` pin names are recognized so the cascade can resolve the
    LDO pins without raising ``PinNotFoundError``.
    """
    from kicad_tools.schematic.exceptions import PinNotFoundError

    sch = Mock()

    def create_mock_component(symbol, x, y, ref, *args, **kwargs):
        comp = Mock()
        comp.reference = ref
        comp.value = kwargs.get("value", args[0] if args else "")
        rotation = kwargs.get("rotation", 0)
        symbol_str = str(symbol)

        if "LM2596" in symbol_str or "Switching" in symbol_str:
            pin_map = {
                "VIN": (x - 15, y),
                "IN": (x - 15, y),
                "OUT": (x + 15, y),
                "SW": (x + 15, y),
                "VOUT": (x + 15, y),
                "GND": (x, y + 10),
                "VSS": (x, y + 10),
                "FB": (x + 5, y + 5),
                "ON/OFF": (x - 5, y + 5),
                "~{ON}/OFF": (x - 5, y + 5),
            }

            def _pin(name, _pm=pin_map):
                if name in _pm:
                    return _pm[name]
                raise PinNotFoundError(name, "LM2596", [])

            comp.pin_position.side_effect = _pin
        elif "AMS1117" in symbol_str or "AP1117" in symbol_str:
            # AMS1117 / AP1117 use VI / VO (no VIN / VOUT)
            pin_map = {
                "VI": (x - 10, y),
                "VO": (x + 10, y),
                "GND": (x, y + 10),
            }

            def _pin(name, _pm=pin_map):
                if name in _pm:
                    return _pm[name]
                raise PinNotFoundError(name, "AMS1117", [])

            comp.pin_position.side_effect = _pin
        elif "XC6206" in symbol_str or "Regulator_Linear" in symbol_str:
            # XC6206 uses VIN / VOUT
            pin_map = {
                "VIN": (x - 10, y),
                "VOUT": (x + 10, y),
                "GND": (x, y + 10),
            }

            def _pin(name, _pm=pin_map):
                if name in _pm:
                    return _pm[name]
                raise PinNotFoundError(name, "XC6206", [])

            comp.pin_position.side_effect = _pin
        elif "Device:L" in symbol_str:
            comp.pin_position.side_effect = lambda name: {
                "1": (x - 5, y),
                "2": (x + 5, y),
            }.get(name, (0, 0))
        elif "Schottky" in symbol_str or "D_" in symbol_str:
            if rotation == 90:
                comp.pin_position.side_effect = lambda name: {
                    "A": (x, y + 5),
                    "K": (x, y - 5),
                }.get(name, (0, 0))
            else:
                comp.pin_position.side_effect = lambda name: {
                    "A": (x - 5, y),
                    "K": (x + 5, y),
                }.get(name, (0, 0))
        else:
            # Default capacitor / resistor pins
            comp.pin_position.side_effect = lambda name: {
                "1": (x, y - 5),
                "2": (x, y + 5),
            }.get(name, (0, 0))
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.wire_to_rail = Mock()
    sch.wire_decoupling_cap = Mock()
    sch.add_rail = Mock()
    return sch


# ---------------------------------------------------------------------------
# Construction & per-stage configuration
# ---------------------------------------------------------------------------


def test_cascade_24v_5v_3v3_default(mock_schematic):
    """Board-05 case: 24V → 5V (LM2596-5.0) → 3.3V (AMS1117-3.3)."""
    cascade = create_dual_supply_cascade(
        mock_schematic,
        x_buck=80,
        x_ldo=140,
        y=100,
        vin=24.0,
        v_mid=5.0,
        vout=3.3,
        cap_ref_start=3,
    )

    assert isinstance(cascade, DualSupplyCascade)
    assert isinstance(cascade, CircuitBlock)
    assert isinstance(cascade.buck, BuckConverter)
    assert isinstance(cascade.ldo, LDOBlock)

    # Voltage parameters propagated
    assert cascade.vin == 24.0
    assert cascade.v_mid == 5.0
    assert cascade.vout == 3.3
    assert cascade.buck.input_voltage == 24.0
    assert cascade.buck.output_voltage == 5.0

    # Reference designators match the board-05 BOM (U1, U2, C3-C6, L1, D2)
    refs = [c.reference for c in cascade.components.values()]
    assert "U1" in refs  # buck regulator
    assert "U2" in refs  # LDO
    assert "L1" in refs  # buck inductor
    assert "D2" in refs  # buck Schottky diode
    assert "C3" in refs  # buck input cap
    assert "C4" in refs  # buck output cap
    assert "C5" in refs  # LDO input cap
    assert "C6" in refs  # LDO output cap

    # Composed components dict aliases child stages for BOM iteration
    assert "BUCK_REGULATOR" in cascade.components
    assert "BUCK_C_IN" in cascade.components
    assert "BUCK_C_OUT" in cascade.components
    assert "BUCK_L" in cascade.components
    assert "BUCK_D" in cascade.components
    assert "LDO_LDO" in cascade.components
    assert "LDO_C_IN" in cascade.components
    assert "LDO_C_OUT1" in cascade.components

    # Part values plumbed through the table
    assert cascade.buck.regulator.reference == "U1"
    assert cascade.ldo.ldo.reference == "U2"


def test_cascade_12v_5v_3v3_uses_same_parts(mock_schematic):
    """12V input case still picks LM2596-5.0 + AMS1117-3.3."""
    cascade = create_dual_supply_cascade(
        mock_schematic,
        x_buck=80,
        x_ldo=140,
        y=100,
        vin=12.0,
        v_mid=5.0,
        vout=3.3,
    )

    # Buck regulator gets the right input voltage (used by efficiency calc)
    assert cascade.buck.input_voltage == 12.0
    assert cascade.buck.output_voltage == 5.0
    # Both stages use the same parts as the 24V case (different Vin, same regulators)
    refs = [c.reference for c in cascade.components.values()]
    assert "U1" in refs and "U2" in refs


def test_cascade_48v_12v_5v(mock_schematic):
    """High-voltage case: 48V → 12V (LM2596-12) → 5V (AMS1117-5.0)."""
    cascade = create_dual_supply_cascade(
        mock_schematic,
        x_buck=80,
        x_ldo=140,
        y=100,
        vin=48.0,
        v_mid=12.0,
        vout=5.0,
    )

    assert cascade.buck.input_voltage == 48.0
    assert cascade.buck.output_voltage == 12.0
    assert cascade.vout == 5.0

    # Efficiency must be multiplicative and noticeably below the buck-only
    # estimate (linear regulator drops V_out / V_mid = 5/12 ≈ 0.42).
    total_eff = cascade.get_efficiency_estimate()
    buck_eff = cascade.buck.get_efficiency_estimate()
    assert total_eff < buck_eff
    assert total_eff < 0.5  # <50% wall-to-load is the whole point of this test


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


def test_cascade_ports(mock_schematic):
    """Cascade exposes VIN, V_MID, VOUT, GND ports backed by child positions."""
    cascade = create_dual_supply_cascade(
        mock_schematic,
        x_buck=80,
        x_ldo=140,
        y=100,
    )

    assert set(cascade.ports.keys()) == {"VIN", "V_MID", "VOUT", "GND"}

    # VIN comes from the buck stage
    assert cascade.ports["VIN"] == cascade.buck.ports["VIN"]
    # V_MID is the buck output (== LDO input rail)
    assert cascade.ports["V_MID"] == cascade.buck.ports["VOUT"]
    # VOUT comes from the LDO stage
    assert cascade.ports["VOUT"] == cascade.ldo.ports["VOUT"]
    # GND comes from the buck (shared with LDO)
    assert cascade.ports["GND"] == cascade.buck.ports["GND"]

    # Typed ports populated for future composition
    assert cascade.typed_ports["VIN"].direction == "input"
    assert cascade.typed_ports["VOUT"].direction == "output"
    assert cascade.typed_ports["V_MID"].direction == "bidirectional"


# ---------------------------------------------------------------------------
# connect_to_rails
# ---------------------------------------------------------------------------


def test_cascade_connect_to_rails(mock_schematic):
    """connect_to_rails invokes wire_to_rail / wire_decoupling_cap on both stages."""
    cascade = create_dual_supply_cascade(
        mock_schematic,
        x_buck=80,
        x_ldo=140,
        y=100,
    )

    # Reset call counts after construction so we only count rail wiring
    mock_schematic.wire_to_rail.reset_mock()
    mock_schematic.wire_decoupling_cap.reset_mock()
    mock_schematic.add_wire.reset_mock()

    cascade.connect_to_rails(
        vin_rail_y=20,
        v_mid_rail_y=40,
        vout_rail_y=60,
        gnd_rail_y=200,
    )

    # LDO uses sch.wire_to_rail for its three pins; buck uses sch.add_wire
    # directly. Combined, we expect at least 3 wire_to_rail calls (LDO VIN,
    # VOUT, GND) and several add_wire calls (buck VIN/VOUT/GND + diode anode).
    assert mock_schematic.wire_to_rail.call_count >= 3
    assert mock_schematic.add_wire.call_count >= 3

    # Both stages wire their decoupling caps:
    #   buck: input cap -> VIN/GND, output cap -> V_MID/GND  (2 calls)
    #   ldo:  input cap -> V_MID/GND, output cap -> VOUT/GND  (2 calls minimum)
    assert mock_schematic.wire_decoupling_cap.call_count >= 4

    # LDO output cap is wired to the VOUT rail (60), not back to V_MID (40).
    # Verify this by inspecting at least one wire_decoupling_cap call argued
    # with vout_rail_y=60.
    rail_args_seen = {
        (call.args[1], call.args[2]) for call in mock_schematic.wire_decoupling_cap.call_args_list
    }
    assert (60, 200) in rail_args_seen  # LDO output cap on VOUT rail
    assert (40, 200) in rail_args_seen  # LDO input cap and/or buck output cap on V_MID rail


# ---------------------------------------------------------------------------
# Efficiency estimate
# ---------------------------------------------------------------------------


def test_cascade_efficiency_estimate_multiplicative(mock_schematic):
    """Cascade efficiency = buck_efficiency * (vout / v_mid)."""
    cascade = create_dual_supply_cascade(
        mock_schematic,
        x_buck=80,
        x_ldo=140,
        y=100,
        vin=24.0,
        v_mid=5.0,
        vout=3.3,
    )

    expected_buck_eff = cascade.buck.get_efficiency_estimate()
    expected_ldo_eff = 3.3 / 5.0
    expected_total = expected_buck_eff * expected_ldo_eff

    actual = cascade.get_efficiency_estimate()
    assert actual == pytest.approx(expected_total, rel=1e-9)
    # Sanity: should be < 0.85 * 0.66 ~ 0.561 for the 24/5/3.3 case
    assert actual < 0.85 * (3.3 / 5.0) + 1e-9


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_cascade_unsupported_voltages_raises(mock_schematic):
    """Unsupported (vin, v_mid, vout) raises ValueError listing supported pairs."""
    with pytest.raises(ValueError) as exc_info:
        create_dual_supply_cascade(
            mock_schematic,
            x_buck=80,
            x_ldo=140,
            y=100,
            vin=24.0,
            v_mid=7.0,  # Not a supported intermediate
            vout=3.3,
        )

    msg = str(exc_info.value)
    assert "Unsupported" in msg
    # Error should mention at least one supported triple to guide the caller
    assert "24" in msg or "12" in msg or "48" in msg
    # And should point users at the manual-construction escape hatch
    assert "DualSupplyCascade" in msg


# ---------------------------------------------------------------------------
# Caller-side drilling
# ---------------------------------------------------------------------------


def test_cascade_caller_can_access_buck_pins(mock_schematic):
    """``cascade.buck.regulator.pin_position()`` works for board-side tweaks.

    Board 05 ties the LM2596 ``~{ON}/OFF`` pin to GND for always-on
    operation. After the refactor, the cascade must still allow this
    via ``cascade.buck.regulator.pin_position(...)``.
    """
    cascade = create_dual_supply_cascade(
        mock_schematic,
        x_buck=80,
        x_ldo=140,
        y=100,
    )

    # Drill into the buck regulator
    on_off_pos = cascade.buck.regulator.pin_position("~{ON}/OFF")
    assert on_off_pos == (80 - 5, 100 + 5)  # mock returns (x-5, y+5)

    # Also access the LDO regulator directly
    ldo_vi = cascade.ldo.ldo.pin_position("VI")
    assert ldo_vi == (140 - 10, 100)


# ---------------------------------------------------------------------------
# Direct subclass construction (escape hatch)
# ---------------------------------------------------------------------------


def test_cascade_direct_construction_with_overrides(mock_schematic):
    """``DualSupplyCascade(...)`` works directly with explicit symbol overrides."""
    cascade = DualSupplyCascade(
        mock_schematic,
        x_buck=80,
        x_ldo=140,
        y=100,
        vin=15.0,
        v_mid=5.0,
        vout=3.3,
        buck_value="LM2596-5.0",
        buck_symbol="Regulator_Switching:LM2596S-5",
        ldo_value="AMS1117-3.3",
        ldo_symbol="Regulator_Linear:AMS1117-3.3",
        ldo_input_cap="22uF",  # non-default cap
        ldo_output_caps=["22uF", "100nF"],
    )

    assert cascade.buck.input_voltage == 15.0
    assert cascade.ldo.input_cap.value == "22uF"
    # Two output caps configured -> two C_OUT entries
    assert "LDO_C_OUT1" in cascade.components
    assert "LDO_C_OUT2" in cascade.components


# ---------------------------------------------------------------------------
# Ref-designator stability (BOM check for board 05 refactor)
# ---------------------------------------------------------------------------


def test_cascade_board05_bom_refs(mock_schematic):
    """Ref designators on the cascade match the board-05 pre-refactor BOM.

    After the design.py refactor the generated schematic must still have
    refs U1/U2/C3/C4/C5/C6/L1/D2 (no renumbering, no extra parts).
    """
    cascade = create_dual_supply_cascade(
        mock_schematic,
        x_buck=80,
        x_ldo=140,
        y=100,
        vin=24.0,
        v_mid=5.0,
        vout=3.3,
        cap_ref_start=3,
        buck_ref="U1",
        ldo_ref="U2",
        buck_diode_ref="D2",
        buck_inductor_ref="L1",
    )

    refs = sorted({c.reference for c in cascade.components.values()})
    # Expect exactly the board-05 BOM: U1, U2, C3, C4, C5, C6, L1, D2
    assert refs == ["C3", "C4", "C5", "C6", "D2", "L1", "U1", "U2"]
