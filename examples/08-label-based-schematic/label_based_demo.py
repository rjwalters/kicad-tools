#!/usr/bin/env python3
"""
Label-Based Schematic Generation Demo

This script demonstrates generating KiCad schematics using global labels
instead of explicit wires for connections. This approach simplifies
agent-based schematic generation by eliminating wire routing complexity.

Key benefits:
1. No wire routing logic needed
2. Symbols can be placed independently
3. Connectivity is explicit via named labels
4. Easier validation (net names are searchable)

Drawbacks:
1. Visual density (many labels)
2. Harder to visually trace signal flow
3. Unconventional style

Usage:
    python label_based_demo.py
    # Generates: label_based_mcu.kicad_sch
"""

# Add project to path for development
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from kicad_tools.schematic.models.schematic import Schematic


def create_label_based_schematic() -> Schematic:
    """Create a schematic using only global labels for connections.

    Circuit: Simple power distribution with decoupling caps and LED
    - Voltage regulator (LDO)
    - 2x decoupling caps
    - LED with current limiting resistor
    - Connector for external signals

    This demonstrates the label-based approach without requiring specific MCU libraries.
    """
    sch = Schematic(
        title="Label-Based Circuit Demo",
        date="2025-01",
        revision="POC",
        comment1="Proof of concept: No wires, only global labels",
    )

    # Grid spacing for symbol placement
    GRID = 2.54

    # =========================================================================
    # Place symbols independently (no spatial relationship needed)
    # =========================================================================

    # Voltage regulator (generic LDO)
    ldo = sch.add_symbol("Regulator_Linear:AMS1117-3.3", x=80, y=60, ref="U1", value="AMS1117-3.3")

    # Input and output decoupling caps
    cap_in = sch.add_symbol("Device:C", x=50, y=80, ref="C1", value="10uF")
    cap_out = sch.add_symbol("Device:C", x=110, y=80, ref="C2", value="10uF")

    # LED circuit
    led = sch.add_symbol("Device:LED", x=150, y=60, ref="D1", value="LED")
    r_led = sch.add_symbol("Device:R", x=150, y=40, ref="R1", value="330")

    # Generic connector for I/O signals
    conn = sch.add_symbol("Connector:Conn_01x04_Pin", x=200, y=60, ref="J1", value="CONN_4PIN")

    # =========================================================================
    # Power connections using power symbols (standard practice)
    # =========================================================================

    # LDO power pins
    vin_pos = ldo.pin_position("VI")
    vout_pos = ldo.pin_position("VO")
    gnd_pos = ldo.pin_position("GND")

    sch.add_power("power:+5V", vin_pos[0], vin_pos[1] - GRID)
    sch.add_power("power:GND", gnd_pos[0], gnd_pos[1] + GRID)
    # Output will use global label for 3.3V rail

    # Input cap power
    cap_in_pos1 = cap_in.pin_position("1")
    cap_in_pos2 = cap_in.pin_position("2")
    sch.add_power("power:+5V", cap_in_pos1[0], cap_in_pos1[1] - GRID)
    sch.add_power("power:GND", cap_in_pos2[0], cap_in_pos2[1] + GRID)

    # Output cap power via global labels
    cap_out_pos1 = cap_out.pin_position("1")
    cap_out_pos2 = cap_out.pin_position("2")
    sch.add_power("power:GND", cap_out_pos2[0], cap_out_pos2[1] + GRID)

    # LED resistor to 3.3V
    r_led_pos1 = r_led.pin_position("1")

    # =========================================================================
    # Signal connections using GLOBAL LABELS (the key innovation)
    # =========================================================================

    # 3.3V rail - global label from LDO output
    sch.add_global_label("VCC_3V3", vout_pos[0] + GRID * 2, vout_pos[1], shape="output")

    # 3.3V rail - global label at output cap
    sch.add_global_label("VCC_3V3", cap_out_pos1[0], cap_out_pos1[1] - GRID, shape="input")

    # 3.3V rail - global label at LED resistor
    sch.add_global_label("VCC_3V3", r_led_pos1[0], r_led_pos1[1] - GRID, shape="input")

    # LED anode/cathode connection (LED_A net)
    led_a = led.pin_position("A")
    led_k = led.pin_position("K")
    r_led_pos2 = r_led.pin_position("2")

    sch.add_global_label("LED_A", led_a[0], led_a[1] - GRID, shape="passive")
    sch.add_global_label("LED_A", r_led_pos2[0], r_led_pos2[1] + GRID, shape="passive")

    # LED cathode to ground
    sch.add_power("power:GND", led_k[0], led_k[1] + GRID)

    # Connector signals - each pin gets a global label
    for i, pin_num in enumerate(["1", "2", "3", "4"]):
        pin_pos = conn.pin_position(pin_num)
        signal_names = ["EXT_SDA", "EXT_SCL", "EXT_INT", "EXT_GND"]
        shapes = ["bidirectional", "bidirectional", "input", "passive"]
        sch.add_global_label(
            signal_names[i], pin_pos[0] - GRID * 2, pin_pos[1], shape=shapes[i], rotation=180
        )

    # =========================================================================
    # Add explanatory text
    # =========================================================================

    sch.add_text("Label-Based Schematic Demo", 30, 20)
    sch.add_text("All signal connections via global labels - no wires needed", 30, 28)
    sch.add_text("Matching label names = electrical connection", 30, 36)

    return sch


def print_statistics(sch: Schematic):
    """Print statistics about the generated schematic."""
    print("\n" + "=" * 60)
    print("LABEL-BASED SCHEMATIC STATISTICS")
    print("=" * 60)
    print(f"Symbols:        {len(sch.symbols)}")
    print(f"Power symbols:  {len(sch.power_symbols)}")
    print(f"Global labels:  {len(sch.global_labels)}")
    print(f"Hier labels:    {len(sch.hier_labels)}")
    print(f"Local labels:   {len(sch.labels)}")
    print(f"Wires:          {len(sch.wires)}")
    print(f"Junctions:      {len(sch.junctions)}")
    print()

    if sch.wires:
        print("WARNING: Wires present - this should be 0 for pure label-based!")
    else:
        print("SUCCESS: No wires - pure label-based connectivity!")

    print("\nGlobal labels used:")
    label_names = sorted({gl.text for gl in sch.global_labels})
    for name in label_names:
        count = sum(1 for gl in sch.global_labels if gl.text == name)
        print(f"  {name}: {count} instances")

    print("=" * 60)


def main():
    """Generate the label-based schematic demo."""
    print("Creating label-based schematic...")

    sch = create_label_based_schematic()

    # Write output
    output_path = Path(__file__).parent / "label_based_mcu.kicad_sch"
    sch.write(output_path)
    print(f"Wrote: {output_path}")

    # Print statistics
    print_statistics(sch)

    print("\nTo verify:")
    print("1. Open in KiCad EEschema")
    print("2. Run ERC - should pass with no connectivity errors")
    print("3. Export netlist - verify I2C_SDA and I2C_SCL nets are correct")


if __name__ == "__main__":
    main()
