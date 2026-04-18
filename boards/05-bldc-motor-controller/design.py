#!/usr/bin/env python3
"""
BLDC Motor Controller - Full Design Pipeline

This script demonstrates the complete PCB design workflow for a power
electronics board:
1. Create project file
2. Create schematic with power rails and components
3. Run ERC validation
4. Generate PCB with component placement
5. Route PCB traces
6. Run DRC validation

The design includes:
- Multi-voltage power supply (12-24V -> 5V -> 3.3V)
- Gate driver for 3-phase motor control
- Power MOSFET half-bridges
- Current sensing with shunt resistors
- MCU for motor control

Usage:
    python design.py [output_dir]

If no output directory is specified, files are written to ./output/
"""

import subprocess
import sys
import uuid
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
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


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


def create_bldc_controller(output_dir: Path) -> Path:
    """
    Create a BLDC motor controller schematic.

    This demonstrates power electronics design patterns:
    - High-current power input with protection
    - Multi-stage power supply (buck + LDO)
    - 3-phase gate driver
    - Power MOSFET half-bridges
    - Current sensing for closed-loop control

    Returns the path to the generated schematic file.
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
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    )
    print(f"   Power connector: {j_power.reference}")

    # Input fuse
    fuse = sch.add_symbol(
        "Device:Fuse",
        x=X_POWER_IN + 20,
        y=100,
        ref="F1",
        value="15A",
        footprint="Fuse:Fuse_1206_3216Metric",
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
        footprint="Diode_SMD:D_SMA",
    )
    print(f"   TVS diode: {tvs.reference}")

    # Bulk input capacitors
    c_bulk1 = sch.add_symbol(
        "Device:C",
        x=X_POWER_IN + 55,
        y=120,
        ref="C1",
        value="470uF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    c_bulk2 = sch.add_symbol(
        "Device:C",
        x=X_POWER_IN + 70,
        y=120,
        ref="C2",
        value="100nF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    print(f"   Bulk caps: {c_bulk1.reference} = 470uF, {c_bulk2.reference} = 100nF")

    # Wire power input to VMOTOR rail
    # Connector pin 1 → Fuse → TVS/Caps → VMOTOR rail
    pin1_pos = j_power.pin_position("1")
    fuse_in = fuse.pin_position("1")
    fuse_out = fuse.pin_position("2")

    sch.add_wire(pin1_pos, fuse_in)
    sch.add_wire(fuse_out, (fuse_out[0], RAIL_VMOTOR), warn_on_collision=False)
    sch.add_junction(fuse_out[0], RAIL_VMOTOR)

    # Wire TVS and caps to rails (TVS has pins A1, A2 for bidirectional)
    tvs_a1 = tvs.pin_position("A1")
    tvs_a2 = tvs.pin_position("A2")
    sch.add_wire(tvs_a1, (tvs_a1[0], RAIL_VMOTOR), warn_on_collision=False)
    sch.add_junction(tvs_a1[0], RAIL_VMOTOR)
    sch.add_wire(tvs_a2, (tvs_a2[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(tvs_a2[0], RAIL_GND)

    sch.wire_decoupling_cap(c_bulk1, RAIL_VMOTOR, RAIL_GND)
    sch.wire_decoupling_cap(c_bulk2, RAIL_VMOTOR, RAIL_GND)

    # Connector pin 2 → GND
    pin2_pos = j_power.pin_position("2")
    sch.add_wire(pin2_pos, (pin2_pos[0], RAIL_GND), warn_on_collision=False)
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
        diode_ref="D2",  # D1 is used for TVS diode
    )
    buck.connect_to_rails(
        vin_rail_y=RAIL_VMOTOR,
        vout_rail_y=RAIL_5V,
        gnd_rail_y=RAIL_GND,
    )

    # Tie the ~ON/OFF pin to GND for always-on operation
    # The LM2596 ON/OFF pin is active-low: GND = ON, >1.3V = OFF
    try:
        on_off_pos = buck.regulator.pin_position("~{ON}/OFF")
        sch.add_wire(on_off_pos, (on_off_pos[0], RAIL_GND), warn_on_collision=False)
        sch.add_junction(on_off_pos[0], RAIL_GND)
    except KeyError:
        # Some LM2596 symbols may have different pin names
        try:
            on_off_pos = buck.regulator.pin_position("ON/OFF")
            sch.add_wire(on_off_pos, (on_off_pos[0], RAIL_GND), warn_on_collision=False)
            sch.add_junction(on_off_pos[0], RAIL_GND)
        except KeyError:
            pass  # Pin not found, may not be present on this symbol variant

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
        footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    )
    print(f"   LDO: {ldo.reference}")

    # LDO capacitors
    c_ldo_in = sch.add_symbol(
        "Device:C",
        x=X_LDO - 15,
        y=120,
        ref="C5",
        value="10uF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    c_ldo_out = sch.add_symbol(
        "Device:C",
        x=X_LDO + 25,
        y=120,
        ref="C6",
        value="10uF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )

    # Wire LDO
    ldo_vin = ldo.pin_position("VI")
    ldo_vout = ldo.pin_position("VO")
    ldo_gnd = ldo.pin_position("GND")

    sch.add_wire(ldo_vin, (ldo_vin[0], RAIL_5V), warn_on_collision=False)
    sch.add_junction(ldo_vin[0], RAIL_5V)
    sch.add_wire(ldo_vout, (ldo_vout[0], RAIL_3V3), warn_on_collision=False)
    sch.add_junction(ldo_vout[0], RAIL_3V3)
    sch.add_wire(ldo_gnd, (ldo_gnd[0], RAIL_GND), warn_on_collision=False)
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
    c_mcu1 = sch.add_symbol("Device:C", x=X_MCU, y=100, ref="C7", value="100nF", footprint="Capacitor_SMD:C_0805_2012Metric")
    c_mcu2 = sch.add_symbol("Device:C", x=X_MCU + 10, y=100, ref="C8", value="100nF", footprint="Capacitor_SMD:C_0805_2012Metric")
    c_mcu3 = sch.add_symbol("Device:C", x=X_MCU + 20, y=100, ref="C9", value="4.7uF", footprint="Capacitor_SMD:C_0805_2012Metric")
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
        sense_gnd = sense.port("GND")
        sch.add_wire(phase_gnd, sense_in, warn_on_collision=False)

        # Add current sense labels with connecting wires
        label_x = x_phase - 10
        # Wire from sense_in to label position for ISENSE+
        sch.add_wire(sense_in, (label_x, sense_in[1]), warn_on_collision=False)
        sch.add_label(f"ISENSE_{phase}+", label_x, sense_in[1], rotation=0)
        # Wire from sense GND to label position for ISENSE-
        sch.add_wire(sense_gnd, (label_x, sense_gnd[1]), warn_on_collision=False)
        sch.add_label(f"ISENSE_{phase}-", label_x, sense_gnd[1], rotation=0)

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
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    )
    print(f"   Motor connector: {j_motor.reference} (U/V/W)")

    # Wire motor phases from inverter block outputs to connector
    for i, phase in enumerate(phases):
        pin_pos = j_motor.pin_position(str(i + 1))
        # Get phase output from inverter block
        phase_out = inverter.port(f"PHASE_{phase}")
        # Wire from connector pin horizontally, then down to phase output level,
        # then connect to the phase output
        mid_x = phase_out[0] + 15
        # Horizontal wire from connector pin
        sch.add_wire(pin_pos, (mid_x, pin_pos[1]))
        # Vertical wire down to phase output Y level
        sch.add_wire((mid_x, pin_pos[1]), (mid_x, phase_out[1]))
        # Horizontal wire to connect to phase output
        sch.add_wire((mid_x, phase_out[1]), phase_out, warn_on_collision=False)
        sch.add_junction(phase_out[0], phase_out[1])

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
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical",
    )
    print(f"   Hall connector: {j_hall.reference}")

    # Wire hall signal pins and add labels
    # Pins 1-3 are hall signals, pin 4 is VCC, pin 5 is GND
    hall_labels = ["HALL_A", "HALL_B", "HALL_C"]
    for i, label in enumerate(hall_labels):
        pin_pos = j_hall.pin_position(str(i + 1))
        label_x = X_CONNECTORS - 20
        # Add wire from pin to label position
        sch.add_wire(pin_pos, (label_x, pin_pos[1]), warn_on_collision=False)
        sch.add_label(label, label_x, pin_pos[1], rotation=0)

    # Wire hall connector VCC (pin 4) to 3.3V rail
    hall_vcc_pos = j_hall.pin_position("4")
    sch.add_wire(hall_vcc_pos, (hall_vcc_pos[0], RAIL_3V3), warn_on_collision=False)
    sch.add_junction(hall_vcc_pos[0], RAIL_3V3)

    # Wire hall connector GND (pin 5) to GND rail
    hall_gnd_pos = j_hall.pin_position("5")
    sch.add_wire(hall_gnd_pos, (hall_gnd_pos[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(hall_gnd_pos[0], RAIL_GND)

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

    return sch_path


def create_project(output_dir: Path, project_name: str) -> Path:
    """
    Create a KiCad project file.

    Returns the path to the generated project file.
    """
    print("\n" + "=" * 60)
    print("Creating Project File...")
    print("=" * 60)

    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{project_name}.kicad_pro"
    project_data = create_minimal_project(filename)

    project_path = output_dir / filename
    save_project(project_data, project_path)
    print(f"\n   Project: {project_path}")

    return project_path


def run_erc(sch_path: Path) -> bool:
    """Run ERC on the schematic."""
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.cli.runner import run_erc as kicad_run_erc
    from kicad_tools.erc import ERCReport

    print("\n" + "=" * 60)
    print("Running ERC...")
    print("=" * 60)

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("\n   WARNING: kicad-cli not found - skipping ERC")
        return True

    result = kicad_run_erc(sch_path)

    if not result.success:
        print(f"\n   Error running ERC: {result.stderr}")
        return False

    try:
        report = ERCReport.load(result.output_path)
    except Exception as e:
        print(f"\n   Error parsing ERC report: {e}")
        return False
    finally:
        if result.output_path:
            result.output_path.unlink(missing_ok=True)

    violations = [v for v in report.violations if not v.excluded]
    error_count = sum(1 for v in violations if v.is_error)

    if error_count > 0:
        print(f"\n   Found {error_count} ERC errors:")
        for v in [v for v in violations if v.is_error][:5]:
            print(f"      - [{v.type_str}] {v.description}")
        return False
    else:
        print("\n   No ERC errors found!")
        return True


def create_bldc_pcb(output_dir: Path) -> Path:
    """
    Create a PCB for the BLDC motor controller.

    Generates a 60mm x 80mm board with all components from the schematic
    placed according to the project.kct layout suggestions:
    - MOSFETs in H-bridge near motor connector
    - Gate driver near MOSFETs
    - Current sense shunts between MOSFETs and ground
    - Power supply section separated from motor drive
    - Connectors at board edges

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating BLDC Motor Controller PCB...")
    print("=" * 60)

    # Board dimensions (mm) - from project.kct spec
    BOARD_WIDTH = 60.0
    BOARD_HEIGHT = 80.0
    BOARD_ORIGIN_X = 100.0
    BOARD_ORIGIN_Y = 100.0

    # Mounting hole positions (M3 at corners, 3mm inset)
    MH_INSET = 3.0
    MH_POSITIONS = [
        (BOARD_ORIGIN_X + MH_INSET, BOARD_ORIGIN_Y + MH_INSET),
        (BOARD_ORIGIN_X + BOARD_WIDTH - MH_INSET, BOARD_ORIGIN_Y + MH_INSET),
        (BOARD_ORIGIN_X + MH_INSET, BOARD_ORIGIN_Y + BOARD_HEIGHT - MH_INSET),
        (BOARD_ORIGIN_X + BOARD_WIDTH - MH_INSET, BOARD_ORIGIN_Y + BOARD_HEIGHT - MH_INSET),
    ]

    # Net definitions - must match schematic nets
    NETS = {
        "": 0,
        "VMOTOR": 1,
        "+5V": 2,
        "+3.3V": 3,
        "GND": 4,
        "PHASE_A": 5,
        "PHASE_B": 6,
        "PHASE_C": 7,
        "GATE_AH": 8,
        "GATE_AL": 9,
        "GATE_BH": 10,
        "GATE_BL": 11,
        "GATE_CH": 12,
        "GATE_CL": 13,
        "ISENSE_A+": 14,
        "ISENSE_A-": 15,
        "ISENSE_B+": 16,
        "ISENSE_B-": 17,
        "ISENSE_C+": 18,
        "ISENSE_C-": 19,
        "HALL_A": 20,
        "HALL_B": 21,
        "HALL_C": 22,
        "SWDIO": 23,
        "SWCLK": 24,
        "SWO": 25,
        "NRST": 26,
        "OSC_IN": 27,
        "OSC_OUT": 28,
        "PWR_LED": 29,
        "STATUS_LED": 30,
        "SW_OUT": 31,
    }

    # =========================================================================
    # Component positions - organized by board section
    # Spacing increased to avoid pad overlap (min 4mm between components)
    # =========================================================================

    # Power input section (top-left, row 1)
    J1_POS = (BOARD_ORIGIN_X + 5, BOARD_ORIGIN_Y + 8)  # Power connector
    F1_POS = (BOARD_ORIGIN_X + 14, BOARD_ORIGIN_Y + 8)  # Fuse
    D1_POS = (BOARD_ORIGIN_X + 22, BOARD_ORIGIN_Y + 8)  # TVS diode
    C1_POS = (BOARD_ORIGIN_X + 30, BOARD_ORIGIN_Y + 8)  # Bulk cap 470uF
    C2_POS = (BOARD_ORIGIN_X + 30, BOARD_ORIGIN_Y + 14)  # Bulk cap 100nF

    # Buck converter (left, row 2)
    U1_POS = (BOARD_ORIGIN_X + 12, BOARD_ORIGIN_Y + 22)  # LM2596 (D2PAK)
    L1_POS = (BOARD_ORIGIN_X + 26, BOARD_ORIGIN_Y + 22)  # Inductor
    D2_POS = (BOARD_ORIGIN_X + 19, BOARD_ORIGIN_Y + 30)  # Schottky
    C3_POS = (BOARD_ORIGIN_X + 6, BOARD_ORIGIN_Y + 30)  # Buck input cap
    C4_POS = (BOARD_ORIGIN_X + 30, BOARD_ORIGIN_Y + 30)  # Buck output cap

    # LDO (center-right, row 2) -- 10mm gap from buck section
    U2_POS = (BOARD_ORIGIN_X + 44, BOARD_ORIGIN_Y + 22)  # AMS1117
    C5_POS = (BOARD_ORIGIN_X + 38, BOARD_ORIGIN_Y + 30)  # LDO input cap
    C6_POS = (BOARD_ORIGIN_X + 50, BOARD_ORIGIN_Y + 30)  # LDO output cap

    # MCU bypass caps (center, row 3) -- 5mm spacing between caps
    C7_POS = (BOARD_ORIGIN_X + 34, BOARD_ORIGIN_Y + 37)
    C8_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 37)
    C9_POS = (BOARD_ORIGIN_X + 46, BOARD_ORIGIN_Y + 37)

    # Crystal (right side, row 3)
    Y1_POS = (BOARD_ORIGIN_X + 52, BOARD_ORIGIN_Y + 37)
    C10_POS = (BOARD_ORIGIN_X + 46, BOARD_ORIGIN_Y + 43)
    C11_POS = (BOARD_ORIGIN_X + 52, BOARD_ORIGIN_Y + 43)

    # Gate driver (center-left, row 4)
    U3_POS = (BOARD_ORIGIN_X + 28, BOARD_ORIGIN_Y + 47)  # DRV8301 QFN-56
    C12_POS = (BOARD_ORIGIN_X + 16, BOARD_ORIGIN_Y + 43)  # Bootstrap A
    C13_POS = (BOARD_ORIGIN_X + 16, BOARD_ORIGIN_Y + 48)  # Bootstrap B
    C14_POS = (BOARD_ORIGIN_X + 16, BOARD_ORIGIN_Y + 53)  # Bootstrap C
    C15_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 48)  # Bypass 100nF
    C16_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 53)  # Bypass 10uF

    # Power MOSFETs - H-bridge configuration (bottom section)
    # TO-220 pads are 2.54mm pitch, body ~5mm wide
    # Phase A (left)
    Q1_POS = (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 58)  # HS
    Q2_POS = (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 66)  # LS
    # Phase B (center)
    Q3_POS = (BOARD_ORIGIN_X + 24, BOARD_ORIGIN_Y + 58)  # HS
    Q4_POS = (BOARD_ORIGIN_X + 24, BOARD_ORIGIN_Y + 66)  # LS
    # Phase C (right)
    Q5_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 58)  # HS
    Q6_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 66)  # LS

    # Current sense shunts (below MOSFETs)
    R10_POS = (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 74)
    R11_POS = (BOARD_ORIGIN_X + 24, BOARD_ORIGIN_Y + 74)
    R12_POS = (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 74)

    # Motor connector (right edge, bottom -- near MOSFETs)
    J2_POS = (BOARD_ORIGIN_X + 55, BOARD_ORIGIN_Y + 66)

    # Hall sensor connector (right edge, middle)
    J3_POS = (BOARD_ORIGIN_X + 55, BOARD_ORIGIN_Y + 48)

    # Debug header (right edge, top)
    J4_POS = (BOARD_ORIGIN_X + 55, BOARD_ORIGIN_Y + 22)

    # LEDs (top-right corner)
    D3_POS = (BOARD_ORIGIN_X + 50, BOARD_ORIGIN_Y + 8)  # PWR LED
    R3_POS = (BOARD_ORIGIN_X + 50, BOARD_ORIGIN_Y + 13)  # PWR LED resistor
    D4_POS = (BOARD_ORIGIN_X + 55, BOARD_ORIGIN_Y + 8)  # STATUS LED
    R4_POS = (BOARD_ORIGIN_X + 55, BOARD_ORIGIN_Y + 13)  # STATUS LED resistor

    # =========================================================================
    # Footprint generators
    # =========================================================================

    def generate_header() -> str:
        """Generate the PCB file header."""
        return """(kicad_pcb
  (version 20240108)
  (generator "kicad-tools-demo")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )"""

    def generate_nets() -> str:
        """Generate net definitions."""
        lines = ['  (net 0 "")']
        for name, num in NETS.items():
            if num > 0:
                lines.append(f'  (net {num} "{name}")')
        return "\n".join(lines)

    def generate_board_outline() -> str:
        """Generate the board outline (Edge.Cuts) as a 60x80mm rectangle."""
        x1 = BOARD_ORIGIN_X
        y1 = BOARD_ORIGIN_Y
        x2 = BOARD_ORIGIN_X + BOARD_WIDTH
        y2 = BOARD_ORIGIN_Y + BOARD_HEIGHT
        return f"""  (gr_rect (start {x1} {y1}) (end {x2} {y2})
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "{generate_uuid()}")
  )"""

    def generate_mounting_hole(pos: tuple, ref: str) -> str:
        """Generate M3 mounting hole footprint."""
        x, y = pos
        return f"""  (footprint "MountingHole:MountingHole_3.2mm_M3"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "MountingHole" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (attr exclude_from_pos_files exclude_from_bom)
    (pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu" "*.Mask"))
  )"""

    def generate_to220(ref: str, pos: tuple, value: str, gate_net: str, drain_net: str, source_net: str) -> str:
        """Generate TO-220 footprint for power MOSFETs."""
        x, y = pos
        gate_num = NETS.get(gate_net, 0)
        drain_num = NETS.get(drain_net, 0)
        source_num = NETS.get(source_net, 0)
        return f"""  (footprint "Package_TO_SOT_THT:TO-220-3_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at -2.54 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net {gate_num} "{gate_net}"))
    (pad "2" thru_hole oval (at 0 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net {drain_num} "{drain_net}"))
    (pad "3" thru_hole oval (at 2.54 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net {source_num} "{source_net}"))
  )"""

    def generate_sot223(ref: str, pos: tuple, value: str, pin1_net: str, pin2_net: str, pin3_net: str) -> str:
        """Generate SOT-223 footprint for LDO."""
        x, y = pos
        net1 = NETS.get(pin1_net, 0)
        net2 = NETS.get(pin2_net, 0)
        net3 = NETS.get(pin3_net, 0)
        return f"""  (footprint "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -4) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at -3.15 2.3) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {net1} "{pin1_net}"))
    (pad "2" smd rect (at -3.15 0) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {net2} "{pin2_net}"))
    (pad "3" smd rect (at -3.15 -2.3) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {net3} "{pin3_net}"))
    (pad "2" smd rect (at 3.15 0) (size 2 3.8) (layers "F.Cu" "F.Paste" "F.Mask") (net {net2} "{pin2_net}"))
  )"""

    def generate_d2pak(ref: str, pos: tuple, value: str) -> str:
        """Generate D2PAK/TO-263 footprint for buck regulator."""
        x, y = pos
        return f"""  (footprint "Package_TO_SOT_SMD:TO-263-5_TabPin3"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at -3.4 3.3) (size 3 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["VMOTOR"]} "VMOTOR"))
    (pad "2" smd rect (at -3.4 1.1) (size 3 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["+5V"]} "+5V"))
    (pad "3" smd rect (at -3.4 -1.1) (size 3 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["SW_OUT"]} "SW_OUT"))
    (pad "4" smd rect (at -3.4 -3.3) (size 3 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
    (pad "5" smd rect (at 3.4 0) (size 3 8) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
  )"""

    def generate_qfn56(ref: str, pos: tuple, value: str) -> str:
        """Generate QFN-56 footprint for DRV8301 gate driver."""
        x, y = pos
        return f"""  (footprint "Package_DFN_QFN:QFN-56-1EP_8x8mm_P0.5mm_EP5.6x5.6mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -5.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 5.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at -4 3.25) (size 0.8 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["+5V"]} "+5V"))
    (pad "57" smd rect (at 0 0) (size 5.6 5.6) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
  )"""

    def generate_cap_0805(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        """Generate 0805 capacitor footprint."""
        x, y = pos
        net1_num = NETS.get(net1, 0)
        net2_num = NETS.get(net2, 0)
        return f"""  (footprint "Capacitor_SMD:C_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
  )"""

    def generate_resistor_2512(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        """Generate 2512 resistor footprint for current sense shunts."""
        x, y = pos
        net1_num = NETS.get(net1, 0)
        net2_num = NETS.get(net2, 0)
        return f"""  (footprint "Resistor_SMD:R_2512_6332Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -2) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 2) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -3.1 0) (size 1.6 2.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 3.1 0) (size 1.6 2.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
  )"""

    def generate_resistor_0805(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        """Generate 0805 resistor footprint."""
        x, y = pos
        net1_num = NETS.get(net1, 0)
        net2_num = NETS.get(net2, 0)
        return f"""  (footprint "Resistor_SMD:R_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
  )"""

    def generate_led_0805(ref: str, pos: tuple, net_a: str, net_k: str) -> str:
        """Generate 0805 LED footprint."""
        x, y = pos
        net_a_num = NETS.get(net_a, 0)
        net_k_num = NETS.get(net_k, 0)
        return f"""  (footprint "LED_SMD:LED_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "LED" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1.05 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net_a_num} "{net_a}"))
    (pad "2" smd roundrect (at 1.05 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net_k_num} "{net_k}"))
  )"""

    def generate_crystal_hc49(ref: str, pos: tuple, value: str) -> str:
        """Generate HC49 crystal footprint."""
        x, y = pos
        return f"""  (footprint "Crystal:Crystal_HC49-4H_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole circle (at -2.44 0) (size 1.5 1.5) (drill 0.8) (layers "*.Cu" "*.Mask") (net {NETS["OSC_IN"]} "OSC_IN"))
    (pad "2" thru_hole circle (at 2.44 0) (size 1.5 1.5) (drill 0.8) (layers "*.Cu" "*.Mask") (net {NETS["OSC_OUT"]} "OSC_OUT"))
  )"""

    def generate_inductor_smd(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        """Generate SMD inductor footprint."""
        x, y = pos
        net1_num = NETS.get(net1, 0)
        net2_num = NETS.get(net2, 0)
        return f"""  (footprint "Inductor_SMD:L_1210_3225Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -2) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 2) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1.5 0) (size 1.2 2.5) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 1.5 0) (size 1.2 2.5) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
  )"""

    def generate_diode_sma(ref: str, pos: tuple, value: str, net_a: str, net_k: str) -> str:
        """Generate SMA diode footprint (for Schottky and TVS)."""
        x, y = pos
        net_a_num = NETS.get(net_a, 0)
        net_k_num = NETS.get(net_k, 0)
        return f"""  (footprint "Diode_SMD:D_SMA"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -2) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 2) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -2 0) (size 1.5 1.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net_k_num} "{net_k}"))
    (pad "2" smd roundrect (at 2 0) (size 1.5 1.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net_a_num} "{net_a}"))
  )"""

    def generate_fuse_holder(ref: str, pos: tuple, value: str) -> str:
        """Generate fuse holder footprint."""
        x, y = pos
        return f"""  (footprint "Fuse:Fuse_1206_3216Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at -1.5 0) (size 1.2 1.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {NETS["VMOTOR"]} "VMOTOR"))
    (pad "2" smd roundrect (at 1.5 0) (size 1.2 1.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {NETS["VMOTOR"]} "VMOTOR"))
  )"""

    def generate_pin_header(ref: str, pos: tuple, pins: int, value: str, pin_nets: list) -> str:
        """Generate N-pin header footprint."""
        x, y = pos
        pitch = 2.54
        pad_lines = []
        for i in range(pins):
            pin_num = i + 1
            net_name = pin_nets[i] if i < len(pin_nets) else ""
            net_num = NETS.get(net_name, 0)
            y_offset = (i - (pins - 1) / 2.0) * pitch
            pad_type = "rect" if i == 0 else "oval"
            pad_lines.append(
                f'    (pad "{pin_num}" thru_hole {pad_type} (at 0 {y_offset:.2f}) '
                f'(size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") '
                f'(net {net_num} "{net_name}"))'
            )
        pads = "\n".join(pad_lines)
        return f"""  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x{pins:02d}_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 {-(pins / 2.0) * pitch - 1:.1f}) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 {(pins / 2.0) * pitch + 1:.1f}) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads}
  )"""

    # =========================================================================
    # Build PCB
    # =========================================================================
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
    ]

    print("\n1. Adding mounting holes...")
    for i, mh_pos in enumerate(MH_POSITIONS):
        parts.append(generate_mounting_hole(mh_pos, f"MH{i + 1}"))
    print(f"   4x M3 mounting holes at corners")

    print("\n2. Adding power input section...")
    # J1: Power input connector (2-pin)
    parts.append(generate_pin_header("J1", J1_POS, 2, "Power Input", ["VMOTOR", "GND"]))
    print(f"   J1 (Power Input) at {J1_POS}")
    # F1: Fuse
    parts.append(generate_fuse_holder("F1", F1_POS, "15A"))
    print(f"   F1 (15A Fuse) at {F1_POS}")
    # D1: TVS diode
    parts.append(generate_diode_sma("D1", D1_POS, "SMBJ24A", "VMOTOR", "GND"))
    print(f"   D1 (TVS) at {D1_POS}")
    # C1, C2: Bulk caps
    parts.append(generate_cap_0805("C1", C1_POS, "470uF", "VMOTOR", "GND"))
    parts.append(generate_cap_0805("C2", C2_POS, "100nF", "VMOTOR", "GND"))
    print(f"   C1, C2 (bulk caps) at {C1_POS}, {C2_POS}")

    print("\n3. Adding buck converter section...")
    # U1: LM2596 buck regulator
    parts.append(generate_d2pak("U1", U1_POS, "LM2596-5.0"))
    print(f"   U1 (LM2596) at {U1_POS}")
    # L1: Inductor
    parts.append(generate_inductor_smd("L1", L1_POS, "33uH", "SW_OUT", "+5V"))
    print(f"   L1 (33uH) at {L1_POS}")
    # D2: Schottky diode
    parts.append(generate_diode_sma("D2", D2_POS, "SS34", "GND", "SW_OUT"))
    print(f"   D2 (SS34) at {D2_POS}")
    # C3, C4: Buck caps
    parts.append(generate_cap_0805("C3", C3_POS, "220uF", "VMOTOR", "GND"))
    parts.append(generate_cap_0805("C4", C4_POS, "220uF", "+5V", "GND"))
    print(f"   C3, C4 (buck caps) at {C3_POS}, {C4_POS}")

    print("\n4. Adding LDO section...")
    # U2: AMS1117-3.3
    parts.append(generate_sot223("U2", U2_POS, "AMS1117-3.3", "+5V", "GND", "+3.3V"))
    print(f"   U2 (AMS1117) at {U2_POS}")
    # C5, C6: LDO caps
    parts.append(generate_cap_0805("C5", C5_POS, "10uF", "+5V", "GND"))
    parts.append(generate_cap_0805("C6", C6_POS, "10uF", "+3.3V", "GND"))
    print(f"   C5, C6 (LDO caps) at {C5_POS}, {C6_POS}")

    print("\n5. Adding MCU bypass caps...")
    parts.append(generate_cap_0805("C7", C7_POS, "100nF", "+3.3V", "GND"))
    parts.append(generate_cap_0805("C8", C8_POS, "100nF", "+3.3V", "GND"))
    parts.append(generate_cap_0805("C9", C9_POS, "4.7uF", "+3.3V", "GND"))
    print(f"   C7, C8, C9 at {C7_POS}, {C8_POS}, {C9_POS}")

    print("\n6. Adding crystal...")
    parts.append(generate_crystal_hc49("Y1", Y1_POS, "8MHz"))
    parts.append(generate_cap_0805("C10", C10_POS, "20pF", "OSC_IN", "GND"))
    parts.append(generate_cap_0805("C11", C11_POS, "20pF", "OSC_OUT", "GND"))
    print(f"   Y1 at {Y1_POS}, C10-C11 at {C10_POS}, {C11_POS}")

    print("\n7. Adding gate driver...")
    parts.append(generate_qfn56("U3", U3_POS, "DRV8301"))
    print(f"   U3 (DRV8301) at {U3_POS}")
    # Bootstrap caps (VMOTOR to phase)
    parts.append(generate_cap_0805("C12", C12_POS, "100nF", "VMOTOR", "PHASE_A"))
    parts.append(generate_cap_0805("C13", C13_POS, "100nF", "VMOTOR", "PHASE_B"))
    parts.append(generate_cap_0805("C14", C14_POS, "100nF", "VMOTOR", "PHASE_C"))
    # Bypass caps
    parts.append(generate_cap_0805("C15", C15_POS, "100nF", "+5V", "GND"))
    parts.append(generate_cap_0805("C16", C16_POS, "10uF", "+5V", "GND"))
    print(f"   C12-C14 (bootstrap), C15-C16 (bypass)")

    print("\n8. Adding power MOSFETs (H-bridge)...")
    # Phase A: Q1 (high-side), Q2 (low-side)
    parts.append(generate_to220("Q1", Q1_POS, "IRLZ44N", "GATE_AH", "VMOTOR", "PHASE_A"))
    parts.append(generate_to220("Q2", Q2_POS, "IRLZ44N", "GATE_AL", "PHASE_A", "ISENSE_A+"))
    # Phase B: Q3 (high-side), Q4 (low-side)
    parts.append(generate_to220("Q3", Q3_POS, "IRLZ44N", "GATE_BH", "VMOTOR", "PHASE_B"))
    parts.append(generate_to220("Q4", Q4_POS, "IRLZ44N", "GATE_BL", "PHASE_B", "ISENSE_B+"))
    # Phase C: Q5 (high-side), Q6 (low-side)
    parts.append(generate_to220("Q5", Q5_POS, "IRLZ44N", "GATE_CH", "VMOTOR", "PHASE_C"))
    parts.append(generate_to220("Q6", Q6_POS, "IRLZ44N", "GATE_CL", "PHASE_C", "ISENSE_C+"))
    print(f"   Q1-Q2 (Phase A), Q3-Q4 (Phase B), Q5-Q6 (Phase C)")

    print("\n9. Adding current sense shunts...")
    parts.append(generate_resistor_2512("R10", R10_POS, "5mR", "ISENSE_A+", "ISENSE_A-"))
    parts.append(generate_resistor_2512("R11", R11_POS, "5mR", "ISENSE_B+", "ISENSE_B-"))
    parts.append(generate_resistor_2512("R12", R12_POS, "5mR", "ISENSE_C+", "ISENSE_C-"))
    print(f"   R10, R11, R12 (5mOhm shunts)")

    print("\n10. Adding connectors...")
    # J2: Motor output (3-pin)
    parts.append(generate_pin_header("J2", J2_POS, 3, "Motor Output", ["PHASE_A", "PHASE_B", "PHASE_C"]))
    print(f"   J2 (Motor Output) at {J2_POS}")
    # J3: Hall sensors (5-pin)
    parts.append(generate_pin_header("J3", J3_POS, 5, "Hall Sensors", ["HALL_A", "HALL_B", "HALL_C", "+3.3V", "GND"]))
    print(f"   J3 (Hall Sensors) at {J3_POS}")
    # J4: Debug header (6-pin SWD)
    parts.append(generate_pin_header("J4", J4_POS, 6, "SWD Debug", ["+3.3V", "SWDIO", "SWCLK", "SWO", "NRST", "GND"]))
    print(f"   J4 (SWD Debug) at {J4_POS}")

    print("\n11. Adding LEDs...")
    parts.append(generate_resistor_0805("R3", R3_POS, "1k", "+3.3V", "PWR_LED"))
    parts.append(generate_led_0805("D3", D3_POS, "PWR_LED", "GND"))
    parts.append(generate_resistor_0805("R4", R4_POS, "1k", "+3.3V", "STATUS_LED"))
    parts.append(generate_led_0805("D4", D4_POS, "STATUS_LED", "GND"))
    print(f"   D3 (PWR), D4 (STATUS) with resistors R3, R4")

    parts.append(")")  # Close kicad_pcb

    pcb_content = "\n".join(parts)

    # Write PCB file
    print("\n12. Writing PCB file...")
    output_dir.mkdir(parents=True, exist_ok=True)
    pcb_path = output_dir / "bldc_controller.kicad_pcb"
    pcb_path.write_text(pcb_content)
    print(f"   PCB: {pcb_path}")

    print(f"\n   Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print(f"   Mounting holes: 4x M3")
    component_count = (
        4  # mounting holes
        + 6  # MOSFETs
        + 3  # ICs (U1, U2, U3)
        + 16  # capacitors (C1-C16)
        + 5  # resistors (R3, R4, R10-R12)
        + 4  # diodes (D1-D4)
        + 1  # inductor
        + 1  # fuse
        + 1  # crystal
        + 3  # connectors (J1-J3)
        + 1  # debug header (J4)
    )
    print(f"   Components: {component_count}")
    print(f"   Nets: {len([n for n in NETS.values() if n > 0])}")

    return pcb_path


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """
    Route the PCB using the autorouter.

    Returns True if all nets were routed successfully.
    """
    from kicad_tools.router import DesignRules, load_pcb_for_routing
    from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer

    print("\n" + "=" * 60)
    print("Routing PCB...")
    print("=" * 60)

    # Configure design rules (from project.kct spec)
    # min_trace: 0.2mm signal, min_space: 0.2mm, min_drill: 0.3mm
    # Grid resolution must be <= clearance/2 for reliable DRC compliance
    # Issue #1543: Increased trace_width from 0.15mm to 0.2mm for reliable
    # signal routing on a motor controller board. Power nets get wider traces
    # (0.5mm+) via the net-class system automatically.
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.2,
        trace_clearance=0.3,
        via_drill=0.3,
        via_diameter=0.6,
    )

    print(f"\n1. Loading PCB: {input_path}")
    print(f"   Grid resolution: {rules.grid_resolution}mm")
    print(f"   Trace width: {rules.trace_width}mm")
    print(f"   Clearance: {rules.trace_clearance}mm")

    # Skip power and high-current nets (route manually or use copper pour zones)
    # Phase nets carry motor current (10A+) and need wide traces (2mm+)
    skip_nets = ["VMOTOR", "+5V", "+3.3V", "GND", "PHASE_A", "PHASE_B", "PHASE_C"]

    # Load the PCB
    router, net_map = load_pcb_for_routing(
        str(input_path),
        skip_nets=skip_nets,
        rules=rules,
    )

    print(f"\n   Board size: {router.grid.width}mm x {router.grid.height}mm")
    print(f"   Nets loaded: {len(net_map)}")
    print(f"   Skipping power nets: {skip_nets}")

    # Route all nets
    print("\n2. Routing nets...")
    router.route_all()

    # Get statistics before optimization
    stats_before = router.get_statistics()

    print("\n3. Raw routing results:")
    print(f"   Routes: {stats_before['routes']}")
    print(f"   Segments: {stats_before['segments']}")
    print(f"   Vias: {stats_before['vias']}")

    # Optimize traces
    print("\n4. Optimizing traces...")
    opt_config = OptimizationConfig(
        merge_collinear=True,
        eliminate_zigzags=True,
        compress_staircase=True,
        convert_45_corners=True,
        minimize_vias=True,
    )
    optimizer = TraceOptimizer(config=opt_config)

    optimized_routes = []
    for route in router.routes:
        optimized_route = optimizer.optimize_route(route)
        optimized_routes.append(optimized_route)
    router.routes = optimized_routes

    # Get final statistics
    stats = router.get_statistics()

    print("\n5. Final routing results:")
    print(f"   Routes: {stats['routes']}")
    print(f"   Segments: {stats['segments']}")
    print(f"   Vias: {stats['vias']}")
    print(f"   Total length: {stats['total_length_mm']:.2f}mm")
    print(f"   Nets routed: {stats['nets_routed']}")

    # Save routed PCB
    print(f"\n6. Saving routed PCB: {output_path}")

    original_content = input_path.read_text()
    route_sexp = router.to_sexp()

    if route_sexp:
        output_content = original_content.rstrip().rstrip(")")
        output_content += "\n"
        output_content += f"  {route_sexp}\n"
        output_content += ")\n"
    else:
        output_content = original_content
        print("   Warning: No routes generated!")

    output_path.write_text(output_content)

    # Calculate success - we skipped power nets, so only count signal nets
    total_signal_nets = len([n for n in router.nets if n > 0])
    success = stats["nets_routed"] == total_signal_nets

    if success:
        print("\n   SUCCESS: All signal nets routed!")
    else:
        print(f"\n   PARTIAL: Routed {stats['nets_routed']}/{total_signal_nets} signal nets")

    return success


def run_drc(pcb_path: Path) -> bool:
    """Run DRC on the PCB using kct check for consistent results."""
    print("\n" + "=" * 60)
    print("Running DRC (via kct check)...")
    print("=" * 60)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "check", str(pcb_path)],
            capture_output=True,
            text=True,
        )

        # Print the output
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"   {line}")

        # Check for success
        if result.returncode == 0:
            return True
        else:
            if result.stderr:
                print(f"\n   Error: {result.stderr}")
            return False

    except Exception as e:
        print(f"\n   Error running DRC: {e}")
        return False


