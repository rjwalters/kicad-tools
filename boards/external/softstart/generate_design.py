#!/usr/bin/env python3
"""
Generator Soft-Start - Supercapacitor Power Assist

Enables a Honda EU1000i (1000W) generator to start an 8000 BTU window AC
by injecting supplemental current during the 300-500ms motor startup surge.

Design sections:
1. AC Power Input - fuse, varistor, terminal block for 120VAC
2. AC Voltage Sensing - resistor divider to scale mains for MCU ADC
3. Zero-Crossing Detection - H11AA1 optocoupler for AC phase detection
4. Supercap Charging - resistor-limited charging via bridge rectifier
5. Discharge Circuit - 2x IRFB4110 N-MOSFETs for supercap discharge
6. Current Sensing - 0.005 ohm shunt + INA180A1 current sense amp
7. MCU - STM32G031F6P6 (TSSOP-20) with decoupling, reset, boot, SWD
8. 3.3V Power Supply - LDO from rectified DC
9. Status LED - power/status indicator
10. Board - 150mm x 100mm, 2-layer, JLCPCB

Usage:
    python generate_design.py [output_dir]
"""

import subprocess
import sys
import uuid
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic.blocks import (
    DebugHeader,
    FuseBlock,
    LEDIndicator,
    VoltageDividerSense,
)
from kicad_tools.schematic.blocks.mcu import BootModeSelector, MCUBlock, ResetButton
from kicad_tools.schematic.models.schematic import Schematic

# Warn if running source scripts with stale pipx install
warn_if_stale()


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


