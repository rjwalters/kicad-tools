#!/usr/bin/env python3
"""
Generate a KiCad Schematic for the 3x3 Charlieplex LED Grid.

Uses global labels with short wire stubs for all net connections.
This approach:
1. Places symbols
2. Draws short wire from each pin
3. Places global label at wire end

Usage:
    python generate_schematic.py [output_file]
"""

import sys
from pathlib import Path

from kicad_tools.schematic.models.schematic import Schematic, SnapMode


# Charlieplex LED connections: (LED_ref, anode_node, cathode_node)
LED_CONNECTIONS = [
    ("D1", "NODE_A", "NODE_B"),  # A->B
    ("D2", "NODE_B", "NODE_A"),  # B->A
    ("D3", "NODE_A", "NODE_C"),  # A->C
    ("D4", "NODE_C", "NODE_A"),  # C->A
    ("D5", "NODE_A", "NODE_D"),  # A->D
    ("D6", "NODE_D", "NODE_A"),  # D->A
    ("D7", "NODE_B", "NODE_C"),  # B->C
    ("D8", "NODE_C", "NODE_B"),  # C->B
    ("D9", "NODE_B", "NODE_D"),  # B->D
]

# MCU pin assignments
MCU_PINS = {
    "1": "LINE_A",
    "2": "LINE_B",
    "3": "LINE_C",
    "4": "LINE_D",
    "5": None,  # NC
    "6": None,  # NC
    "7": "VCC",
    "8": "GND",
}

# Resistor connections: (ref, pin1_net, pin2_net)
RESISTOR_CONNECTIONS = [
    ("R1", "LINE_A", "NODE_A"),
    ("R2", "LINE_B", "NODE_B"),
    ("R3", "LINE_C", "NODE_C"),
    ("R4", "LINE_D", "NODE_D"),
]

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

    # Draw wire from pin to label position
    # Use snap=False because pin positions may not be exactly on grid
    sch.add_wire((x, y), (end_x, y), snap=False)
    # Place global label at end of wire (also don't snap - must match wire endpoint)
    sch.add_global_label(net_name, end_x, y, shape="bidirectional", rotation=rotation, snap=False)


def create_charlieplex_schematic(output_path: Path) -> bool:
    """
    Create a 3x3 charlieplex LED grid schematic using global labels with wire stubs.

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
        grid=2.54,
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
                sch.add_no_connect(pin_pos[0], pin_pos[1], snap=False)
                print(f"      Pin {pin_num} -> NC (no-connect)")

    # =========================================================================
    # Section 2: Place Resistors with wire stubs
    # =========================================================================
    print("\n2. Placing resistors...")

    resistor_base_x = 101.6
    resistor_base_y = 63.5
    resistor_spacing = 12.7

    for i, (ref, pin1_net, pin2_net) in enumerate(RESISTOR_CONNECTIONS):
        x = resistor_base_x
        y = resistor_base_y + i * resistor_spacing

        r = sch.add_symbol("Device:R", x=x, y=y, ref=ref, value="330R")
        print(f"   {ref}: placed at ({r.x}, {r.y})")

        # Add wire stubs with global labels at resistor pins
        pin1_pos = r.pin_position("1")
        pin2_pos = r.pin_position("2")

        add_pin_label(sch, pin1_pos, pin1_net, direction="left")
        add_pin_label(sch, pin2_pos, pin2_net, direction="right")
        print(f"      {pin1_net} --[{ref}]-- {pin2_net}")

    # =========================================================================
    # Section 3: Place LEDs with wire stubs
    # =========================================================================
    print("\n3. Placing LEDs in 3x3 grid...")

    led_start_x = 152.4
    led_start_y = 50.8
    led_spacing_x = 25.4
    led_spacing_y = 25.4

    for i, (ref, anode_net, cathode_net) in enumerate(LED_CONNECTIONS):
        row = i // 3
        col = i % 3
        x = led_start_x + col * led_spacing_x
        y = led_start_y + row * led_spacing_y

        led = sch.add_symbol("Device:LED", x=x, y=y, ref=ref, value="LED")
        print(f"   {ref}: placed at ({led.x}, {led.y})")

        # LED pins: pin 1 = cathode (K), pin 2 = anode (A)
        pin1_pos = led.pin_position("1")  # Cathode
        pin2_pos = led.pin_position("2")  # Anode

        add_pin_label(sch, pin1_pos, cathode_net, direction="left")
        add_pin_label(sch, pin2_pos, anode_net, direction="right")
        print(f"      {anode_net} -> LED -> {cathode_net}")

    # =========================================================================
    # Section 4: Add Power Symbols with wire stubs and PWR_FLAG
    # =========================================================================
    print("\n4. Adding power symbols...")

    # VCC power rail - use power symbol's position as connection point
    vcc_pwr = sch.add_power("power:VCC", x=25.4, y=25.4, rotation=0)
    vcc_conn = (vcc_pwr.x, vcc_pwr.y)
    sch.add_wire(vcc_conn, (vcc_conn[0] + WIRE_STUB, vcc_conn[1]), snap=False)
    sch.add_global_label(
        "VCC", vcc_conn[0] + WIRE_STUB, vcc_conn[1], shape="input", rotation=180, snap=False
    )

    # GND power rail - use power symbol's position as connection point
    gnd_pwr = sch.add_power("power:GND", x=25.4, y=50.8, rotation=180)
    gnd_conn = (gnd_pwr.x, gnd_pwr.y)
    sch.add_wire(gnd_conn, (gnd_conn[0] + WIRE_STUB, gnd_conn[1]), snap=False)
    sch.add_global_label(
        "GND", gnd_conn[0] + WIRE_STUB, gnd_conn[1], shape="input", rotation=180, snap=False
    )

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
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    print(f"   Errors: {len(errors)}")
    print(f"   Warnings: {len(warnings)}")

    if errors:
        for err in errors[:5]:
            print(f"      ERROR: {err.get('message', err)}")

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
    if len(sys.argv) > 1:
        output_path = Path(sys.argv[1])
    else:
        output_path = Path(__file__).parent / "charlieplex_3x3.kicad_sch"

    try:
        success = create_charlieplex_schematic(output_path)

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Output: {output_path}")
        print(f"Result: {'SUCCESS' if success else 'FAILED'}")

        print("\nCharlieplex LED mapping:")
        print("  LED   Anode    Cathode  (To light: Anode=HIGH, Cathode=LOW)")
        for ref, anode, cathode in LED_CONNECTIONS:
            print(f"  {ref}    {anode}  {cathode}")

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
