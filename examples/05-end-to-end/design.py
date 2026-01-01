#!/usr/bin/env python3
"""
STM32 Development Board - End-to-End Example

This script demonstrates creating a complete schematic design programmatically:
1. Create schematic with power rails and components
2. Add circuit blocks (LED, crystal, debug header)
3. Wire components together
4. Validate the schematic
5. Generate KiCad schematic file

Note: Some circuit blocks (USBConnector, MCUBlock) require specific KiCad
symbol libraries. This example uses a simpler approach with generic components
to demonstrate the API without external dependencies.

Usage:
    python design.py [output_dir]

If no output directory is specified, files are written to ./output/
"""

import sys
from pathlib import Path

from kicad_tools.schematic.blocks import (
    CrystalOscillator,
    DebugHeader,
    LEDIndicator,
)

# Import the schematic builder and circuit blocks
from kicad_tools.schematic.models.schematic import Schematic


def create_stm32_devboard(output_dir: Path) -> None:
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
    RAIL_5V = 30  # 5V input power
    RAIL_3V3 = 50  # 3.3V regulated
    RAIL_GND = 200  # Ground

    # Schematic boundaries
    X_LEFT = 25
    X_RIGHT = 280

    # =========================================================================
    # Section 1: Power Rails
    # =========================================================================
    print("\n1. Creating power rails...")

    # Add power rails across the schematic
    sch.add_rail(RAIL_5V, x_start=X_LEFT, x_end=150, net_label="+5V")
    sch.add_rail(RAIL_3V3, x_start=80, x_end=X_RIGHT, net_label="+3.3V")
    sch.add_rail(RAIL_GND, x_start=X_LEFT, x_end=X_RIGHT, net_label="GND")
    print("   Added +5V, +3.3V, and GND rails")

    # Add power symbols
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
    )
    print(f"   LDO: {ldo.reference}")

    # Add input capacitor
    c_in = sch.add_symbol(
        "Device:C_Small",
        x=65,
        y=100,
        ref="C1",
        value="10uF",
    )
    print(f"   Input cap: {c_in.reference} = 10uF")

    # Add output capacitors
    c_out1 = sch.add_symbol(
        "Device:C_Small",
        x=135,
        y=100,
        ref="C2",
        value="10uF",
    )
    c_out2 = sch.add_symbol(
        "Device:C_Small",
        x=150,
        y=100,
        ref="C3",
        value="100nF",
    )
    print(f"   Output caps: {c_out1.reference} = 10uF, {c_out2.reference} = 100nF")

    # Wire LDO to power rails
    # VIN to 5V rail
    vin_pos = ldo.pin_position("VI")
    sch.add_wire(vin_pos, (vin_pos[0], RAIL_5V))
    sch.add_junction(vin_pos[0], RAIL_5V)

    # VOUT to 3.3V rail
    vout_pos = ldo.pin_position("VO")
    sch.add_wire(vout_pos, (vout_pos[0], RAIL_3V3))
    sch.add_junction(vout_pos[0], RAIL_3V3)

    # GND to ground rail
    gnd_pos = ldo.pin_position("GND")
    sch.add_wire(gnd_pos, (gnd_pos[0], RAIL_GND))
    sch.add_junction(gnd_pos[0], RAIL_GND)

    # Wire decoupling capacitors
    sch.wire_decoupling_cap(c_in, RAIL_5V, RAIL_GND)
    sch.wire_decoupling_cap(c_out1, RAIL_3V3, RAIL_GND)
    sch.wire_decoupling_cap(c_out2, RAIL_3V3, RAIL_GND)
    print("   Wired LDO and decoupling caps to power rails")

    # =========================================================================
    # Section 3: Crystal Oscillator (8MHz)
    # =========================================================================
    print("\n3. Adding 8MHz crystal oscillator...")

    # Crystal with load capacitors (using the CrystalOscillator block)
    xtal = CrystalOscillator(
        sch,
        x=200,
        y=100,
        frequency="8MHz",
        load_caps="20pF",
        ref_prefix="Y",
        cap_ref_start=10,
    )
    print(f"   Crystal: {xtal.crystal.reference} with C10, C11")

    # Connect crystal ground to GND rail
    xtal.connect_to_rails(gnd_rail_y=RAIL_GND)

    # Add labels for oscillator connections
    in_pos = xtal.port("IN")
    out_pos = xtal.port("OUT")
    sch.add_label("OSC_IN", in_pos[0] - 10, in_pos[1], rotation=0)
    sch.add_wire(in_pos, (in_pos[0] - 10, in_pos[1]))
    sch.add_label("OSC_OUT", out_pos[0] + 10, out_pos[1], rotation=0)
    sch.add_wire(out_pos, (out_pos[0] + 10, out_pos[1]))
    print("   Added OSC_IN and OSC_OUT labels")

    # =========================================================================
    # Section 4: Debug Header (SWD)
    # =========================================================================
    print("\n4. Adding SWD debug header...")

    # 6-pin SWD header
    debug = DebugHeader(
        sch,
        x=250,
        y=100,
        interface="swd",
        pins=6,
        series_resistors=False,
        ref="J1",
    )
    print(f"   Debug header: {debug.header.reference} (SWD-6)")

    # Connect debug header power to rails
    debug.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)

    # =========================================================================
    # Section 5: User LED
    # =========================================================================
    print("\n5. Adding user LED...")

    # LED with current-limiting resistor
    led = LEDIndicator(
        sch,
        x=175,
        y=140,
        ref_prefix="D1",
        label="USER",
        resistor_value="330R",
    )
    print(f"   LED: {led.led.reference} with current-limiting resistor")

    # Connect LED to power rails
    led.connect_to_rails(vcc_rail_y=RAIL_3V3, gnd_rail_y=RAIL_GND)

    # =========================================================================
    # Section 6: Design Notes
    # =========================================================================
    print("\n6. Adding design notes...")

    sch.add_text(
        "Design Notes:\n"
        "1. Add STM32F103C8T6 MCU from KiCad library\n"
        "2. Connect OSC_IN/OSC_OUT to PA0/PA1\n"
        "3. Connect SWDIO/SWCLK to PA13/PA14\n"
        "4. Connect USER LED to PA5\n"
        "5. Add reset button between NRST and GND",
        x=X_LEFT,
        y=230,
    )

    # =========================================================================
    # Validate Schematic
    # =========================================================================
    print("\n7. Validating schematic...")

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
    print("\n8. Writing output files...")

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write schematic
    sch_path = output_dir / "stm32_devboard.kicad_sch"
    sch.write(sch_path)
    print(f"   Schematic: {sch_path}")

    # =========================================================================
    # Summary
    # =========================================================================
    print("\n" + "=" * 60)
    print("Design complete!")
    print(f"\nOutput files in: {output_dir.absolute()}")
    print("\nNext steps:")
    print("  1. Open schematic in KiCad")
    print("  2. Add STM32F103C8T6 MCU symbol")
    print("  3. Connect MCU to peripherals")
    print("  4. Run ERC check")
    print("  5. Create PCB layout")


def main() -> int:
    """Main entry point."""
    # Determine output directory
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        create_stm32_devboard(output_dir)
        return 0
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