def create_softstart_schematic(output_dir: Path) -> Path:
    """
    Create the soft-start board schematic.

    Returns the path to the generated schematic file.
    """
    print("Creating Generator Soft-Start Schematic...")
    print("=" * 60)

    sch = Schematic(
        title="Generator Soft-Start - Supercapacitor Power Assist",
        date="2025-01",
        revision="A",
        company="kicad-tools",
        comment1="120VAC soft-start for 8000 BTU AC on Honda EU1000i",
        comment2="STM32G031F6P6 MCU, 2x30S supercap banks",
    )

    # =========================================================================
    # Power Rail Y Coordinates
    # =========================================================================
    RAIL_3V3 = 30       # 3.3V logic supply
    RAIL_VRECT = 50     # Rectified DC (~5-12V from small transformer/supply)
    RAIL_GND = 280      # Ground

    # Schematic section X positions
    X_AC_INPUT = 25
    X_VSENSE = 80
    X_ZC_DETECT = 130
    X_CHARGE = 180
    X_DISCHARGE = 250
    X_ISENSE = 320
    X_MCU = 390
    X_LDO = 470
    X_LED = 530
    X_DEBUG = 560

    # =========================================================================
    # Section 1: Power Rails
    # =========================================================================
    print("\n1. Creating power rails...")

    # 3.3V rail
    sch.add_rail(RAIL_3V3, x_start=X_LDO - 10, x_end=X_DEBUG + 40, net_label="+3.3V")
    sch.add_power("power:+3V3", x=X_LDO - 10, y=RAIL_3V3 - 10, rotation=0)

    # Rectified DC rail (from small supply for LDO input)
    sch.add_rail(RAIL_VRECT, x_start=X_CHARGE - 10, x_end=X_LDO + 40, net_label="VRECT")

    # Ground rail (spans full width)
    sch.add_rail(RAIL_GND, x_start=X_AC_INPUT, x_end=X_DEBUG + 40, net_label="GND")
    sch.add_power("power:GND", x=X_AC_INPUT, y=RAIL_GND + 10, rotation=0)

    # PWR_FLAG for ERC
    sch.add_power("power:PWR_FLAG", x=X_AC_INPUT + 10, y=RAIL_GND, rotation=0)

    print("   Added +3.3V, VRECT, and GND rails")

    # =========================================================================
    # Section 2: AC Power Input (Fuse + Varistor + Terminal Block)
    # =========================================================================
    print("\n2. Adding AC power input section...")

    # AC input terminal block (2-pin: LINE, NEUTRAL)
    j_ac_in = sch.add_symbol(
        "Connector_Generic:Conn_01x02",
        x=X_AC_INPUT,
        y=100,
        ref="J1",
        value="AC_INPUT",
        footprint="TerminalBlock:TerminalBlock_bornier-2_P5.08mm",
    )
    print(f"   J1: AC input terminal block")

    # Fuse (15A for 120VAC line)
    fuse = FuseBlock(
        sch,
        x=X_AC_INPUT + 20,
        y=100,
        current_rating="15A",
        ref="F1",
    )
    print(f"   F1: 15A fuse")

    # Varistor (MOV for surge protection) - pins are "1" and "2"
    mov = sch.add_symbol(
        "Device:Varistor",
        x=X_AC_INPUT + 45,
        y=120,
        ref="RV1",
        value="275VAC",
        rotation=90,
        footprint="Varistor:RV_Disc_D12mm_W4.2mm_P7.5mm",
    )
    print(f"   RV1: Varistor 275VAC")

    # AC output terminal block (pass-through to load)
    j_ac_out = sch.add_symbol(
        "Connector_Generic:Conn_01x02",
        x=X_AC_INPUT + 60,
        y=100,
        ref="J2",
        value="AC_OUTPUT",
        footprint="TerminalBlock:TerminalBlock_bornier-2_P5.08mm",
    )
    print(f"   J2: AC output terminal block")

    # Wire AC input section
    j1_pin1 = j_ac_in.pin_position("1")  # LINE
    j1_pin2 = j_ac_in.pin_position("2")  # NEUTRAL
    fuse_in = fuse.port("IN")
    fuse_out = fuse.port("OUT")

    # J1 pin 1 (LINE) -> Fuse
    sch.add_wire(j1_pin1, fuse_in)

    # Fuse output -> J2 pin 1 (pass-through)
    j2_pin1 = j_ac_out.pin_position("1")
    sch.add_wire(fuse_out, j2_pin1)

    # Add AC_LINE label at fuse output
    sch.add_label("AC_LINE", fuse_out[0], fuse_out[1])

    # Varistor across fused LINE and NEUTRAL
    mov_pin1 = mov.pin_position("1")
    mov_pin2 = mov.pin_position("2")
    # Connect mov pin1 to fuse output (AC_LINE)
    sch.add_wire(mov_pin1, (fuse_out[0], mov_pin1[1]), warn_on_collision=False)
    sch.add_wire((fuse_out[0], mov_pin1[1]), fuse_out, warn_on_collision=False)

    # Neutral connections
    j2_pin2 = j_ac_out.pin_position("2")
    sch.add_wire(j1_pin2, j2_pin2)
    sch.add_label("AC_NEUTRAL", j1_pin2[0], j1_pin2[1])

    # Varistor pin 2 to neutral
    sch.add_wire(mov_pin2, (j1_pin2[0], mov_pin2[1]), warn_on_collision=False)
    sch.add_wire((j1_pin2[0], mov_pin2[1]), j1_pin2, warn_on_collision=False)
    sch.add_junction(j1_pin2[0], j1_pin2[1])

    # =========================================================================
    # Section 3: AC Voltage Sensing (Resistor Divider)
    # =========================================================================
    print("\n3. Adding AC voltage sensing...")

    # Voltage divider: 120VAC peak ~170V -> 3.3V ADC
    # ratio = 170V / 3.3V ~ 51.5
    vsense = VoltageDividerSense(
        sch,
        x=X_VSENSE,
        y=140,
        ratio=51.5,
        ref_start=1,
    )
    print(f"   R1/R2: Voltage divider (ratio 51.5:1)")

    # Wire voltage divider
    vsense_vin = vsense.port("VIN")
    vsense_vout = vsense.port("VOUT")
    vsense_gnd = vsense.port("GND")

    # Add a short wire stub at VIN so label is on a wire
    sch.add_wire(vsense_vin, (vsense_vin[0] - 5, vsense_vin[1]))
    sch.add_label("AC_LINE", vsense_vin[0] - 5, vsense_vin[1])

    # Add a short wire stub at VOUT so label is on a wire
    sch.add_wire(vsense_vout, (vsense_vout[0] + 5, vsense_vout[1]))
    sch.add_label("V_AC_SENSE", vsense_vout[0] + 5, vsense_vout[1])

    # Connect ground
    sch.add_wire(vsense_gnd, (vsense_gnd[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(vsense_gnd[0], RAIL_GND)

    # =========================================================================
    # Section 4: Zero-Crossing Detection (H11AA1 Optocoupler)
    # =========================================================================
    print("\n4. Adding zero-crossing detection...")

    # H11AA1 optocoupler (AC input opto)
    # Pins: 1,2 = AC LED inputs; 3 = NC; 4 = emitter; 5 = collector; 6 = base
    u_zc = sch.add_symbol(
        "Isolator:H11AA1",
        x=X_ZC_DETECT,
        y=140,
        ref="U2",
        value="H11AA1",
        footprint="Package_DIP:DIP-6_W7.62mm",
    )
    print(f"   U2: H11AA1 zero-crossing detector")

    # Input resistors for the optocoupler (limit LED current)
    r_zc1 = sch.add_symbol(
        "Device:R",
        x=X_ZC_DETECT - 25,
        y=130,
        ref="R3",
        value="33k",
        auto_footprint=True,
    )
    r_zc2 = sch.add_symbol(
        "Device:R",
        x=X_ZC_DETECT - 25,
        y=150,
        ref="R4",
        value="33k",
        auto_footprint=True,
    )
    print(f"   R3, R4: 33k input resistors for opto LED")

    # Wire opto input side
    r3_pin1 = r_zc1.pin_position("1")
    r3_pin2 = r_zc1.pin_position("2")
    r4_pin1 = r_zc2.pin_position("1")
    r4_pin2 = r_zc2.pin_position("2")

    # R3 input from AC_LINE (stub wire + label)
    sch.add_wire(r3_pin1, (r3_pin1[0] - 5, r3_pin1[1]))
    sch.add_label("AC_LINE", r3_pin1[0] - 5, r3_pin1[1])
    # R4 input from AC_NEUTRAL (stub wire + label)
    sch.add_wire(r4_pin1, (r4_pin1[0] - 5, r4_pin1[1]))
    sch.add_label("AC_NEUTRAL", r4_pin1[0] - 5, r4_pin1[1])

    # Connect R3/R4 to opto pins 1 and 2 (AC LED inputs)
    opto_pin1 = u_zc.pin_position("1")
    opto_pin2 = u_zc.pin_position("2")
    sch.add_wire(r3_pin2, opto_pin1)
    sch.add_wire(r4_pin2, opto_pin2)

    # Output side: pull-up to 3.3V, output to MCU
    r_zc_pull = sch.add_symbol(
        "Device:R",
        x=X_ZC_DETECT + 25,
        y=120,
        ref="R5",
        value="10k",
        auto_footprint=True,
    )
    print(f"   R5: 10k pull-up on opto output")

    r5_pin1 = r_zc_pull.pin_position("1")
    r5_pin2 = r_zc_pull.pin_position("2")

    # Pull-up to 3.3V
    sch.add_wire(r5_pin1, (r5_pin1[0], RAIL_3V3), warn_on_collision=False)
    sch.add_junction(r5_pin1[0], RAIL_3V3)

    # ZC output label at R5 pin2
    sch.add_label("ZC_DETECT", r5_pin2[0], r5_pin2[1])

    # Opto output side: pin 5 = collector, pin 4 = emitter
    opto_pin4 = u_zc.pin_position("4")  # emitter -> GND
    opto_pin5 = u_zc.pin_position("5")  # collector -> pull-up/ZC output

    # Collector to pull-up junction
    sch.add_wire(opto_pin5, (r5_pin2[0], opto_pin5[1]), warn_on_collision=False)
    sch.add_wire((r5_pin2[0], opto_pin5[1]), r5_pin2, warn_on_collision=False)

    # Emitter to GND
    sch.add_wire(opto_pin4, (opto_pin4[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(opto_pin4[0], RAIL_GND)

    # =========================================================================
    # Section 5: Supercap Charging Circuit
    # =========================================================================
    print("\n5. Adding supercap charging circuit...")

    # Bridge rectifier for charging (RB157)
    # Pins: "1"=+DC, "2"=AC~, "3"=-DC, "4"=AC~
    br1 = sch.add_symbol(
        "Diode_Bridge:RB157",
        x=X_CHARGE,
        y=140,
        ref="D1",
        value="RB157",
        footprint="Diode_THT:Diode_Bridge_DIP-4_W7.62mm_P5.08mm",
    )
    print(f"   D1: RB157 bridge rectifier for charging")

    # Charging resistor (limit current to ~0.5-1A)
    r_charge = sch.add_symbol(
        "Device:R",
        x=X_CHARGE - 25,
        y=140,
        ref="R6",
        value="150R 5W",
        footprint="Resistor_THT:R_Axial_DIN0617_L17.0mm_D6.0mm_P25.40mm_Horizontal",
    )
    print(f"   R6: 150 ohm 5W charging resistor")

    # Wire charging resistor input from AC (stub + label)
    r6_pin1 = r_charge.pin_position("1")
    r6_pin2 = r_charge.pin_position("2")
    sch.add_wire(r6_pin1, (r6_pin1[0] - 5, r6_pin1[1]))
    sch.add_label("AC_LINE", r6_pin1[0] - 5, r6_pin1[1])

    # Wire resistor output to bridge rectifier AC input (pin 2)
    br_ac = br1.pin_position("2")
    sch.add_wire(r6_pin2, br_ac)

    # Bridge rectifier DC output: pin 1 (+) = VRECT, pin 3 (-) = GND
    br_plus = br1.pin_position("1")
    br_minus = br1.pin_position("3")
    br_ac2 = br1.pin_position("4")

    # Wire AC neutral to other AC input (pin 4)
    sch.add_wire(br_ac2, (br_ac2[0], br_ac2[1] + 5))
    sch.add_label("AC_NEUTRAL", br_ac2[0], br_ac2[1] + 5)

    # Wire DC+ to VRECT rail
    sch.add_wire(br_plus, (br_plus[0], RAIL_VRECT), warn_on_collision=False)
    sch.add_junction(br_plus[0], RAIL_VRECT)

    # Wire DC- to GND rail
    sch.add_wire(br_minus, (br_minus[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(br_minus[0], RAIL_GND)

    # Supercap bank connectors (off-board banks connected via terminal blocks)
    # Positive bank connector
    j_scap_pos = sch.add_symbol(
        "Connector_Generic:Conn_01x02",
        x=X_CHARGE + 30,
        y=120,
        ref="J3",
        value="SCAP_POS",
        footprint="TerminalBlock:TerminalBlock_bornier-2_P5.08mm",
    )
    # Negative bank connector
    j_scap_neg = sch.add_symbol(
        "Connector_Generic:Conn_01x02",
        x=X_CHARGE + 30,
        y=160,
        ref="J4",
        value="SCAP_NEG",
        footprint="TerminalBlock:TerminalBlock_bornier-2_P5.08mm",
    )
    print(f"   J3, J4: Supercap bank connectors (positive, negative)")

    # Labels for supercap bank voltages (add stub wires for labels)
    j3_pin1 = j_scap_pos.pin_position("1")
    j3_pin2 = j_scap_pos.pin_position("2")
    sch.add_wire(j3_pin1, (j3_pin1[0] + 5, j3_pin1[1]))
    sch.add_label("SCAP_POS+", j3_pin1[0] + 5, j3_pin1[1])
    sch.add_wire(j3_pin2, (j3_pin2[0] + 5, j3_pin2[1]))
    sch.add_label("SCAP_POS_GND", j3_pin2[0] + 5, j3_pin2[1])

    j4_pin1 = j_scap_neg.pin_position("1")
    j4_pin2 = j_scap_neg.pin_position("2")
    sch.add_wire(j4_pin1, (j4_pin1[0] + 5, j4_pin1[1]))
    sch.add_label("SCAP_NEG+", j4_pin1[0] + 5, j4_pin1[1])
    sch.add_wire(j4_pin2, (j4_pin2[0] + 5, j4_pin2[1]))
    sch.add_label("SCAP_NEG_GND", j4_pin2[0] + 5, j4_pin2[1])

    # =========================================================================
    # Section 6: Discharge Circuit (2x IRFB4110 N-MOSFETs)
    # =========================================================================
    print("\n6. Adding discharge circuits...")

    # Positive bank discharge MOSFET
    q1 = sch.add_symbol(
        "Device:Q_NMOS",
        x=X_DISCHARGE,
        y=120,
        ref="Q1",
        value="IRFB4110",
        footprint="Package_TO_SOT_THT:TO-220-3_Vertical",
    )
    # Gate resistor for Q1
    r_gate1 = sch.add_symbol(
        "Device:R",
        x=X_DISCHARGE - 20,
        y=120,
        ref="R7",
        value="10R",
        auto_footprint=True,
    )
    print(f"   Q1: IRFB4110 (positive bank discharge)")
    print(f"   R7: 10R gate resistor")

    # Wire Q1 gate
    q1_gate = q1.pin_position("G")
    q1_drain = q1.pin_position("D")
    q1_source = q1.pin_position("S")

    r7_pin1 = r_gate1.pin_position("1")
    r7_pin2 = r_gate1.pin_position("2")
    sch.add_wire(r7_pin2, q1_gate)
    # Gate label (stub wire)
    sch.add_wire(r7_pin1, (r7_pin1[0] - 5, r7_pin1[1]))
    sch.add_label("GATE_POS", r7_pin1[0] - 5, r7_pin1[1])
    # Drain label (stub wire)
    sch.add_wire(q1_drain, (q1_drain[0], q1_drain[1] - 5))
    sch.add_label("SCAP_POS+", q1_drain[0], q1_drain[1] - 5)
    # Source label (stub wire)
    sch.add_wire(q1_source, (q1_source[0], q1_source[1] + 5))
    sch.add_label("DISCHARGE_POS", q1_source[0], q1_source[1] + 5)

    # Negative bank discharge MOSFET
    q2 = sch.add_symbol(
        "Device:Q_NMOS",
        x=X_DISCHARGE,
        y=180,
        ref="Q2",
        value="IRFB4110",
        footprint="Package_TO_SOT_THT:TO-220-3_Vertical",
    )
    # Gate resistor for Q2
    r_gate2 = sch.add_symbol(
        "Device:R",
        x=X_DISCHARGE - 20,
        y=180,
        ref="R8",
        value="10R",
        auto_footprint=True,
    )
    print(f"   Q2: IRFB4110 (negative bank discharge)")
    print(f"   R8: 10R gate resistor")

    # Wire Q2 gate
    q2_gate = q2.pin_position("G")
    q2_drain = q2.pin_position("D")
    q2_source = q2.pin_position("S")

    r8_pin1 = r_gate2.pin_position("1")
    r8_pin2 = r_gate2.pin_position("2")
    sch.add_wire(r8_pin2, q2_gate)
    # Gate label (stub wire)
    sch.add_wire(r8_pin1, (r8_pin1[0] - 5, r8_pin1[1]))
    sch.add_label("GATE_NEG", r8_pin1[0] - 5, r8_pin1[1])
    # Drain label (stub wire)
    sch.add_wire(q2_drain, (q2_drain[0], q2_drain[1] - 5))
    sch.add_label("SCAP_NEG+", q2_drain[0], q2_drain[1] - 5)
    # Source label (stub wire)
    sch.add_wire(q2_source, (q2_source[0], q2_source[1] + 5))
    sch.add_label("DISCHARGE_NEG", q2_source[0], q2_source[1] + 5)

    # =========================================================================
    # Section 7: Current Sensing (0.005 ohm shunt + INA180A1)
    # =========================================================================
    print("\n7. Adding current sensing...")

    # Current sense shunt resistor (low side, in discharge path)
    r_shunt = sch.add_symbol(
        "Device:R",
        x=X_ISENSE,
        y=140,
        ref="R9",
        value="5mR",
        footprint="Resistor_SMD:R_2512_6332Metric",
    )
    print(f"   R9: 0.005 ohm current sense shunt")

    r9_pin1 = r_shunt.pin_position("1")
    r9_pin2 = r_shunt.pin_position("2")
    # Shunt labels (stub wires)
    sch.add_wire(r9_pin1, (r9_pin1[0] - 5, r9_pin1[1]))
    sch.add_label("ISENSE_POS", r9_pin1[0] - 5, r9_pin1[1])
    sch.add_wire(r9_pin2, (r9_pin2[0] + 5, r9_pin2[1]))
    sch.add_label("ISENSE_NEG", r9_pin2[0] + 5, r9_pin2[1])

    # Connect shunt to GND rail
    sch.add_wire(r9_pin2, (r9_pin2[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(r9_pin2[0], RAIL_GND)

    # INA180A1 current sense amplifier
    # Pins: 1=OUT, 2=GND, 3=IN+, 4=IN-, 5=V+
    u_ina = sch.add_symbol(
        "Amplifier_Current:INA180A1",
        x=X_ISENSE + 30,
        y=140,
        ref="U3",
        value="INA180A1",
        footprint="Package_TO_SOT_SMD:SOT-23-5",
    )
    print(f"   U3: INA180A1 current sense amplifier (50V/V gain)")

    # Wire INA180 inputs to shunt via labels
    ina_inp = u_ina.pin_position("+")     # pin 3 = IN+
    ina_inn = u_ina.pin_position("-")     # pin 4 = IN-
    ina_out = u_ina.pin_position("1")     # pin 1 = OUT (no name, just number)
    ina_vs = u_ina.pin_position("V+")    # pin 5 = V+
    ina_gnd = u_ina.pin_position("GND")  # pin 2 = GND

    # IN+ label
    sch.add_wire(ina_inp, (ina_inp[0] - 5, ina_inp[1]))
    sch.add_label("ISENSE_POS", ina_inp[0] - 5, ina_inp[1])
    # IN- label
    sch.add_wire(ina_inn, (ina_inn[0] - 5, ina_inn[1]))
    sch.add_label("ISENSE_NEG", ina_inn[0] - 5, ina_inn[1])
    # OUT label
    sch.add_wire(ina_out, (ina_out[0] + 5, ina_out[1]))
    sch.add_label("I_SENSE_OUT", ina_out[0] + 5, ina_out[1])

    # Power connections
    sch.add_wire(ina_vs, (ina_vs[0], RAIL_3V3), warn_on_collision=False)
    sch.add_junction(ina_vs[0], RAIL_3V3)
    sch.add_wire(ina_gnd, (ina_gnd[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(ina_gnd[0], RAIL_GND)

    # Decoupling cap for INA180
    c_ina = sch.add_symbol(
        "Device:C",
        x=X_ISENSE + 45,
        y=155,
        ref="C1",
        value="100nF",
        auto_footprint=True,
    )
    sch.wire_decoupling_cap(c_ina, RAIL_3V3, RAIL_GND)
    print(f"   C1: 100nF decoupling for INA180")

    # =========================================================================
    # Section 8: MCU (STM32G031F6P6 TSSOP-20)
    # =========================================================================
    print("\n8. Adding MCU section...")

    # Place MCU directly (MCUBlock has unit param issue with add_symbol)
    u1_mcu = sch.add_symbol(
        "MCU_ST_STM32G0:STM32G031F6Px",
        x=X_MCU,
        y=140,
        ref="U1",
        value="STM32G031F6P6",
        footprint="Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm",
    )
    print(f"   U1: STM32G031F6P6")

    # Wire MCU VDD (pin 4) to 3.3V rail
    mcu_vdd = u1_mcu.pin_position("VDD")
    sch.add_wire(mcu_vdd, (mcu_vdd[0], RAIL_3V3), warn_on_collision=False)
    sch.add_junction(mcu_vdd[0], RAIL_3V3)

    # Wire MCU VSS (pin 5) to GND rail
    mcu_vss = u1_mcu.pin_position("VSS")
    sch.add_wire(mcu_vss, (mcu_vss[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(mcu_vss[0], RAIL_GND)

    # Bypass capacitors
    c2 = sch.add_symbol("Device:C", x=X_MCU - 30, y=160, ref="C2", value="100nF", auto_footprint=True)
    c3 = sch.add_symbol("Device:C", x=X_MCU - 20, y=160, ref="C3", value="100nF", auto_footprint=True)
    c4 = sch.add_symbol("Device:C", x=X_MCU - 10, y=160, ref="C4", value="4.7uF", auto_footprint=True)
    for cap in [c2, c3, c4]:
        sch.wire_decoupling_cap(cap, RAIL_3V3, RAIL_GND)
    print(f"   C2-C4: Bypass capacitors")

    # Reset button
    reset = ResetButton(
        sch,
        x=X_MCU - 40,
        y=200,
        pullup_value="10k",
        debounce_cap="100nF",
        ref_prefix="SW1",
        resistor_ref_start=10,
        cap_ref_start=5,
    )
    reset.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)
    print(f"   SW1: Reset button with R10 pull-up, C5 debounce")

    # Boot mode selector (BOOT0 = low for normal flash boot)
    boot = BootModeSelector(
        sch,
        x=X_MCU - 20,
        y=200,
        mode="stm32",
        default_state="flash",
        include_button=False,
        resistor_value="10k",
        ref_prefix="R11",
    )
    boot.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)
    print(f"   R11: BOOT0 pull-down (10k)")

    # SWD debug header
    debug = DebugHeader(
        sch,
        x=X_DEBUG,
        y=140,
        interface="swd",
        pins=6,
        ref="J5",
    )
    debug.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)
    print(f"   J5: 6-pin SWD debug header")

    # MCU signal labels (connect to peripherals via net labels)
    # PA0 -> V_AC_SENSE (ADC input from voltage divider)
    mcu_pa0 = u1_mcu.pin_position("PA0")
    sch.add_wire(mcu_pa0, (mcu_pa0[0] + 5, mcu_pa0[1]))
    sch.add_label("V_AC_SENSE", mcu_pa0[0] + 5, mcu_pa0[1])

    # ADC inputs
    sch.add_text(
        "MCU Pin Assignments:\n"
        "PA0 = V_AC_SENSE (ADC)\n"
        "PA1 = I_SENSE_OUT (ADC)\n"
        "PA4 = ZC_DETECT (EXTI)\n"
        "PA6 = GATE_POS (PWM)\n"
        "PA7 = GATE_NEG (PWM)\n"
        "PA8 = STATUS_LED\n"
        "PA13 = SWDIO\n"
        "PA14 = SWCLK\n",
        x=X_MCU - 40,
        y=230,
    )

    # =========================================================================
    # Section 9: 3.3V Power Supply (LDO)
    # =========================================================================
    print("\n9. Adding 3.3V LDO power supply...")

    # Place LDO manually (XC6206 pins are VI/VO/GND)
    u4_ldo = sch.add_symbol(
        "Regulator_Linear:XC6206PxxxMR",
        x=X_LDO,
        y=100,
        ref="U4",
        value="XC6206-3.3V",
    )

    # Get pin positions (VI, VO, GND)
    ldo_vi = u4_ldo.pin_position("VI")
    ldo_vo = u4_ldo.pin_position("VO")
    ldo_gnd = u4_ldo.pin_position("GND")

    # Wire LDO to rails
    sch.add_wire(ldo_vi, (ldo_vi[0], RAIL_VRECT), warn_on_collision=False)
    sch.add_junction(ldo_vi[0], RAIL_VRECT)
    sch.add_wire(ldo_vo, (ldo_vo[0], RAIL_3V3), warn_on_collision=False)
    sch.add_junction(ldo_vo[0], RAIL_3V3)
    sch.add_wire(ldo_gnd, (ldo_gnd[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(ldo_gnd[0], RAIL_GND)

    # Input cap
    c6 = sch.add_symbol("Device:C", x=X_LDO - 15, y=115, ref="C6", value="10uF", auto_footprint=True)
    sch.wire_decoupling_cap(c6, RAIL_VRECT, RAIL_GND)

    # Output caps
    c7 = sch.add_symbol("Device:C", x=X_LDO + 15, y=115, ref="C7", value="10uF", auto_footprint=True)
    c8 = sch.add_symbol("Device:C", x=X_LDO + 25, y=115, ref="C8", value="100nF", auto_footprint=True)
    sch.wire_decoupling_cap(c7, RAIL_3V3, RAIL_GND)
    sch.wire_decoupling_cap(c8, RAIL_3V3, RAIL_GND)

    print(f"   U4: XC6206-3.3V with caps C6-C8")

    # =========================================================================
    # Section 10: Status LED
    # =========================================================================
    print("\n10. Adding status LED...")

    led = LEDIndicator(
        sch,
        x=X_LED,
        y=100,
        ref_prefix="D2",
        label="STATUS",
        resistor_value="1k",
    )
    led.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)
    print(f"   D2: Status LED with 1k resistor")

    # =========================================================================
    # Section 11: Design Notes
    # =========================================================================
    print("\n11. Adding design notes...")

    sch.add_text(
        "Generator Soft-Start Design Notes:\n"
        "====================================\n"
        "1. Supercaps are off-board (hand-soldered)\n"
        "   2x banks of 30 series Tecate 12F 2.7V cells\n"
        "   Bank voltage: 81V, Bank capacitance: 0.4F\n"
        "2. MOSFET Q1/Q2 need heatsinks (TO-220)\n"
        "3. High-current traces: 2mm+ for discharge path\n"
        "4. AC mains isolation: keep HV section separate\n"
        "5. Current shunt R9 close to MOSFET source pins\n"
        "6. Board: 150mm x 100mm, 2-layer, 2oz copper\n",
        x=X_AC_INPUT,
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

    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Power symbols: {stats['power_symbol_count']}")
    print(f"      Wires: {stats['wire_count']}")
    print(f"      Junctions: {stats['junction_count']}")
    print(f"      Labels: {stats['label_count']}")

    # =========================================================================
    # Write Output
    # =========================================================================
    print("\n13. Writing schematic...")

    output_dir.mkdir(parents=True, exist_ok=True)
    sch_path = output_dir / "softstart.kicad_sch"
    sch.write(sch_path)
    print(f"   Schematic: {sch_path}")

    return sch_path


def create_softstart_pcb(output_dir: Path) -> Path:
    """
    Create the soft-start PCB with component placement.

    Board: 150mm x 100mm, 2-layer, 2oz copper, JLCPCB.

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating Soft-Start PCB...")
    print("=" * 60)

    # Board dimensions
    BOARD_WIDTH = 150.0
    BOARD_HEIGHT = 100.0
    BOARD_ORIGIN_X = 100.0
    BOARD_ORIGIN_Y = 100.0

    # Mounting hole positions (M3 at corners, 4mm inset)
    MH_INSET = 4.0
    MH_POSITIONS = [
        (BOARD_ORIGIN_X + MH_INSET, BOARD_ORIGIN_Y + MH_INSET),
        (BOARD_ORIGIN_X + BOARD_WIDTH - MH_INSET, BOARD_ORIGIN_Y + MH_INSET),
        (BOARD_ORIGIN_X + MH_INSET, BOARD_ORIGIN_Y + BOARD_HEIGHT - MH_INSET),
        (BOARD_ORIGIN_X + BOARD_WIDTH - MH_INSET, BOARD_ORIGIN_Y + BOARD_HEIGHT - MH_INSET),
    ]

    # Net definitions
    NETS = {
        "": 0,
        "AC_LINE": 1,
        "AC_NEUTRAL": 2,
        "GND": 3,
        "+3.3V": 4,
        "VRECT": 5,
        "SCAP_POS+": 6,
        "SCAP_POS_GND": 7,
        "SCAP_NEG+": 8,
        "SCAP_NEG_GND": 9,
        "GATE_POS": 10,
        "GATE_NEG": 11,
        "DISCHARGE_POS": 12,
        "DISCHARGE_NEG": 13,
        "ISENSE_POS": 14,
        "ISENSE_NEG": 15,
        "I_SENSE_OUT": 16,
        "V_AC_SENSE": 17,
        "ZC_DETECT": 18,
        "SWDIO": 19,
        "SWCLK": 20,
        "NRST": 21,
        "STATUS_LED": 22,
        "FUSED_LINE": 23,
    }

    # Component positions (organized by board section)
    # AC input section (left edge)
    J1_POS = (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 15)
    F1_POS = (BOARD_ORIGIN_X + 20, BOARD_ORIGIN_Y + 12)
    RV1_POS = (BOARD_ORIGIN_X + 30, BOARD_ORIGIN_Y + 20)
    J2_POS = (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 35)

    # Voltage sensing (left-center)
    R1_POS = (BOARD_ORIGIN_X + 45, BOARD_ORIGIN_Y + 15)
    R2_POS = (BOARD_ORIGIN_X + 45, BOARD_ORIGIN_Y + 25)

    # Zero-crossing detection
    U2_POS = (BOARD_ORIGIN_X + 60, BOARD_ORIGIN_Y + 15)
    R3_POS = (BOARD_ORIGIN_X + 50, BOARD_ORIGIN_Y + 10)
    R4_POS = (BOARD_ORIGIN_X + 50, BOARD_ORIGIN_Y + 20)
    R5_POS = (BOARD_ORIGIN_X + 70, BOARD_ORIGIN_Y + 10)

    # Charging circuit (center)
    D1_POS = (BOARD_ORIGIN_X + 75, BOARD_ORIGIN_Y + 40)
    R6_POS = (BOARD_ORIGIN_X + 60, BOARD_ORIGIN_Y + 40)

    # Supercap connectors (center-right)
    J3_POS = (BOARD_ORIGIN_X + 95, BOARD_ORIGIN_Y + 30)
    J4_POS = (BOARD_ORIGIN_X + 95, BOARD_ORIGIN_Y + 50)

    # Discharge MOSFETs (right section, near supercap connectors)
    Q1_POS = (BOARD_ORIGIN_X + 110, BOARD_ORIGIN_Y + 30)
    Q2_POS = (BOARD_ORIGIN_X + 110, BOARD_ORIGIN_Y + 50)
    R7_POS = (BOARD_ORIGIN_X + 102, BOARD_ORIGIN_Y + 30)
    R8_POS = (BOARD_ORIGIN_X + 102, BOARD_ORIGIN_Y + 50)

    # Current sensing
    R9_POS = (BOARD_ORIGIN_X + 125, BOARD_ORIGIN_Y + 40)
    U3_POS = (BOARD_ORIGIN_X + 135, BOARD_ORIGIN_Y + 35)
    C1_POS = (BOARD_ORIGIN_X + 135, BOARD_ORIGIN_Y + 45)

    # MCU section (right side, isolated from high-current)
    U1_POS = (BOARD_ORIGIN_X + 115, BOARD_ORIGIN_Y + 75)
    C2_POS = (BOARD_ORIGIN_X + 105, BOARD_ORIGIN_Y + 70)
    C3_POS = (BOARD_ORIGIN_X + 105, BOARD_ORIGIN_Y + 76)
    C4_POS = (BOARD_ORIGIN_X + 105, BOARD_ORIGIN_Y + 82)

    # LDO (center-right, between high-current and MCU)
    U4_POS = (BOARD_ORIGIN_X + 85, BOARD_ORIGIN_Y + 75)
    C6_POS = (BOARD_ORIGIN_X + 78, BOARD_ORIGIN_Y + 82)
    C7_POS = (BOARD_ORIGIN_X + 92, BOARD_ORIGIN_Y + 82)
    C8_POS = (BOARD_ORIGIN_X + 98, BOARD_ORIGIN_Y + 82)

    # Status LED
    D2_POS = (BOARD_ORIGIN_X + 130, BOARD_ORIGIN_Y + 70)
    R12_POS = (BOARD_ORIGIN_X + 130, BOARD_ORIGIN_Y + 76)

    # Debug header (right edge)
    J5_POS = (BOARD_ORIGIN_X + 142, BOARD_ORIGIN_Y + 75)

    # Reset button / boot resistor
    SW1_POS = (BOARD_ORIGIN_X + 115, BOARD_ORIGIN_Y + 90)
    R10_POS = (BOARD_ORIGIN_X + 120, BOARD_ORIGIN_Y + 90)
    C5_POS = (BOARD_ORIGIN_X + 125, BOARD_ORIGIN_Y + 90)
    R11_POS = (BOARD_ORIGIN_X + 110, BOARD_ORIGIN_Y + 90)

    # =========================================================================
    # Footprint generators
    # =========================================================================

    def generate_header() -> str:
        return """(kicad_pcb
  (version 20240108)
  (generator "kicad-tools-softstart")
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
        lines = ['  (net 0 "")']
        for name, num in NETS.items():
            if num > 0:
                lines.append(f'  (net {num} "{name}")')
        return "\n".join(lines)

    def generate_board_outline() -> str:
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

    def generate_terminal_block_2(ref: str, pos: tuple, net1: str, net2: str) -> str:
        x, y = pos
        n1 = NETS.get(net1, 0)
        n2 = NETS.get(net2, 0)
        return f"""  (footprint "TerminalBlock:TerminalBlock_bornier-2_P5.08mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -4) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "TB" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at -2.54 0) (size 2.5 2.5) (drill 1.3) (layers "*.Cu" "*.Mask") (net {n1} "{net1}"))
    (pad "2" thru_hole oval (at 2.54 0) (size 2.5 2.5) (drill 1.3) (layers "*.Cu" "*.Mask") (net {n2} "{net2}"))
  )"""

    def generate_to220(ref: str, pos: tuple, value: str, gate_net: str, drain_net: str, source_net: str) -> str:
        x, y = pos
        g = NETS.get(gate_net, 0)
        d = NETS.get(drain_net, 0)
        s = NETS.get(source_net, 0)
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
    (pad "1" thru_hole rect (at -2.54 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net {g} "{gate_net}"))
    (pad "2" thru_hole oval (at 0 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net {d} "{drain_net}"))
    (pad "3" thru_hole oval (at 2.54 0) (size 1.8 1.8) (drill 1.0) (layers "*.Cu" "*.Mask") (net {s} "{source_net}"))
  )"""

    def generate_cap_0805(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        x, y = pos
        n1 = NETS.get(net1, 0)
        n2 = NETS.get(net2, 0)
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
    (pad "1" smd roundrect (at -1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {n1} "{net1}"))
    (pad "2" smd roundrect (at 1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {n2} "{net2}"))
  )"""

    def generate_resistor_0805(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        x, y = pos
        n1 = NETS.get(net1, 0)
        n2 = NETS.get(net2, 0)
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
    (pad "1" smd roundrect (at -1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {n1} "{net1}"))
    (pad "2" smd roundrect (at 1 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {n2} "{net2}"))
  )"""

    def generate_resistor_2512(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        x, y = pos
        n1 = NETS.get(net1, 0)
        n2 = NETS.get(net2, 0)
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
    (pad "1" smd roundrect (at -3.1 0) (size 1.6 2.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {n1} "{net1}"))
    (pad "2" smd roundrect (at 3.1 0) (size 1.6 2.7) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {n2} "{net2}"))
  )"""

    def generate_sot223(ref: str, pos: tuple, value: str, pin1_net: str, pin2_net: str, pin3_net: str) -> str:
        x, y = pos
        n1 = NETS.get(pin1_net, 0)
        n2 = NETS.get(pin2_net, 0)
        n3 = NETS.get(pin3_net, 0)
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
    (pad "1" smd rect (at -3.15 2.3) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {n1} "{pin1_net}"))
    (pad "2" smd rect (at -3.15 0) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {n2} "{pin2_net}"))
    (pad "3" smd rect (at -3.15 -2.3) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {n3} "{pin3_net}"))
    (pad "2" smd rect (at 3.15 0) (size 2 3.8) (layers "F.Cu" "F.Paste" "F.Mask") (net {n2} "{pin2_net}"))
  )"""

    def generate_tssop20(ref: str, pos: tuple, value: str) -> str:
        x, y = pos
        return f"""  (footprint "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at -2.85 -2.925) (size 1.5 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["+3.3V"]} "+3.3V"))
    (pad "20" smd rect (at 2.85 -2.925) (size 1.5 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
  )"""

    def generate_sot23_5(ref: str, pos: tuple, value: str) -> str:
        x, y = pos
        return f"""  (footprint "Package_TO_SOT_SMD:SOT-23-5"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -2.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 2.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd rect (at -1.1 0.95) (size 1.06 0.65) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["ISENSE_POS"]} "ISENSE_POS"))
    (pad "2" smd rect (at -1.1 0) (size 1.06 0.65) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
    (pad "3" smd rect (at -1.1 -0.95) (size 1.06 0.65) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["ISENSE_NEG"]} "ISENSE_NEG"))
    (pad "4" smd rect (at 1.1 -0.95) (size 1.06 0.65) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["I_SENSE_OUT"]} "I_SENSE_OUT"))
    (pad "5" smd rect (at 1.1 0.95) (size 1.06 0.65) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["+3.3V"]} "+3.3V"))
  )"""

    def generate_dip6(ref: str, pos: tuple, value: str) -> str:
        x, y = pos
        return f"""  (footprint "Package_DIP:DIP-6_W7.62mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at -3.81 -2.54) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") (net {NETS["AC_LINE"]} "AC_LINE"))
    (pad "2" thru_hole oval (at -3.81 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") (net {NETS["AC_NEUTRAL"]} "AC_NEUTRAL"))
    (pad "3" thru_hole oval (at -3.81 2.54) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") (net 0 ""))
    (pad "4" thru_hole oval (at 3.81 2.54) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") (net {NETS["GND"]} "GND"))
    (pad "5" thru_hole oval (at 3.81 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") (net {NETS["ZC_DETECT"]} "ZC_DETECT"))
    (pad "6" thru_hole oval (at 3.81 -2.54) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") (net {NETS["+3.3V"]} "+3.3V"))
  )"""

    def generate_fuse_holder(ref: str, pos: tuple, value: str) -> str:
        x, y = pos
        return f"""  (footprint "Fuse:Fuseholder_Cylinder-5x20mm_Schurter_0031.8201_Horizontal_Open"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -4) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at -12.5 0) (size 2.5 2.5) (drill 1.5) (layers "*.Cu" "*.Mask") (net {NETS["AC_LINE"]} "AC_LINE"))
    (pad "2" thru_hole oval (at 12.5 0) (size 2.5 2.5) (drill 1.5) (layers "*.Cu" "*.Mask") (net {NETS["FUSED_LINE"]} "FUSED_LINE"))
  )"""

    def generate_varistor(ref: str, pos: tuple, value: str) -> str:
        x, y = pos
        return f"""  (footprint "Varistor:RV_Disc_D12mm_W4.2mm_P7.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -4) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole circle (at -3.75 0) (size 2 2) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["FUSED_LINE"]} "FUSED_LINE"))
    (pad "2" thru_hole circle (at 3.75 0) (size 2 2) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["AC_NEUTRAL"]} "AC_NEUTRAL"))
  )"""

    def generate_bridge_rect(ref: str, pos: tuple, value: str) -> str:
        x, y = pos
        return f"""  (footprint "Diode_THT:Diode_Bridge_DIP-4_W7.62mm_P5.08mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at -2.54 -2.54) (size 1.8 1.8) (drill 0.9) (layers "*.Cu" "*.Mask") (net {NETS["FUSED_LINE"]} "FUSED_LINE"))
    (pad "2" thru_hole oval (at 2.54 -2.54) (size 1.8 1.8) (drill 0.9) (layers "*.Cu" "*.Mask") (net {NETS["VRECT"]} "VRECT"))
    (pad "3" thru_hole oval (at 2.54 2.54) (size 1.8 1.8) (drill 0.9) (layers "*.Cu" "*.Mask") (net {NETS["AC_NEUTRAL"]} "AC_NEUTRAL"))
    (pad "4" thru_hole oval (at -2.54 2.54) (size 1.8 1.8) (drill 0.9) (layers "*.Cu" "*.Mask") (net {NETS["GND"]} "GND"))
  )"""

    def generate_resistor_axial(ref: str, pos: tuple, value: str, net1: str, net2: str) -> str:
        x, y = pos
        n1 = NETS.get(net1, 0)
        n2 = NETS.get(net2, 0)
        return f"""  (footprint "Resistor_THT:R_Axial_DIN0617_L17.0mm_D6.0mm_P25.40mm_Horizontal"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 12.7 -4) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 12.7 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole circle (at 0 0) (size 2 2) (drill 1.0) (layers "*.Cu" "*.Mask") (net {n1} "{net1}"))
    (pad "2" thru_hole circle (at 25.4 0) (size 2 2) (drill 1.0) (layers "*.Cu" "*.Mask") (net {n2} "{net2}"))
  )"""

    def generate_led_0805(ref: str, pos: tuple, net_a: str, net_k: str) -> str:
        x, y = pos
        na = NETS.get(net_a, 0)
        nk = NETS.get(net_k, 0)
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
    (pad "1" smd roundrect (at -1.05 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {na} "{net_a}"))
    (pad "2" smd roundrect (at 1.05 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {nk} "{net_k}"))
  )"""

    def generate_pin_header(ref: str, pos: tuple, pins: int, value: str, pin_nets: list) -> str:
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

    def generate_switch(ref: str, pos: tuple) -> str:
        x, y = pos
        return f"""  (footprint "Button_Switch_THT:SW_PUSH_6mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -4) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "RESET" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole circle (at -3.25 -2.25) (size 2 2) (drill 1.2) (layers "*.Cu" "*.Mask") (net {NETS["NRST"]} "NRST"))
    (pad "2" thru_hole circle (at 3.25 -2.25) (size 2 2) (drill 1.2) (layers "*.Cu" "*.Mask") (net {NETS["NRST"]} "NRST"))
    (pad "3" thru_hole circle (at -3.25 2.25) (size 2 2) (drill 1.2) (layers "*.Cu" "*.Mask") (net {NETS["GND"]} "GND"))
    (pad "4" thru_hole circle (at 3.25 2.25) (size 2 2) (drill 1.2) (layers "*.Cu" "*.Mask") (net {NETS["GND"]} "GND"))
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
    print("   4x M3 mounting holes at corners")

    print("\n2. Adding AC input section...")
    parts.append(generate_terminal_block_2("J1", J1_POS, "AC_LINE", "AC_NEUTRAL"))
    parts.append(generate_fuse_holder("F1", F1_POS, "15A"))
    parts.append(generate_varistor("RV1", RV1_POS, "275VAC"))
    parts.append(generate_terminal_block_2("J2", J2_POS, "FUSED_LINE", "AC_NEUTRAL"))

    print("\n3. Adding voltage sensing...")
    parts.append(generate_resistor_0805("R1", R1_POS, "510k", "AC_LINE", "V_AC_SENSE"))
    parts.append(generate_resistor_0805("R2", R2_POS, "10k", "V_AC_SENSE", "GND"))

    print("\n4. Adding zero-crossing detection...")
    parts.append(generate_dip6("U2", U2_POS, "H11AA1"))
    parts.append(generate_resistor_0805("R3", R3_POS, "33k", "AC_LINE", ""))
    parts.append(generate_resistor_0805("R4", R4_POS, "33k", "AC_NEUTRAL", ""))
    parts.append(generate_resistor_0805("R5", R5_POS, "10k", "+3.3V", "ZC_DETECT"))

    print("\n5. Adding charging circuit...")
    parts.append(generate_resistor_axial("R6", R6_POS, "150R 5W", "FUSED_LINE", ""))
    parts.append(generate_bridge_rect("D1", D1_POS, "DB107"))

    print("\n6. Adding supercap connectors...")
    parts.append(generate_terminal_block_2("J3", J3_POS, "SCAP_POS+", "SCAP_POS_GND"))
    parts.append(generate_terminal_block_2("J4", J4_POS, "SCAP_NEG+", "SCAP_NEG_GND"))

    print("\n7. Adding discharge MOSFETs...")
    parts.append(generate_to220("Q1", Q1_POS, "IRFB4110", "GATE_POS", "SCAP_POS+", "DISCHARGE_POS"))
    parts.append(generate_to220("Q2", Q2_POS, "IRFB4110", "GATE_NEG", "SCAP_NEG+", "DISCHARGE_NEG"))
    parts.append(generate_resistor_0805("R7", R7_POS, "10R", "GATE_POS", ""))
    parts.append(generate_resistor_0805("R8", R8_POS, "10R", "GATE_NEG", ""))

    print("\n8. Adding current sensing...")
    parts.append(generate_resistor_2512("R9", R9_POS, "5mR", "ISENSE_POS", "ISENSE_NEG"))
    parts.append(generate_sot23_5("U3", U3_POS, "INA180A1"))
    parts.append(generate_cap_0805("C1", C1_POS, "100nF", "+3.3V", "GND"))

    print("\n9. Adding MCU...")
    parts.append(generate_tssop20("U1", U1_POS, "STM32G031F6P6"))
    parts.append(generate_cap_0805("C2", C2_POS, "100nF", "+3.3V", "GND"))
    parts.append(generate_cap_0805("C3", C3_POS, "100nF", "+3.3V", "GND"))
    parts.append(generate_cap_0805("C4", C4_POS, "4.7uF", "+3.3V", "GND"))

    print("\n10. Adding LDO...")
    parts.append(generate_sot223("U4", U4_POS, "AMS1117-3.3", "VRECT", "GND", "+3.3V"))
    parts.append(generate_cap_0805("C6", C6_POS, "10uF", "VRECT", "GND"))
    parts.append(generate_cap_0805("C7", C7_POS, "10uF", "+3.3V", "GND"))
    parts.append(generate_cap_0805("C8", C8_POS, "100nF", "+3.3V", "GND"))

    print("\n11. Adding status LED...")
    parts.append(generate_led_0805("D2", D2_POS, "STATUS_LED", "GND"))
    parts.append(generate_resistor_0805("R12", R12_POS, "1k", "+3.3V", "STATUS_LED"))

    print("\n12. Adding debug header...")
    parts.append(generate_pin_header("J5", J5_POS, 6, "SWD", ["+3.3V", "SWDIO", "GND", "SWCLK", "GND", "NRST"]))

    print("\n13. Adding reset/boot components...")
    parts.append(generate_switch("SW1", SW1_POS))
    parts.append(generate_resistor_0805("R10", R10_POS, "10k", "+3.3V", "NRST"))
    parts.append(generate_cap_0805("C5", C5_POS, "100nF", "NRST", "GND"))
    parts.append(generate_resistor_0805("R11", R11_POS, "10k", "GND", ""))  # BOOT0 pull-down

    parts.append(")")  # Close kicad_pcb

    pcb_content = "\n".join(parts)

    # Write PCB file
    print("\n14. Writing PCB file...")
    output_dir.mkdir(parents=True, exist_ok=True)
    pcb_path = output_dir / "softstart.kicad_pcb"
    pcb_path.write_text(pcb_content)
    print(f"   PCB: {pcb_path}")

    print(f"\n   Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("   2-layer, 2oz copper, JLCPCB target")
    print(f"   Nets: {len([n for n in NETS.values() if n > 0])}")

    return pcb_path


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """Route the PCB using the autorouter."""
    from kicad_tools.router import DesignRules, load_pcb_for_routing
    from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer

    print("\n" + "=" * 60)
    print("Routing PCB...")
    print("=" * 60)

    rules = DesignRules(
        grid_resolution=0.1,
        trace_width=0.3,
        trace_clearance=0.3,
        via_drill=0.3,
        via_diameter=0.6,
    )

    print(f"\n1. Loading PCB: {input_path}")
    print(f"   Grid resolution: {rules.grid_resolution}mm")
    print(f"   Trace width: {rules.trace_width}mm")
    print(f"   Clearance: {rules.trace_clearance}mm")

    # Skip power and high-current nets
    skip_nets = [
        "AC_LINE", "AC_NEUTRAL", "FUSED_LINE", "GND",
        "+3.3V", "VRECT",
        "SCAP_POS+", "SCAP_POS_GND", "SCAP_NEG+", "SCAP_NEG_GND",
        "DISCHARGE_POS", "DISCHARGE_NEG",
    ]

    router, net_map = load_pcb_for_routing(
        str(input_path),
        skip_nets=skip_nets,
        rules=rules,
    )

    print(f"\n   Board size: {router.grid.width}mm x {router.grid.height}mm")
    print(f"   Nets loaded: {len(net_map)}")
    print(f"   Skipping high-current nets: {len(skip_nets)}")

    print("\n2. Routing nets...")
    router.route_all()

    stats_before = router.get_statistics()
    print("\n3. Raw routing results:")
    print(f"   Routes: {stats_before['routes']}")
    print(f"   Segments: {stats_before['segments']}")
    print(f"   Vias: {stats_before['vias']}")

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

    stats = router.get_statistics()
    print("\n5. Final routing results:")
    print(f"   Routes: {stats['routes']}")
    print(f"   Segments: {stats['segments']}")
    print(f"   Vias: {stats['vias']}")
    print(f"   Total length: {stats['total_length_mm']:.2f}mm")
    print(f"   Nets routed: {stats['nets_routed']}")

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

    total_signal_nets = len([n for n in router.nets if n > 0])
    success = stats["nets_routed"] == total_signal_nets

    if success:
        print("\n   SUCCESS: All signal nets routed!")
    else:
        print(f"\n   PARTIAL: Routed {stats['nets_routed']}/{total_signal_nets} signal nets")

    return success


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


def run_drc(pcb_path: Path) -> bool:
    """Run DRC on the PCB using kct check."""
    print("\n" + "=" * 60)
    print("Running DRC (via kct check)...")
    print("=" * 60)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "check", str(pcb_path)],
            capture_output=True,
            text=True,
        )

        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"   {line}")

        if result.returncode == 0:
            return True
        else:
            if result.stderr:
                print(f"\n   Error: {result.stderr}")
            return False

    except Exception as e:
        print(f"\n   Error running DRC: {e}")
        return False


def create_project(output_dir: Path, project_name: str) -> Path:
    """Create a KiCad project file."""
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


def main() -> int:
    """Main entry point."""
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        # Step 1: Create project file
        project_path = create_project(output_dir, "softstart")

        # Step 2: Create schematic
        sch_path = create_softstart_schematic(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_softstart_pcb(output_dir)

        # Step 5: Route PCB
        routed_path = output_dir / "softstart_routed.kicad_pcb"
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
        print("  AC Input: J1, J2, F1, RV1")
        print("  Voltage Sensing: R1, R2")
        print("  Zero-Crossing: U2 (H11AA1), R3-R5")
        print("  Charging: R6, D1 (bridge rect)")
        print("  Supercap Connectors: J3, J4")
        print("  Discharge: Q1, Q2 (IRFB4110), R7, R8")
        print("  Current Sense: R9 (5mR), U3 (INA180A1), C1")
        print("  MCU: U1 (STM32G031F6P6), C2-C4")
        print("  Power: U4 (AMS1117-3.3), C6-C8")
        print("  Reset: SW1, R10, C5")
        print("  Boot: R11")
        print("  Status: D2, R12")
        print("  Debug: J5 (SWD)")

        return 0 if erc_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
