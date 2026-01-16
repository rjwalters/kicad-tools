#!/usr/bin/env python3
"""
BLDC Motor Controller - Schematic Generation

This script demonstrates creating a power electronics schematic with:
1. Multi-voltage power supply (12-24V → 5V → 3.3V)
2. Gate driver for 3-phase motor control
3. Power MOSFET half-bridges
4. Current sensing with shunt resistors
5. MCU for motor control

The design exercises kicad-tools thermal analysis and high-current
routing capabilities.

Usage:
    python design.py [output_dir]

If no output directory is specified, files are written to ./output/
"""

import sys
from pathlib import Path

from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic.blocks import (
    CrystalOscillator,
    CurrentSenseShunt,
    DebugHeader,
    GateDriverBlock,
    LEDIndicator,
    ThreePhaseInverter,
    create_5v_buck,
)
from kicad_tools.schematic.models.schematic import Schematic

# Warn if running source scripts with stale pipx install
warn_if_stale()


def create_bldc_controller(output_dir: Path) -> None:
    """
    Create a BLDC motor controller schematic.

    This demonstrates power electronics design patterns:
    - High-current power input with protection
    - Multi-stage power supply (buck + LDO)
    - 3-phase gate driver
    - Power MOSFET half-bridges
    - Current sensing for closed-loop control
    """
    print("Creating BLDC Motor Controller schematic...")
    print("=" * 60)

    # Create schematic with title block
    sch = Schematic(
        title="BLDC Motor Controller",
        date="2025-01",
        revision="A",
        company="kicad-tools Example",
        comment1="3-Phase Brushless DC Motor Driver",
        comment2="Thermal analysis and high-current routing demo",
    )

    # Define power rail Y coordinates
    RAIL_VMOTOR = 25  # 12-24V motor power
    RAIL_5V = 45  # 5V gate driver supply
    RAIL_3V3 = 65  # 3.3V logic supply
    RAIL_GND = 280  # Ground

    # Schematic section X positions
    X_POWER_IN = 25  # Power input section
    X_BUCK = 80  # Buck converter
    X_LDO = 140  # LDO regulator
    X_MCU = 200  # MCU section
    X_GATE_DRV = 280  # Gate driver
    X_PHASE_A = 25  # Phase A half-bridge
    X_PHASE_C = 175  # Phase C half-bridge
    X_CONNECTORS = 260  # Connectors section

    # Y position for power stage (lower section)
    Y_POWER_STAGE = 160

    # =========================================================================
    # Section 1: Power Rails
    # =========================================================================
    print("\n1. Creating power rails...")

    # Motor power rail (12-24V)
    sch.add_rail(RAIL_VMOTOR, x_start=X_POWER_IN, x_end=X_PHASE_C + 60, net_label="VMOTOR")
    sch.add_power("power:+24V", x=X_POWER_IN, y=RAIL_VMOTOR - 10, rotation=0)

    # 5V rail
    sch.add_rail(RAIL_5V, x_start=X_BUCK + 25, x_end=X_GATE_DRV + 30, net_label="+5V")
    sch.add_power("power:+5V", x=X_BUCK + 25, y=RAIL_5V - 10, rotation=0)

    # 3.3V rail
    sch.add_rail(RAIL_3V3, x_start=X_LDO + 25, x_end=X_MCU + 80, net_label="+3.3V")
    sch.add_power("power:+3V3", x=X_LDO + 25, y=RAIL_3V3 - 10, rotation=0)

    # Ground rail (spans full width)
    sch.add_rail(RAIL_GND, x_start=X_POWER_IN, x_end=X_CONNECTORS + 40, net_label="GND")
    sch.add_power("power:GND", x=X_POWER_IN, y=RAIL_GND + 10, rotation=0)

    print("   Added VMOTOR, +5V, +3.3V, and GND rails")

    # =========================================================================
    # Section 2: Power Input (12-24V DC)
    # =========================================================================
    print("\n2. Adding power input section...")

    # Power input connector (2-pin)
    j_power = sch.add_symbol(
        "Connector:Conn_01x02_Pin",
        x=X_POWER_IN,
        y=100,
        ref="J1",
        value="Power Input",
    )
    print(f"   Power connector: {j_power.reference}")

    # Input fuse
    fuse = sch.add_symbol(
        "Device:Fuse",
        x=X_POWER_IN + 20,
        y=100,
        ref="F1",
        value="15A",
    )
    print(f"   Fuse: {fuse.reference} = 15A")

    # TVS diode for transient protection
    tvs = sch.add_symbol(
        "Device:D_TVS",
        x=X_POWER_IN + 40,
        y=120,
        ref="D1",
        value="SMBJ24A",
        rotation=90,
    )
    print(f"   TVS diode: {tvs.reference}")

    # Bulk input capacitors
    c_bulk1 = sch.add_symbol(
        "Device:C",
        x=X_POWER_IN + 55,
        y=120,
        ref="C1",
        value="470uF",
    )
    c_bulk2 = sch.add_symbol(
        "Device:C",
        x=X_POWER_IN + 70,
        y=120,
        ref="C2",
        value="100nF",
    )
    print(f"   Bulk caps: {c_bulk1.reference} = 470uF, {c_bulk2.reference} = 100nF")

    # Wire power input to VMOTOR rail
    # Connector pin 1 → Fuse → TVS/Caps → VMOTOR rail
    pin1_pos = j_power.pin_position("1")
    fuse_in = fuse.pin_position("1")
    fuse_out = fuse.pin_position("2")

    sch.add_wire(pin1_pos, fuse_in)
    sch.add_wire(fuse_out, (fuse_out[0], RAIL_VMOTOR))
    sch.add_junction(fuse_out[0], RAIL_VMOTOR)

    # Wire TVS and caps to rails (TVS has pins A1, A2 for bidirectional)
    tvs_a1 = tvs.pin_position("A1")
    tvs_a2 = tvs.pin_position("A2")
    sch.add_wire(tvs_a1, (tvs_a1[0], RAIL_VMOTOR))
    sch.add_junction(tvs_a1[0], RAIL_VMOTOR)
    sch.add_wire(tvs_a2, (tvs_a2[0], RAIL_GND))
    sch.add_junction(tvs_a2[0], RAIL_GND)

    sch.wire_decoupling_cap(c_bulk1, RAIL_VMOTOR, RAIL_GND)
    sch.wire_decoupling_cap(c_bulk2, RAIL_VMOTOR, RAIL_GND)

    # Connector pin 2 → GND
    pin2_pos = j_power.pin_position("2")
    sch.add_wire(pin2_pos, (pin2_pos[0], RAIL_GND))
    sch.add_junction(pin2_pos[0], RAIL_GND)

    # =========================================================================
    # Section 3: Buck Converter (24V → 5V)
    # =========================================================================
    print("\n3. Adding buck converter (24V → 5V)...")

    # Create 5V buck converter using BuckConverter block
    # This creates the full LM2596 module with all supporting components:
    # - Regulator IC (U1)
    # - Input/output capacitors (C3, C4)
    # - Inductor (L1)
    # - Schottky diode (D2) for async topology
    buck = create_5v_buck(
        sch,
        x=X_BUCK,
        y=100,
        ref="U1",
        input_voltage=24.0,
        cap_ref_start=3,  # C3, C4
    )
    buck.connect_to_rails(
        vin_rail_y=RAIL_VMOTOR,
        vout_rail_y=RAIL_5V,
        gnd_rail_y=RAIL_GND,
    )
    print(f"   Buck regulator: {buck.regulator.reference} (LM2596-5.0)")
    print(f"   Inductor: {buck.inductor.reference} = 33uH")
    print(f"   Diode: {buck.diode.reference} = SS34 (Schottky)")
    print(f"   Estimated efficiency: {buck.get_efficiency_estimate() * 100:.0f}%")

    # =========================================================================
    # Section 4: LDO (5V → 3.3V)
    # =========================================================================
    print("\n4. Adding LDO (5V → 3.3V)...")

    ldo = sch.add_symbol(
        "Regulator_Linear:AMS1117-3.3",
        x=X_LDO,
        y=100,
        ref="U2",
        value="AMS1117-3.3",
    )
    print(f"   LDO: {ldo.reference}")

    # LDO capacitors
    c_ldo_in = sch.add_symbol(
        "Device:C",
        x=X_LDO - 15,
        y=120,
        ref="C5",
        value="10uF",
    )
    c_ldo_out = sch.add_symbol(
        "Device:C",
        x=X_LDO + 25,
        y=120,
        ref="C6",
        value="10uF",
    )

    # Wire LDO
    ldo_vin = ldo.pin_position("VI")
    ldo_vout = ldo.pin_position("VO")
    ldo_gnd = ldo.pin_position("GND")

    sch.add_wire(ldo_vin, (ldo_vin[0], RAIL_5V))
    sch.add_junction(ldo_vin[0], RAIL_5V)
    sch.add_wire(ldo_vout, (ldo_vout[0], RAIL_3V3))
    sch.add_junction(ldo_vout[0], RAIL_3V3)
    sch.add_wire(ldo_gnd, (ldo_gnd[0], RAIL_GND))
    sch.add_junction(ldo_gnd[0], RAIL_GND)

    sch.wire_decoupling_cap(c_ldo_in, RAIL_5V, RAIL_GND)
    sch.wire_decoupling_cap(c_ldo_out, RAIL_3V3, RAIL_GND)

    # =========================================================================
    # Section 5: MCU (STM32G431)
    # =========================================================================
    print("\n5. Adding MCU section...")

    # MCU placeholder - STM32G4 for motor control
    # Note: Full MCU symbol would be added from KiCad library
    sch.add_text(
        "MCU: STM32G431KB\n(Add from KiCad library:\nMCU_ST_STM32G4)",
        x=X_MCU + 30,
        y=115,
    )
    print("   MCU: STM32G431KB (placeholder)")

    # Bypass capacitors for MCU
    c_mcu1 = sch.add_symbol("Device:C", x=X_MCU, y=100, ref="C7", value="100nF")
    c_mcu2 = sch.add_symbol("Device:C", x=X_MCU + 10, y=100, ref="C8", value="100nF")
    c_mcu3 = sch.add_symbol("Device:C", x=X_MCU + 20, y=100, ref="C9", value="4.7uF")
    print(f"   Bypass caps: {c_mcu1.reference}, {c_mcu2.reference}, {c_mcu3.reference}")

    for cap in [c_mcu1, c_mcu2, c_mcu3]:
        sch.wire_decoupling_cap(cap, RAIL_3V3, RAIL_GND)

    # Crystal oscillator (8MHz)
    xtal = CrystalOscillator(
        sch,
        x=X_MCU + 70,
        y=100,
        frequency="8MHz",
        load_caps="20pF",
        ref_prefix="Y",
        cap_ref_start=10,
    )
    xtal.connect_to_rails(gnd_rail_y=RAIL_GND)
    print(f"   Crystal: {xtal.crystal.reference} 8MHz")

    # Debug header (SWD)
    debug = DebugHeader(
        sch,
        x=X_MCU + 100,
        y=100,
        interface="swd",
        pins=6,
        ref="J4",
    )
    debug.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)
    print(f"   Debug header: {debug.header.reference}")

    # =========================================================================
    # Section 6: Gate Driver (using GateDriverBlock)
    # =========================================================================
    print("\n6. Adding gate driver...")

    # 3-phase gate driver with bootstrap capacitors
    # Note: C10-C11 are used by CrystalOscillator, so start at C12
    gate_driver = GateDriverBlock(
        sch,
        x=X_GATE_DRV,
        y=95,
        driver_type="3-phase",
        ref="U3",
        value="DRV8301",
        bootstrap_caps="100nF",
        bypass_caps=["100nF", "10uF"],
        cap_ref_start=12,  # C12-C14 for bootstrap, C15-C16 for bypass
    )
    gate_driver.connect_to_rails(vcc_rail_y=RAIL_5V, gnd_rail_y=RAIL_GND)
    print("   Gate driver: DRV8301 (GateDriverBlock)")
    print("   Bootstrap caps: C12, C13, C14")
    print("   Bypass caps: C15, C16")

    # =========================================================================
    # Section 7: Power Stage (using ThreePhaseInverter and CurrentSenseShunt)
    # =========================================================================
    print("\n7. Adding power stage (6 MOSFETs)...")

    # Create 3-phase inverter using ThreePhaseInverter block
    # This creates 6 MOSFETs (Q1-Q6) in three half-bridge configuration
    inverter = ThreePhaseInverter(
        sch,
        x=X_PHASE_A,
        y=Y_POWER_STAGE,
        ref_start=1,
        ref_prefix="Q",
        mosfet_value="IRLZ44N",
        phase_labels=["A", "B", "C"],
        phase_spacing=75,
        hs_ls_spacing=40,
    )
    inverter.connect_to_rails(vin_rail_y=RAIL_VMOTOR, gnd_rail_y=RAIL_GND)
    print("   Three-phase inverter: Q1-Q6 (ThreePhaseInverter block)")

    # Add current sense shunts for each phase (R10-R12)
    # Using CurrentSenseShunt blocks for proper current sensing
    phases = ["A", "B", "C"]
    current_sensors = []
    for i, phase in enumerate(phases):
        x_phase = X_PHASE_A + (i * 75)

        # Create current sense shunt for this phase
        sense = CurrentSenseShunt(
            sch,
            x=x_phase,
            y=Y_POWER_STAGE + 80,
            shunt_value="5mR",
            shunt_package="2512",
            ref_start=10 + i,  # R10, R11, R12
            amplifier=False,  # No amplifier for basic sensing
        )
        sense.connect_to_rails(gnd_rail_y=RAIL_GND)
        current_sensors.append(sense)

        # Wire the inverter LS output to the current sense input
        # Get the phase output from inverter and wire to shunt
        phase_gnd = inverter.half_bridges[i].port("GND")
        sense_in = sense.port("IN_POS")
        sch.add_wire(phase_gnd, sense_in)

        # Add current sense labels
        sch.add_label(f"ISENSE_{phase}+", x_phase - 10, sense_in[1], rotation=0)
        sch.add_label(f"ISENSE_{phase}-", x_phase - 10, sense.port("GND")[1], rotation=0)

        print(f"   Phase {phase}: Current sense R{10 + i} (CurrentSenseShunt block)")

    # =========================================================================
    # Section 8: Motor Output Connector
    # =========================================================================
    print("\n8. Adding motor output connector...")

    j_motor = sch.add_symbol(
        "Connector:Conn_01x03_Pin",
        x=X_CONNECTORS,
        y=Y_POWER_STAGE + 20,
        ref="J2",
        value="Motor Output",
    )
    print(f"   Motor connector: {j_motor.reference} (U/V/W)")

    # Wire motor phases from inverter block outputs to connector
    for i, phase in enumerate(phases):
        pin_pos = j_motor.pin_position(str(i + 1))
        # Get phase output from inverter block
        phase_out = inverter.port(f"PHASE_{phase}")
        # Wire from phase output node to connector
        sch.add_wire(pin_pos, (phase_out[0] + 15, pin_pos[1]))
        sch.add_wire((phase_out[0] + 15, pin_pos[1]), (phase_out[0] + 15, Y_POWER_STAGE + 20))

    # =========================================================================
    # Section 9: Hall Sensor Connector
    # =========================================================================
    print("\n9. Adding hall sensor connector...")

    j_hall = sch.add_symbol(
        "Connector:Conn_01x05_Pin",
        x=X_CONNECTORS,
        y=100,
        ref="J3",
        value="Hall Sensors",
    )
    print(f"   Hall connector: {j_hall.reference}")

    # Add hall signal labels
    sch.add_label("HALL_A", X_CONNECTORS - 20, 100, rotation=0)
    sch.add_label("HALL_B", X_CONNECTORS - 20, 105, rotation=0)
    sch.add_label("HALL_C", X_CONNECTORS - 20, 110, rotation=0)

    # =========================================================================
    # Section 10: Status LEDs
    # =========================================================================
    print("\n10. Adding status LEDs...")

    # Power LED
    led_pwr = LEDIndicator(
        sch,
        x=X_LDO + 50,
        y=120,
        ref_prefix="D3",
        label="PWR",
        resistor_value="1k",
    )
    led_pwr.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)
    print(f"   Power LED: {led_pwr.led.reference}")

    # Status LED
    led_status = LEDIndicator(
        sch,
        x=X_LDO + 70,
        y=120,
        ref_prefix="D4",
        label="STATUS",
        resistor_value="1k",
    )
    led_status.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)
    print(f"   Status LED: {led_status.led.reference}")

    # =========================================================================
    # Section 11: Design Notes
    # =========================================================================
    print("\n11. Adding design notes...")

    sch.add_text(
        "BLDC Motor Controller Design Notes:\n"
        "=====================================\n"
        "1. MOSFETs Q1-Q6 require thermal vias (min 6 per device)\n"
        "2. Use 2mm+ trace width for motor phase and power traces\n"
        "3. Ground plane on bottom layer for heat spreading\n"
        "4. Keep gate drive traces short (<10mm)\n"
        "5. Kelvin connection for current sense resistors\n"
        "6. Separate analog ground near current sense\n"
        "7. Bulk capacitors near MOSFETs for motor current\n",
        x=X_POWER_IN,
        y=RAIL_GND + 20,
    )

    # =========================================================================
    # Validate Schematic
    # =========================================================================
    print("\n12. Validating schematic...")

    issues = sch.validate()
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if errors:
        print(f"   Found {len(errors)} errors:")
        for err in errors[:5]:
            print(f"      - {err['message']}")
    else:
        print("   No errors found")

    if warnings:
        print(f"   Found {len(warnings)} warnings")

    # Get statistics
    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Power symbols: {stats['power_symbol_count']}")
    print(f"      Wires: {stats['wire_count']}")
    print(f"      Junctions: {stats['junction_count']}")
    print(f"      Labels: {stats['label_count']}")

    # =========================================================================
    # Write Output Files
    # =========================================================================
    print("\n13. Writing output files...")

    output_dir.mkdir(parents=True, exist_ok=True)

    sch_path = output_dir / "bldc_controller.kicad_sch"
    sch.write(sch_path)
    print(f"   Schematic: {sch_path}")

    # =========================================================================
    # Summary
    # =========================================================================
    print("\n" + "=" * 60)
    print("Design complete!")
    print(f"\nOutput files in: {output_dir.absolute()}")
    print("\nComponent summary:")
    print("   Power input: J1, F1, D1, C1-C2")
    print("   Buck (24V→5V): U1, L1, D2, C3-C4")
    print("   LDO (5V→3.3V): U2, C5-C6")
    print("   MCU: C7-C9, Y1 (C10-C11)")
    print("   Gate driver: C12-C16 (bootstrap/bypass)")
    print("   Power stage: Q1-Q6, R10-R12 (shunts)")
    print("   Connectors: J1-J4")
    print("   LEDs: D3-D4, R3-R4")
    print(f"\n   Total: ~{stats['symbol_count']} components")
    print("\nNext steps:")
    print("  1. Run ERC check")
    print("  2. Create PCB layout")
    print("  3. Run thermal analysis on power stage")
    print("  4. Add copper pour zones")
    print("  5. Route with appropriate trace widths")


def main() -> int:
    """Main entry point."""
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        create_bldc_controller(output_dir)
        return 0
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
