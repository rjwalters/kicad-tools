#!/usr/bin/env python3
"""
STM32 Development Board - End-to-End Example

This script demonstrates the complete PCB design workflow:
1. Create project file
2. Create schematic with power rails and components
3. Run ERC validation
4. Generate PCB with component placement
5. Route PCB traces
6. Run DRC validation

The design includes:
- LDO voltage regulator (5V to 3.3V)
- STM32F103C8T6 MCU (LQFP-48) with full power, decoupling and reset wiring
- 8MHz HSE crystal oscillator on PD0/PD1
- SWD debug header (PA13/PA14/PB3/NRST)
- User LED indicator on PB12 (active-low)

Usage:
    python generate_design.py [output_dir]

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
    DebugHeader,
    LEDIndicator,
    create_gpio_pull_resistor,
    create_mcu_decoupling_array,
)
from kicad_tools.schematic.models.schematic import Schematic

# Warn if running source scripts with stale pipx install
warn_if_stale()


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


def create_stm32_schematic(output_dir: Path) -> Path:
    """
    Create an STM32 development board schematic.

    This demonstrates the workflow for creating a simple MCU board with:
    - Power rails (5V, 3.3V, GND)
    - LDO voltage regulator (manually added)
    - 8MHz crystal oscillator (using CrystalOscillator block)
    - SWD debug header (using DebugHeader block)
    - User LED (using LEDIndicator block)

    The schematic is organized with power on the left, peripherals in the center,
    and debug interface on the right.
    """
    print("Creating STM32 Development Board schematic...")
    print("=" * 60)

    # Create schematic with title block
    sch = Schematic(
        title="STM32F103C8 Development Board",
        date="2025-01",
        revision="A",
        company="kicad-tools Example",
        comment1="End-to-end design example",
        comment2="Demonstrates circuit blocks API",
    )

    # Define power rail Y coordinates for organized layout
    # NOTE: With the MCU symbol added (LQFP-48 ~80mm tall), the schematic now
    # spans a much wider Y range. The MCU is centered around y=120, with VDD/
    # VBAT pins reaching up to y=80 and VSS/VSSA pins reaching down to y=160.
    # Power rails are placed above and below the MCU body so pin stubs don't
    # cross the rails.
    RAIL_5V = 30  # 5V input power (top, used only by LDO input)
    RAIL_3V3 = 70  # 3.3V regulated (above MCU body)
    RAIL_GND = 200  # Ground (below MCU body)

    # Schematic boundaries.  X_RIGHT terminates the +3.3V and GND rails at
    # the SWD header J1's pin column (computed at runtime below to avoid
    # dangling wire endpoints).  X_LEFT marks the +5V/GND power-symbol column.
    X_LEFT = 25
    # Position of the SWD header (used for rail termination too)
    X_SWD = 290

    # =========================================================================
    # Section 1: Power Rails
    # =========================================================================
    print("\n1. Creating power rails...")

    # Add power rails - endpoints should match actual component connection points
    # to avoid floating wire endpoints. For T-connections, use add_segmented_rail()
    # or ensure rail endpoints align with component tap points.
    #
    # Rail endpoints based on component positions:
    # - 5V: Power symbol (25) to LDO VIN (~93)
    # - 3.3V: LDO VOUT (~108) to debug header / MCU bypass caps / SWD header
    # - GND: Power symbol (25) across the full width to MCU VSS pins / SWD header
    # Rails extend from their leftmost power-symbol column to the SWD header
    # column (X_SWD - 5.52mm = pin-1 column after the header symbol's internal
    # offset).  Choosing the rail endpoint to coincide with an actual tap
    # point keeps the validator from flagging dangling wire endpoints.
    rail_3v3_xend = X_SWD - 5.52
    rail_gnd_xend = X_SWD - 5.52
    sch.add_rail(RAIL_5V, x_start=X_LEFT, x_end=93, net_label="+5V")
    sch.add_rail(RAIL_3V3, x_start=80, x_end=rail_3v3_xend, net_label="+3.3V")
    sch.add_rail(RAIL_GND, x_start=X_LEFT, x_end=rail_gnd_xend, net_label="GND")
    print("   Added +5V, +3.3V, and GND rails")

    # Add power symbols at the left ends of the rails.
    sch.add_power("power:+5V", x=X_LEFT, y=RAIL_5V - 10, rotation=0)
    sch.add_power("power:+3V3", x=80, y=RAIL_3V3 - 10, rotation=0)
    sch.add_power("power:GND", x=X_LEFT, y=RAIL_GND + 10, rotation=0)
    print("   Added power symbols")

    # =========================================================================
    # Section 2: LDO Voltage Regulator (Manual Component Placement)
    # =========================================================================
    print("\n2. Adding LDO voltage regulator...")

    # Note: The LDOBlock requires specific symbol libraries. Here we
    # demonstrate manual component placement as an alternative.

    # Add LDO symbol (using a generic 3-terminal regulator)
    ldo = sch.add_symbol(
        "Regulator_Linear:AMS1117-3.3",
        x=100,
        y=100,
        ref="U1",
        value="AMS1117-3.3",
        footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    )
    print(f"   LDO: {ldo.reference}")

    # Add input capacitor
    c_in = sch.add_symbol(
        "Device:C_Small",
        x=65,
        y=100,
        ref="C1",
        value="10uF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    print(f"   Input cap: {c_in.reference} = 10uF")

    # Add output capacitors
    c_out1 = sch.add_symbol(
        "Device:C_Small",
        x=135,
        y=100,
        ref="C2",
        value="10uF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    c_out2 = sch.add_symbol(
        "Device:C_Small",
        x=150,
        y=100,
        ref="C3",
        value="100nF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    print(f"   Output caps: {c_out1.reference} = 10uF, {c_out2.reference} = 100nF")

    # Wire LDO to power rails
    # VIN to 5V rail
    vin_pos = ldo.pin_position("VI")
    sch.add_wire(vin_pos, (vin_pos[0], RAIL_5V), warn_on_collision=False)
    sch.add_junction(vin_pos[0], RAIL_5V)

    # VOUT to 3.3V rail
    vout_pos = ldo.pin_position("VO")
    sch.add_wire(vout_pos, (vout_pos[0], RAIL_3V3), warn_on_collision=False)
    sch.add_junction(vout_pos[0], RAIL_3V3)

    # GND to ground rail
    gnd_pos = ldo.pin_position("GND")
    sch.add_wire(gnd_pos, (gnd_pos[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(gnd_pos[0], RAIL_GND)

    # Wire decoupling capacitors
    sch.wire_decoupling_cap(c_in, RAIL_5V, RAIL_GND)
    sch.wire_decoupling_cap(c_out1, RAIL_3V3, RAIL_GND)
    sch.wire_decoupling_cap(c_out2, RAIL_3V3, RAIL_GND)
    print("   Wired LDO and decoupling caps to power rails")

    # =========================================================================
    # Section 3: STM32F103C8T6 MCU (LQFP-48)
    # =========================================================================
    print("\n3. Adding STM32F103C8T6 MCU...")

    # MCU position - centered, with room for pin stubs and labels on all sides.
    # The STM32F103CxTx symbol spans roughly x=±20mm and y=±42mm around its
    # placement origin (after the 2.54mm pin lengths).  With MCU at (210, 120)
    # the bounding box is approximately x=[190,230], y=[78,162], which fits
    # between RAIL_3V3 (y=70) and RAIL_GND (y=200) with stub clearance.
    MCU_X = 210
    MCU_Y = 120

    mcu = sch.add_symbol(
        "MCU_ST_STM32F1:STM32F103C8Tx",
        x=MCU_X,
        y=MCU_Y,
        ref="U2",
        value="STM32F103C8T6",
        footprint="Package_QFP:LQFP-48_7x7mm_P0.5mm",
    )
    print(f"   {mcu.reference}: STM32F103C8T6 (LQFP-48)")

    # Wire MCU power pins to +3.3V rail.  STM32F103C8 has VDD on pins 24/36/48,
    # VBAT on pin 1, and VDDA on pin 9 -- all tie to +3.3V on a single-supply
    # design.  Each pin emerges from the top of the symbol so we route a short
    # stub up to the +3.3V rail.
    for pwr_pin in ("VDD", "VBAT", "VDDA"):
        # The symbol has multiple pins named "VDD" (24, 36, 48); pin_position
        # returns the first match.  Walk all pins by number to wire each one.
        for p in mcu.symbol_def.pins:
            if p.name == pwr_pin:
                pos = mcu.pin_position(p.number)
                sch.add_wire(pos, (pos[0], RAIL_3V3), warn_on_collision=False)
                sch.add_junction(pos[0], RAIL_3V3)

    # Wire MCU ground pins to GND rail.  VSS on pins 23/35/47 and VSSA on
    # pin 8 emerge from the bottom of the symbol.
    for gnd_pin in ("VSS", "VSSA"):
        for p in mcu.symbol_def.pins:
            if p.name == gnd_pin:
                pos = mcu.pin_position(p.number)
                sch.add_wire(pos, (pos[0], RAIL_GND), warn_on_collision=False)
                sch.add_junction(pos[0], RAIL_GND)

    print("   Wired MCU VDD/VBAT/VDDA to +3.3V and VSS/VSSA to GND")

    # MCU decoupling caps (one per VDD/VBAT/VDDA pin, plus a bulk cap).
    # Place between MCU and 3.3V rail, on the left side of the symbol.
    # Produces C12-C15 (100nF) at x=160,170,180,190 and C16 (4.7uF) at x=200.
    mcu_decoupling = create_mcu_decoupling_array(
        sch,
        x=160,
        y=85,
        supply_pins=4,
        ref_start=12,
        spacing=10,
        cap_symbol="Device:C_Small",
        cap_footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    mcu_decoupling.connect_to_rails(RAIL_3V3, RAIL_GND)
    print("   C12-C15 (100nF) + C16 (4.7uF) bypass caps")

    # =========================================================================
    # Section 4: Crystal Oscillator (8MHz) - connects to MCU PD0/PD1
    # =========================================================================
    print("\n4. Adding 8MHz crystal oscillator...")

    # Place crystal to the LEFT of the MCU, where PD0 (pin 5, OSC_IN) and PD1
    # (pin 6, OSC_OUT) emerge.  PD0 is at (MCU_X - 17.78, MCU_Y - 22.86) =
    # (192.22, 97.14) and PD1 at (192.22, 99.68) in screen coords.  Place the
    # crystal block well to the left so its IN/OUT labels align cleanly.
    xtal = CrystalOscillator(
        sch,
        x=140,
        y=100,
        frequency="8MHz",
        load_caps="20pF",
        ref_prefix="Y",
        cap_ref_start=10,
        crystal_footprint="Crystal:Crystal_HC49-4H_Vertical",
        cap_footprint="Capacitor_SMD:C_0805_2012Metric",
    )
    print(f"   Crystal: {xtal.crystal.reference} with C10, C11")

    # Connect crystal ground to GND rail
    xtal.connect_to_rails(gnd_rail_y=RAIL_GND)

    # Wire crystal IN/OUT to OSC_IN / OSC_OUT labels (on stubs).  Labels at the
    # MCU side are added below when we wire the MCU pins.
    in_pos = xtal.port("IN")
    out_pos = xtal.port("OUT")
    sch.add_wire(in_pos, (in_pos[0] - 10, in_pos[1]))
    sch.add_label("OSC_IN", in_pos[0] - 10, in_pos[1], rotation=0)
    sch.add_wire(out_pos, (out_pos[0] + 10, out_pos[1]))
    sch.add_label("OSC_OUT", out_pos[0] + 10, out_pos[1], rotation=0)
    print("   Added OSC_IN and OSC_OUT labels at crystal")

    # =========================================================================
    # Section 5: Debug Header (SWD)
    # =========================================================================
    print("\n5. Adding SWD debug header...")

    # 6-pin SWD header on the far right of the MCU
    debug = DebugHeader(
        sch,
        x=X_SWD,
        y=100,
        interface="swd",
        pins=6,
        series_resistors=False,
        ref="J1",
        header_footprint="Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
    )
    print(f"   Debug header: {debug.header.reference} (SWD-6)")

    # Connect debug header power to rails
    debug.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)

    # =========================================================================
    # Section 6: MCU peripheral wiring (SWD, oscillator, USER_LED)
    # =========================================================================
    print("\n6. Wiring MCU peripheral signals...")

    # Helper: route a short stub from a left-side MCU pin out to a label.
    # Left-side pins (PA0 family on the right, the rest on the left) emerge
    # along x=MCU_X-17.78, so the stub goes -10mm to the left.
    def _label_left_pin(pin_name: str, label_text: str, stub_len: float = -8) -> None:
        pos = mcu.pin_position(pin_name)
        sch.add_wire(pos, (pos[0] + stub_len, pos[1]), warn_on_collision=False)
        sch.add_label(label_text, pos[0] + stub_len, pos[1], rotation=0)

    def _label_right_pin(pin_name: str, label_text: str, stub_len: float = 8) -> None:
        pos = mcu.pin_position(pin_name)
        sch.add_wire(pos, (pos[0] + stub_len, pos[1]), warn_on_collision=False)
        sch.add_label(label_text, pos[0] + stub_len, pos[1], rotation=0)

    # Oscillator inputs (left side of symbol)
    _label_left_pin("PD0", "OSC_IN")
    _label_left_pin("PD1", "OSC_OUT")
    print("   PD0 -> OSC_IN, PD1 -> OSC_OUT")

    # Reset (left side)
    _label_left_pin("NRST", "NRST")
    print("   NRST -> NRST (reset header pin)")

    # SWD signals (right side of symbol -- PA13, PA14; left side -- PB3)
    _label_right_pin("PA13", "SWDIO")
    _label_right_pin("PA14", "SWCLK")
    _label_left_pin("PB3", "SWO")
    print("   PA13 -> SWDIO, PA14 -> SWCLK, PB3 -> SWO")

    # User LED on PB12 (left side, lower half).  Active-low: MCU sinks current.
    _label_left_pin("PB12", "USER_LED")
    print("   PB12 -> USER_LED")

    # No-connect markers on every MCU pin we don't drive.  Without these the
    # internal `Schematic.validate()` flags ~30 "pin not connected" errors and
    # KiCad ERC may warn about unconnected bidirectional pins.
    _connected_pin_names: set[str] = {
        # Power
        "VBAT",
        "VDD",
        "VDDA",
        "VSS",
        "VSSA",
        # Signals we explicitly wire below
        "NRST",
        "BOOT0",
        "PD0",
        "PD1",
        "PA13",
        "PA14",
        "PB3",
        "PB12",
    }
    _nc_count = 0
    for p in mcu.symbol_def.pins:
        if p.name in _connected_pin_names:
            continue
        pos = mcu.pin_position(p.number)
        sch.add_no_connect(pos[0], pos[1])
        _nc_count += 1
    print(f"   Added {_nc_count} no-connect markers on unused MCU pins")

    # BOOT0 pull-down (left side, top half).  Tying BOOT0 low forces normal
    # flash boot at reset; this is the typical configuration for development.
    # Refactored to use create_gpio_pull_resistor (issue #2573); previously
    # an inline 10k vertical pull-down with a manual GND stub.
    boot0_pos = mcu.pin_position("BOOT0")
    # Place the block left of BOOT0; the block's "BOOT0" port lands at
    # (block_x, block_y + 5) for a pull-down (resistor below center).
    block_x = boot0_pos[0] - 20
    block_y = boot0_pos[1] - 5
    boot_pull = create_gpio_pull_resistor(
        sch,
        x=block_x,
        y=block_y,
        pin_name="BOOT0",
        rail="GND",
        value="10k",
        pull_type="down",
        ref="R2",
        footprint="Resistor_SMD:R_0805_2012Metric",
    )
    # Wire BOOT0 stub from MCU to the block's BOOT0 port (horizontal).
    sch.add_wire(boot0_pos, boot_pull.port("BOOT0"), warn_on_collision=False)
    # Drop the resistor's GND end down to the GND rail.
    gnd_end = boot_pull.port("GND")
    sch.add_wire(gnd_end, (gnd_end[0], RAIL_GND), warn_on_collision=False)
    sch.add_junction(gnd_end[0], RAIL_GND)
    print("   R2 (10k) BOOT0 pull-down to GND (via create_gpio_pull_resistor)")

    # =========================================================================
    # Section 7: User LED (driven by MCU PB12, active-low)
    # =========================================================================
    print("\n7. Adding user LED (driven by MCU PB12)...")

    # LED + current-limiting resistor.  Wired so 3.3V -> D1 anode -> D1 cathode
    # -> R1 pad1; R1 pad2 -> USER_LED net -> MCU PB12.  When MCU pulls PB12
    # low, the LED illuminates (active-low).
    led = LEDIndicator(
        sch,
        x=265,
        y=160,
        ref_prefix="D1",
        label="USER",
        resistor_value="330R",
        led_footprint="LED_SMD:LED_0805_2012Metric",
        resistor_footprint="Resistor_SMD:R_0805_2012Metric",
    )
    print(f"   LED: {led.led.reference} with current-limiting resistor (active-low)")

    # Connect anode to +3.3V rail (top of LEDIndicator vertical block)
    vcc_pos = led.ports["VCC"]
    sch.add_wire(vcc_pos, (vcc_pos[0], RAIL_3V3), warn_on_collision=False)
    sch.add_junction(vcc_pos[0], RAIL_3V3)

    # Bottom port of LEDIndicator is r.pad2 -- route this to USER_LED label.
    # We do NOT connect this to GND -- the MCU drives the cathode side via
    # the USER_LED net.
    led_user = led.ports["GND"]  # this is r.pad2 (intentional misnomer in block)
    sch.add_wire(led_user, (led_user[0], led_user[1] + 10), warn_on_collision=False)
    sch.add_label("USER_LED", led_user[0], led_user[1] + 10, rotation=0)
    print("   D1/R1 wired between +3.3V and USER_LED (MCU PB12)")

    # =========================================================================
    # Section 8: Design Notes
    # =========================================================================
    print("\n8. Adding design notes...")

    sch.add_text(
        "STM32F103C8T6 Pin Assignments:\n"
        "  PA13 = SWDIO    (pin 34)\n"
        "  PA14 = SWCLK    (pin 37)\n"
        "  PB3  = SWO      (pin 39)\n"
        "  NRST = Reset    (pin 7)\n"
        "  PD0  = OSC_IN   (pin 5, HSE)\n"
        "  PD1  = OSC_OUT  (pin 6, HSE)\n"
        "  PB12 = USER_LED (pin 25, active-low)\n"
        "  BOOT0 pulled low via R2 (10k) for flash boot\n"
        "  HSE: 8MHz crystal Y1 with 20pF load caps C10/C11\n"
        "  Decoupling: C12-C15 (100nF) per VDD pin, C16 (4.7uF) bulk",
        x=X_LEFT,
        y=230,
    )

    # =========================================================================
    # Validate Schematic
    # =========================================================================
    print("\n9. Validating schematic...")

    # Run validation
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
        print(f"   Found {len(warnings)} warnings (floating wires expected)")

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
    print("\n10. Writing output files...")

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write schematic
    sch_path = output_dir / "stm32_devboard.kicad_sch"
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


def create_stm32_pcb(output_dir: Path) -> Path:
    """
    Create a PCB for the STM32 development board.

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating STM32 Development Board PCB...")
    print("=" * 60)

    # Board dimensions (mm) -- expanded to fit the LQFP-48 MCU + decoupling caps
    BOARD_WIDTH = 60.0
    BOARD_HEIGHT = 40.0
    BOARD_ORIGIN_X = 100.0
    BOARD_ORIGIN_Y = 100.0

    # Net definitions - must match schematic nets
    # LED_K is the intermediate node between D1 cathode and R1: gives the
    # PCB generator a name to apply to those two pads so they are not
    # left as net 0 (unconnected).
    NETS = {
        "": 0,
        "+5V": 1,
        "+3.3V": 2,
        "GND": 3,
        "OSC_IN": 4,
        "OSC_OUT": 5,
        "SWDIO": 6,
        "SWCLK": 7,
        "SWO": 8,
        "NRST": 9,
        "USER_LED": 10,
        "LED_K": 11,
        "BOOT0": 12,
    }

    # Component positions for a sensible layout on the 60x40mm board.
    # All passive caps/resistors use 0805 SMD packages: pad span 2mm and pad
    # size 1mm x 1.3mm, so neighbours need ~3mm centre-to-centre to keep
    # 0.127mm minimum pad-pad clearance (JLCPCB rule).  The LQFP-48 MCU
    # footprint spans 9.3mm tip-to-tip, so we leave at least 5mm on every
    # side.
    #
    # Layout (x increases left-to-right, y increases top-to-bottom):
    #   col 1  (x= 5..14): U1 LDO + LDO caps
    #   col 2  (x=15..26): crystal Y1 + load caps C10/C11
    #   col 3  (x=27..40): U2 STM32 MCU (centered around x=33)
    #   col 4  (x=41..50): R1/D1 LED stack + R2 BOOT0 pull-down
    #   col 5  (x=51..58): J1 SWD header
    U1_POS = (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 10)  # LDO (SOT-223)
    C1_POS = (BOARD_ORIGIN_X + 4, BOARD_ORIGIN_Y + 18)  # LDO input cap (left of LDO)
    C2_POS = (BOARD_ORIGIN_X + 15, BOARD_ORIGIN_Y + 10)  # LDO output cap 1 (right of LDO)
    C3_POS = (BOARD_ORIGIN_X + 15, BOARD_ORIGIN_Y + 16)  # LDO output cap 2

    # MCU center placement.  LQFP-48 footprint extends ~4.6mm in each
    # direction from its origin, so we leave at least 5mm clearance from
    # surrounding components.
    U2_POS = (BOARD_ORIGIN_X + 31, BOARD_ORIGIN_Y + 22)  # STM32F103C8T6

    # Crystal section: above the MCU's top edge.  Y1 sits between the LDO
    # and MCU horizontally, with OSC_IN/OSC_OUT load caps below it.
    # OSC_IN/OSC_OUT escape through the open space above the MCU's left
    # edge (PD0=pin 5, PD1=pin 6).
    Y1_POS = (BOARD_ORIGIN_X + 22, BOARD_ORIGIN_Y + 10)  # 8MHz crystal
    C10_POS = (BOARD_ORIGIN_X + 19, BOARD_ORIGIN_Y + 16)  # OSC_IN load cap
    C11_POS = (BOARD_ORIGIN_X + 25, BOARD_ORIGIN_Y + 16)  # OSC_OUT load cap

    # MCU decoupling caps (cluster below MCU footprint, near VSS/VDD pins)
    # Spaced 4mm apart to keep pad-pad clearance well above 0.127mm.
    C12_POS = (BOARD_ORIGIN_X + 26, BOARD_ORIGIN_Y + 32)  # 100nF (VDD/1)
    C13_POS = (BOARD_ORIGIN_X + 31, BOARD_ORIGIN_Y + 32)  # 100nF (VDD/24)
    C14_POS = (BOARD_ORIGIN_X + 36, BOARD_ORIGIN_Y + 32)  # 100nF (VDD/48)
    C15_POS = (BOARD_ORIGIN_X + 26, BOARD_ORIGIN_Y + 36)  # 100nF (VDDA/9)
    C16_POS = (BOARD_ORIGIN_X + 31, BOARD_ORIGIN_Y + 36)  # 4.7uF bulk

    # Right column (x=46..50): user LED + BOOT0 pull-down
    R1_POS = (BOARD_ORIGIN_X + 47, BOARD_ORIGIN_Y + 8)  # LED current-limiting R (3.3V to LED_K)
    D1_POS = (BOARD_ORIGIN_X + 47, BOARD_ORIGIN_Y + 12)  # User LED (LED_K to USER_LED)
    R2_POS = (BOARD_ORIGIN_X + 47, BOARD_ORIGIN_Y + 22)  # BOOT0 pull-down 10k

    # SWD header at far right (uses 1x06 vertical pin header, 2.54mm pitch)
    J1_POS = (BOARD_ORIGIN_X + 55, BOARD_ORIGIN_Y + 22)

    def generate_header() -> str:
        """Generate the PCB file header."""
        return """(kicad_pcb
  (version 20240108)
  (generator "kicad-tools-demo")
  (generator_version "9.0")
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
        """Generate the board outline (Edge.Cuts)."""
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

    def generate_sot223(ref: str, pos: tuple, value: str) -> str:
        """Generate SOT-223 footprint for LDO."""
        x, y = pos
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
    (pad "1" smd rect (at -3.15 2.3) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["+5V"]} "+5V"))
    (pad "2" smd rect (at -3.15 0) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
    (pad "3" smd rect (at -3.15 -2.3) (size 2 1.5) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["+3.3V"]} "+3.3V"))
    (pad "2" smd rect (at 3.15 0) (size 2 3.8) (layers "F.Cu" "F.Paste" "F.Mask") (net {NETS["GND"]} "GND"))
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

    def generate_led_0805(ref: str, pos: tuple, anode_net: str, cathode_net: str) -> str:
        """Generate 0805 LED footprint with explicit anode/cathode nets.

        Pad 1 is the anode, pad 2 is the cathode (KiCad LED_SMD convention).
        """
        x, y = pos
        a_num = NETS.get(anode_net, 0)
        k_num = NETS.get(cathode_net, 0)
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
    (pad "1" smd roundrect (at -1.05 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {a_num} "{anode_net}"))
    (pad "2" smd roundrect (at 1.05 0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {k_num} "{cathode_net}"))
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

    def generate_lqfp48(ref: str, pos: tuple, value: str, pin_nets: dict[int, str]) -> str:
        """Generate LQFP-48 footprint (7x7mm, 0.5mm pitch) for STM32F103C8T6.

        Pad layout (origin at footprint center, y-down):
          - pads 1..12  on the left edge   (x=-4.1625, y=-2.75..+2.75)
          - pads 13..24 on the bottom edge (y=+4.1625, x=-2.75..+2.75)
          - pads 25..36 on the right edge  (x=+4.1625, y=+2.75..-2.75)
          - pads 37..48 on the top edge    (y=-4.1625, x=+2.75..-2.75)

        ``pin_nets`` maps each 1..48 pad number to a net name from ``NETS``;
        missing entries default to net 0 (unconnected, which is correct for
        unused GPIOs on this devboard).
        """
        x, y = pos
        pad_layers = '(layers "F.Cu" "F.Paste" "F.Mask")'
        pad_attrs = "(roundrect_rratio 0.25)"

        # Pad coordinates (in footprint-local mm, relative to component origin)
        # Left edge: pads 1..12, x=-4.1625, y from -2.75 step +0.5
        # Bottom edge: pads 13..24, y=+4.1625, x from -2.75 step +0.5
        # Right edge: pads 25..36, x=+4.1625, y from +2.75 step -0.5
        # Top edge: pads 37..48, y=-4.1625, x from +2.75 step -0.5
        pad_positions: list[tuple[int, float, float]] = []
        for i in range(12):  # left
            pad_positions.append((1 + i, -4.1625, -2.75 + 0.5 * i))
        for i in range(12):  # bottom
            pad_positions.append((13 + i, -2.75 + 0.5 * i, 4.1625))
        for i in range(12):  # right
            pad_positions.append((25 + i, 4.1625, 2.75 - 0.5 * i))
        for i in range(12):  # top
            pad_positions.append((37 + i, 2.75 - 0.5 * i, -4.1625))

        # Build pad lines.  Pads on horizontal edges use rotated size.
        # Pad dimensions (1.475x0.3 mm) come straight from the official
        # Package_QFP:LQFP-48_7x7mm_P0.5mm KiCad footprint.
        pad_lines: list[str] = []
        for pad_num, px, py in pad_positions:
            net_name = pin_nets.get(pad_num, "")
            net_num = NETS.get(net_name, 0)
            # Left and right edges: pad long axis along x.
            # Top and bottom edges: pad long axis along y -- rotate 90.
            if pad_num <= 12 or (25 <= pad_num <= 36):
                size = "(size 1.475 0.3)"
            else:
                size = "(size 0.3 1.475)"
            pad_lines.append(
                f'    (pad "{pad_num}" smd roundrect '
                f"(at {px} {py}) {size} {pad_layers} {pad_attrs} "
                f'(net {net_num} "{net_name}"))'
            )

        pads_block = "\n".join(pad_lines)
        return f"""  (footprint "Package_QFP:LQFP-48_7x7mm_P0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -5.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "{value}" (at 0 5.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_block}
  )"""

    def generate_pin_header_6(ref: str, pos: tuple) -> str:
        """Generate 6-pin header footprint for SWD debug."""
        x, y = pos
        pitch = 2.54
        return f"""  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -8) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "SWD" (at 0 8) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" thru_hole rect (at 0 {-2.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["+3.3V"]} "+3.3V"))
    (pad "2" thru_hole oval (at 0 {-1.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["SWDIO"]} "SWDIO"))
    (pad "3" thru_hole oval (at 0 {-0.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["SWCLK"]} "SWCLK"))
    (pad "4" thru_hole oval (at 0 {0.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["SWO"]} "SWO"))
    (pad "5" thru_hole oval (at 0 {1.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["NRST"]} "NRST"))
    (pad "6" thru_hole oval (at 0 {2.5 * pitch:.2f}) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net {NETS["GND"]} "GND"))
  )"""

    # Build the PCB file
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
    ]

    print("\n1. Adding footprints...")

    # U1: LDO voltage regulator
    parts.append(generate_sot223("U1", U1_POS, "AMS1117-3.3"))
    print(f"   U1 (LDO) at {U1_POS}")

    # C1: Input capacitor (5V to GND)
    parts.append(generate_cap_0805("C1", C1_POS, "10uF", "+5V", "GND"))
    print(f"   C1 (10uF) at {C1_POS}")

    # C2, C3: Output capacitors (3.3V to GND)
    parts.append(generate_cap_0805("C2", C2_POS, "10uF", "+3.3V", "GND"))
    parts.append(generate_cap_0805("C3", C3_POS, "100nF", "+3.3V", "GND"))
    print(f"   C2 (10uF) at {C2_POS}")
    print(f"   C3 (100nF) at {C3_POS}")

    # U2: STM32F103C8T6 MCU (LQFP-48)
    # Full pinmap for the LQFP-48 package.  Connected pins drive nets
    # the SWD header / crystal / LED expect; unused pins default to net 0
    # (unconnected) by being absent from the dict.
    mcu_pin_nets: dict[int, str] = {
        # Power pins
        1: "+3.3V",  # VBAT
        9: "+3.3V",  # VDDA
        24: "+3.3V",  # VDD
        36: "+3.3V",  # VDD
        48: "+3.3V",  # VDD
        8: "GND",  # VSSA
        23: "GND",  # VSS
        35: "GND",  # VSS
        47: "GND",  # VSS
        # Signals
        5: "OSC_IN",  # PD0
        6: "OSC_OUT",  # PD1
        7: "NRST",  # NRST
        25: "USER_LED",  # PB12
        34: "SWDIO",  # PA13
        37: "SWCLK",  # PA14
        39: "SWO",  # PB3
        44: "BOOT0",  # BOOT0 (pulled low via R2)
    }
    parts.append(generate_lqfp48("U2", U2_POS, "STM32F103C8T6", mcu_pin_nets))
    print(f"   U2 (STM32F103C8T6) at {U2_POS}")

    # Y1: Crystal oscillator
    parts.append(generate_crystal_hc49("Y1", Y1_POS, "8MHz"))
    print(f"   Y1 (8MHz) at {Y1_POS}")

    # C10, C11: Crystal load capacitors
    parts.append(generate_cap_0805("C10", C10_POS, "20pF", "OSC_IN", "GND"))
    parts.append(generate_cap_0805("C11", C11_POS, "20pF", "OSC_OUT", "GND"))
    print(f"   C10, C11 (20pF) at {C10_POS}, {C11_POS}")

    # C12-C15: MCU per-pin decoupling caps (100nF, 3.3V to GND)
    parts.append(generate_cap_0805("C12", C12_POS, "100nF", "+3.3V", "GND"))
    parts.append(generate_cap_0805("C13", C13_POS, "100nF", "+3.3V", "GND"))
    parts.append(generate_cap_0805("C14", C14_POS, "100nF", "+3.3V", "GND"))
    parts.append(generate_cap_0805("C15", C15_POS, "100nF", "+3.3V", "GND"))
    print("   C12-C15 (100nF MCU bypass)")

    # C16: Bulk decoupling cap (4.7uF)
    parts.append(generate_cap_0805("C16", C16_POS, "4.7uF", "+3.3V", "GND"))
    print(f"   C16 (4.7uF bulk decoupling) at {C16_POS}")

    # R1: LED current-limiting resistor (3.3V -> LED_K).  D1 pulls LED_K
    # toward USER_LED net; when MCU PB12 sinks current the LED illuminates.
    parts.append(generate_resistor_0805("R1", R1_POS, "330R", "+3.3V", "LED_K"))
    print(f"   R1 (330R) at {R1_POS}")

    # D1: User LED -- anode = LED_K (after R1), cathode = USER_LED (MCU drain)
    parts.append(generate_led_0805("D1", D1_POS, anode_net="LED_K", cathode_net="USER_LED"))
    print(f"   D1 (LED) at {D1_POS}")

    # R2: BOOT0 pull-down (10k) so the MCU boots from flash by default.
    parts.append(generate_resistor_0805("R2", R2_POS, "10k", "BOOT0", "GND"))
    print(f"   R2 (10k BOOT0 pull-down) at {R2_POS}")

    # J1: SWD debug header
    parts.append(generate_pin_header_6("J1", J1_POS))
    print(f"   J1 (SWD header) at {J1_POS}")

    parts.append(")")  # Close kicad_pcb

    pcb_content = "\n".join(parts)

    # Write PCB file
    print("\n2. Writing PCB file...")
    pcb_path = output_dir / "stm32_devboard.kicad_pcb"
    pcb_path.write_text(pcb_content)
    print(f"   PCB: {pcb_path}")

    print(f"\n   Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print(
        "   Components: U1 LDO, U2 STM32F103C8T6, 10 caps, 1 crystal, 2 resistors, 1 LED, 1 header"
    )
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
    # Grid resolution must be <= clearance/2 for reliable DRC compliance
    rules = DesignRules(
        grid_resolution=0.05,
        trace_width=0.15,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
    )

    print(f"\n1. Loading PCB: {input_path}")
    print(f"   Grid resolution: {rules.grid_resolution}mm")
    print(f"   Trace width: {rules.trace_width}mm")
    print(f"   Clearance: {rules.trace_clearance}mm")

    # Skip power nets (route manually or use planes)
    skip_nets = ["+5V", "+3.3V", "GND"]

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
    # Determine output directory
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        # Step 1: Create project file
        project_path = create_project(output_dir, "stm32_devboard")

        # Step 2: Create schematic
        sch_path = create_stm32_schematic(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_stm32_pcb(output_dir)

        # Step 5: Route PCB
        routed_path = output_dir / "stm32_devboard_routed.kicad_pcb"
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
        print("\nBoard description:")
        print("  - U1: AMS1117-3.3 LDO (5V to 3.3V)")
        print("  - U2: STM32F103C8T6 MCU (LQFP-48, 0.5mm pitch)")
        print("  - C1-C3: LDO decoupling capacitors")
        print("  - Y1: 8MHz crystal oscillator (HSE on PD0/PD1)")
        print("  - C10-C11: Crystal load capacitors (20pF)")
        print("  - C12-C15: MCU bypass caps (100nF per VDD pin)")
        print("  - C16: Bulk decoupling (4.7uF)")
        print("  - R1, D1: User LED on PB12 (active-low)")
        print("  - R2: BOOT0 pull-down (10k)")
        print("  - J1: 6-pin SWD debug header")

        # For this demo board, partial routing is acceptable
        # Success if ERC passes and DRC has no errors (warnings OK)
        return 0 if erc_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
