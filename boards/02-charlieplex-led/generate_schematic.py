#!/usr/bin/env python3
"""
Generate a KiCad Schematic for the 3x3 Charlieplex LED Grid.

Uses global labels with short wire stubs for all net connections.
This approach:
1. Places symbols
2. Draws short wire from each pin
3. Places global label at wire end

Usage:
    python generate_schematic.py [output_file] [-v|--verbose]

Note:
    Design data (LED connections, resistor connections, MCU pins) is defined
    in design_spec.py to ensure schematic and PCB stay synchronized.
"""

import argparse
import sys
from pathlib import Path

from design_spec import (
    LED_CONNECTIONS,
    MCU_PINS,
    RESISTOR_CONNECTIONS,
    RESISTOR_VALUE,
)

from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic.grid import GridSize
from kicad_tools.schematic.models.schematic import Schematic, SnapMode
from kicad_tools.schematic.models.validation_mixin import format_validation_summary

# Warn if running source scripts with stale pipx install
warn_if_stale()

# Wire stub length for connecting pins to labels
WIRE_STUB = 5.08  # 200 mils


def add_pin_label(sch: Schematic, pin_pos: tuple, net_name: str, direction: str = "right"):
    """
    Add a wire stub from a pin position to a global label.

    Args:
        sch: Schematic object
        pin_pos: (x, y) tuple of pin position
        net_name: Name for the global label
        direction: "left" or "right" for label placement
    """
    if not pin_pos:
        return

    x, y = pin_pos
    if direction == "right":
        end_x = x + WIRE_STUB
        rotation = 180  # Label points left toward wire
    else:
        end_x = x - WIRE_STUB
        rotation = 0  # Label points right toward wire

    # Draw wire from pin to label position (uses schematic's grid snapping)
    sch.add_wire((x, y), (end_x, y))
    # Place global label at end of wire
    sch.add_global_label(net_name, end_x, y, shape="bidirectional", rotation=rotation)