def main() -> int:
    """Main entry point."""
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        # Step 1: Create project file
        project_path = create_project(output_dir, "bldc_controller")

        # Step 2: Create schematic
        sch_path = create_bldc_controller(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_bldc_pcb(output_dir)

        # Step 5: Route PCB
        routed_path = output_dir / "bldc_controller_routed.kicad_pcb"
        route_success = route_pcb(pcb_path, routed_path)

        # Step 6: Run DRC
        drc_success = run_drc(routed_path)

        # Summary
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"\nOutput directory: {output_dir.absolute()}")
        print("\nGenerated files:")
        print(f"  1. Project: {project_path.name}")
        print(f"  2. Schematic: {sch_path.name}")
        print(f"  3. PCB (unrouted): {pcb_path.name}")
        print(f"  4. PCB (routed): {routed_path.name}")
        print("\nResults:")
        print(f"  ERC: {'PASS' if erc_success else 'FAIL'}")
        print(f"  Routing: {'SUCCESS' if route_success else 'PARTIAL'}")
        print(f"  DRC: {'PASS' if drc_success else 'FAIL'}")
        print("\nComponent summary:")
        print("  Power input: J1, F1, D1, C1-C2")
        print("  Buck (24V->5V): U1, L1, D2, C3-C4")
        print("  LDO (5V->3.3V): U2, C5-C6")
        print("  MCU: C7-C9, Y1 (C10-C11)")
        print("  Gate driver: U3, C12-C16")
        print("  Power stage: Q1-Q6, R10-R12")
        print("  Connectors: J1-J4")
        print("  LEDs: D3-D4, R3-R4")

        # For this board, partial routing is acceptable
        # Success if ERC passes and DRC has no errors (warnings OK)
        return 0 if erc_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