def create_charlieplex_schematic(output_path: Path, verbose: bool = False) -> bool:
    """
    Create a 3x3 charlieplex LED grid schematic using global labels with wire stubs.

    Args:
        output_path: Path to write the schematic file
        verbose: If True, show detailed validation warnings

    Returns True if successful.
    """
    print("Creating Charlieplex LED Grid Schematic...")
    print("=" * 60)

    sch = Schematic(
        title="Charlieplex LED Grid",
        date="2025-01",
        revision="A",
        company="kicad-tools Demo",
        comment1="3x3 LED matrix using charlieplexing technique",
        comment2="9 LEDs driven by 4 GPIO pins",
        snap_mode=SnapMode.AUTO,
        grid=GridSize.SCH_STANDARD.value,  # 1.27mm (50mil) - standard KiCad schematic grid
    )

    # =========================================================================
    # Section 1: Place MCU with wire stubs to global labels
    # =========================================================================
    print("\n1. Placing MCU...")

    mcu_x, mcu_y = 50.8, 88.9
    mcu = sch.add_symbol(
        "Connector_Generic:Conn_01x08",
        x=mcu_x,
        y=mcu_y,
        ref="U1",
        value="MCU",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical",
    )
    print(f"   U1 (MCU): placed at ({mcu.x}, {mcu.y})")

    # Add wire stubs with global labels for MCU pins
    for pin_num, net_name in MCU_PINS.items():
        pin_pos = mcu.pin_position(pin_num)
        if net_name:
            add_pin_label(sch, pin_pos, net_name, direction="right")
            print(f"      Pin {pin_num} -> {net_name}")
        else:
            # Mark NC pins with no-connect markers
            if pin_pos:
                sch.add_no_connect(pin_pos[0], pin_pos[1])
                print(f"      Pin {pin_num} -> NC (no-connect)")

    # =========================================================================
    # Section 2: Place Resistors with wire stubs
    # =========================================================================
    print("\n2. Placing resistors...")

    resistor_base_x = 101.6
    resistor_base_y = 63.5
    resistor_spacing = 12.7

    for i, resistor in enumerate(RESISTOR_CONNECTIONS):
        x = resistor_base_x
        y = resistor_base_y + i * resistor_spacing

        r = sch.add_symbol(
            "Device:R", x=x, y=y, ref=resistor.ref, value=RESISTOR_VALUE, auto_footprint=True
        )
        print(f"   {resistor.ref}: placed at ({r.x}, {r.y})")

        # Add wire stubs with global labels at resistor pins
        pin1_pos = r.pin_position("1")
        pin2_pos = r.pin_position("2")

        add_pin_label(sch, pin1_pos, resistor.input_net, direction="left")
        add_pin_label(sch, pin2_pos, resistor.output_net, direction="right")
        print(f"      {resistor.input_net} --[{resistor.ref}]-- {resistor.output_net}")

    # =========================================================================
    # Section 3: Place LEDs with wire stubs
    # =========================================================================
    print("\n3. Placing LEDs in 3x3 grid...")

    led_start_x = 152.4
    led_start_y = 50.8
    led_spacing_x = 25.4
    led_spacing_y = 25.4

    for i, led_conn in enumerate(LED_CONNECTIONS):
        row = i // 3
        col = i % 3
        x = led_start_x + col * led_spacing_x
        y = led_start_y + row * led_spacing_y

        led = sch.add_symbol(
            "Device:LED",
            x=x,
            y=y,
            ref=led_conn.ref,
            value="LED",
            footprint="LED_SMD:LED_0805_2012Metric",
        )
        print(f"   {led_conn.ref}: placed at ({led.x}, {led.y})")

        # LED pins: pin 1 = cathode (K), pin 2 = anode (A)
        pin1_pos = led.pin_position("1")  # Cathode
        pin2_pos = led.pin_position("2")  # Anode

        add_pin_label(sch, pin1_pos, led_conn.cathode_node, direction="left")
        add_pin_label(sch, pin2_pos, led_conn.anode_node, direction="right")
        print(f"      {led_conn.anode_node} -> LED -> {led_conn.cathode_node}")

    # =========================================================================
    # Section 4: Add Power Symbols with wire stubs and PWR_FLAG
    # =========================================================================
    print("\n4. Adding power symbols...")

    # VCC power rail - use power symbol's position as connection point
    vcc_pwr = sch.add_power("power:VCC", x=25.4, y=25.4, rotation=0)
    vcc_conn = (vcc_pwr.x, vcc_pwr.y)
    sch.add_wire(vcc_conn, (vcc_conn[0] + WIRE_STUB, vcc_conn[1]))
    sch.add_global_label("VCC", vcc_conn[0] + WIRE_STUB, vcc_conn[1], shape="input", rotation=180)

    # GND power rail - use power symbol's position as connection point
    gnd_pwr = sch.add_power("power:GND", x=25.4, y=50.8, rotation=180)
    gnd_conn = (gnd_pwr.x, gnd_pwr.y)
    sch.add_wire(gnd_conn, (gnd_conn[0] + WIRE_STUB, gnd_conn[1]))
    sch.add_global_label("GND", gnd_conn[0] + WIRE_STUB, gnd_conn[1], shape="input", rotation=180)

    # Add PWR_FLAG symbols to indicate power entry points
    # This tells ERC that these power nets are intentionally driven externally
    # (e.g., from an external power supply connected to the MCU)
    sch.add_pwr_flag(vcc_pwr.x, vcc_pwr.y)
    sch.add_pwr_flag(gnd_pwr.x, gnd_pwr.y)

    print("   Added VCC and GND power symbols with PWR_FLAG")

    # =========================================================================
    # Section 5: Validate and Write
    # =========================================================================
    print("\n5. Validating schematic...")

    issues = sch.validate()

    # Print validation summary with warning categorization
    summary = format_validation_summary(issues, verbose=verbose)
    for line in summary.split("\n"):
        print(f"   {line}")

    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Power symbols: {stats['power_symbol_count']}")
    print(f"      Global labels: {len(sch.global_labels)}")
    print(f"      Wires: {stats['wire_count']}")

    print(f"\n6. Writing schematic to {output_path}...")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sch.write(output_path)
    print("   SUCCESS: Schematic written!")

    return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate a KiCad schematic for a 3x3 Charlieplex LED grid"
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output file path or directory (default: output/charlieplex_3x3.kicad_sch)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed validation warnings",
    )

    args = parser.parse_args()

    # Default output filename
    default_filename = "charlieplex_3x3.kicad_sch"

    if args.output:
        output_path = Path(args.output)
        # If user passes a directory, auto-append the default filename
        if output_path.is_dir():
            output_path = output_path / default_filename
            print(f"Note: Directory provided, using {output_path}")
    else:
        output_path = Path(__file__).parent / "output" / default_filename

    try:
        success = create_charlieplex_schematic(output_path, verbose=args.verbose)

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Output: {output_path}")
        print(f"Result: {'SUCCESS' if success else 'FAILED'}")

        print("\nCharlieplex LED mapping:")
        print("  LED   Anode    Cathode  (To light: Anode=HIGH, Cathode=LOW)")
        for led_conn in LED_CONNECTIONS:
            print(f"  {led_conn.ref}    {led_conn.anode_node}  {led_conn.cathode_node}")

        print("\nNet connectivity (via global labels):")
        print("  MCU pins 1-4 -> LINE_A-D -> R1-R4 -> NODE_A-D -> LEDs")

        return 0 if success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
