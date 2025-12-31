#!/usr/bin/env python3
"""
Generate a KiCad PCB for a 3x3 charlieplexed LED grid.

This script creates a PCB file with:
- 8-pin microcontroller (U1)
- 4 current-limiting resistors (R1-R4)
- 9 LEDs in a 3x3 grid (D1-D9)
- Charlieplex connections using 4 GPIO pins

Charlieplexing allows driving N*(N-1) LEDs with N pins.
With 4 pins (A, B, C, D), we can drive 12 LEDs:
  A->B, B->A, A->C, C->A, A->D, D->A (6 LEDs)
  B->C, C->B, B->D, D->B (4 LEDs)
  C->D, D->C (2 LEDs)

For a 3x3 grid, we use 9 of these 12 combinations.

Usage:
    python generate_pcb.py [output_file]
"""

import sys
import uuid
from pathlib import Path


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


# Board dimensions (mm)
BOARD_WIDTH = 50.0
BOARD_HEIGHT = 55.0
BOARD_ORIGIN_X = 100.0  # Offset from KiCad origin
BOARD_ORIGIN_Y = 100.0

# Component positions
MCU_POS = (BOARD_ORIGIN_X + 25, BOARD_ORIGIN_Y + 47)  # U1 at bottom center

# Resistor positions (horizontal row above MCU)
RESISTOR_POSITIONS = [
    (BOARD_ORIGIN_X + 8, BOARD_ORIGIN_Y + 38),   # R1
    (BOARD_ORIGIN_X + 18, BOARD_ORIGIN_Y + 38),  # R2
    (BOARD_ORIGIN_X + 32, BOARD_ORIGIN_Y + 38),  # R3
    (BOARD_ORIGIN_X + 42, BOARD_ORIGIN_Y + 38),  # R4
]

# LED positions (3x3 grid)
LED_SPACING = 8.0
LED_START_X = BOARD_ORIGIN_X + 17
LED_START_Y = BOARD_ORIGIN_Y + 10
LED_POSITIONS = [
    (LED_START_X + i * LED_SPACING, LED_START_Y + j * LED_SPACING)
    for j in range(3) for i in range(3)
]

# Net definitions
# Nets: 0 = no net, 1-4 = LINE_A/B/C/D (MCU to resistors), 5-8 = NODE_A/B/C/D (resistor to LEDs)
NETS = {
    "": 0,
    "LINE_A": 1,
    "LINE_B": 2,
    "LINE_C": 3,
    "LINE_D": 4,
    "NODE_A": 5,
    "NODE_B": 6,
    "NODE_C": 7,
    "NODE_D": 8,
    "VCC": 9,
    "GND": 10,
}

# Charlieplex LED connections: (anode_node, cathode_node)
# Using 9 of the 12 possible combinations for our 3x3 grid
LED_CONNECTIONS = [
    ("NODE_A", "NODE_B"),  # D1: A->B
    ("NODE_B", "NODE_A"),  # D2: B->A
    ("NODE_A", "NODE_C"),  # D3: A->C
    ("NODE_C", "NODE_A"),  # D4: C->A
    ("NODE_A", "NODE_D"),  # D5: A->D
    ("NODE_D", "NODE_A"),  # D6: D->A
    ("NODE_B", "NODE_C"),  # D7: B->C
    ("NODE_C", "NODE_B"),  # D8: C->B
    ("NODE_B", "NODE_D"),  # D9: B->D
]


def generate_header() -> str:
    """Generate the PCB file header."""
    return f"""(kicad_pcb
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
    lines = ["  (net 0 \"\")"]
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


def generate_mcu() -> str:
    """Generate the 8-pin MCU footprint (DIP-8 style)."""
    x, y = MCU_POS
    # DIP-8 dimensions: 7.62mm (300mil) row spacing, 2.54mm (100mil) pitch
    row_spacing = 7.62 / 2  # Distance from center to each row
    pin_pitch = 2.54

    # Pin assignments:
    # 1: LINE_A (GPIO)
    # 2: LINE_B (GPIO)
    # 3: LINE_C (GPIO)
    # 4: LINE_D (GPIO)
    # 5: NC
    # 6: NC
    # 7: VCC
    # 8: GND
    pin_nets = [
        (1, "LINE_A"),
        (2, "LINE_B"),
        (3, "LINE_C"),
        (4, "LINE_D"),
        (5, ""),  # NC
        (6, ""),  # NC
        (7, "VCC"),
        (8, "GND"),
    ]

    pads = []
    for i in range(4):
        # Left side: pins 1-4 (top to bottom)
        pin_num, net_name = pin_nets[i]
        net_num = NETS.get(net_name, 0)
        net_str = f'(net {net_num} "{net_name}")' if net_name else ""
        py = -1.5 * pin_pitch + i * pin_pitch
        pads.append(f"""    (pad "{pin_num}" thru_hole rect (at {-row_spacing:.3f} {py:.3f}) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") {net_str})""")

    for i in range(4):
        # Right side: pins 5-8 (bottom to top)
        pin_num, net_name = pin_nets[4 + i]
        net_num = NETS.get(net_name, 0)
        net_str = f'(net {net_num} "{net_name}")' if net_name else ""
        py = 1.5 * pin_pitch - i * pin_pitch
        pads.append(f"""    (pad "{pin_num}" thru_hole oval (at {row_spacing:.3f} {py:.3f}) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask") {net_str})""")

    pads_str = "\n".join(pads)
    return f"""  (footprint "Package_DIP:DIP-8_W7.62mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U1" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "MCU" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_resistor(ref: str, pos: tuple, input_net: str, output_net: str) -> str:
    """Generate an 0805 resistor footprint."""
    x, y = pos
    input_num = NETS[input_net]
    output_num = NETS[output_net]

    # 0805 pad positions: ~1mm from center
    pad_offset = 1.0

    return f"""  (footprint "Resistor_SMD:R_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "330R" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (pad "1" smd roundrect (at {-pad_offset} 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {input_num} "{input_net}"))
    (pad "2" smd roundrect (at {pad_offset} 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {output_num} "{output_net}"))
  )"""


def generate_led(ref: str, pos: tuple, anode_net: str, cathode_net: str) -> str:
    """Generate an 0805 LED footprint."""
    x, y = pos
    anode_num = NETS[anode_net]
    cathode_num = NETS[cathode_net]

    # 0805 pad positions
    pad_offset = 1.0

    return f"""  (footprint "LED_SMD:LED_0805_2012Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (fp_text value "LED" (at 0 1.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (pad "1" smd roundrect (at {-pad_offset} 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {cathode_num} "{cathode_net}"))
    (pad "2" smd roundrect (at {pad_offset} 0) (size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {anode_num} "{anode_net}"))
  )"""


def generate_pcb() -> str:
    """Generate the complete PCB file."""
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
        generate_mcu(),
    ]

    # Resistors: connect LINE_x to NODE_x
    resistor_connections = [
        ("R1", RESISTOR_POSITIONS[0], "LINE_A", "NODE_A"),
        ("R2", RESISTOR_POSITIONS[1], "LINE_B", "NODE_B"),
        ("R3", RESISTOR_POSITIONS[2], "LINE_C", "NODE_C"),
        ("R4", RESISTOR_POSITIONS[3], "LINE_D", "NODE_D"),
    ]
    for ref, pos, in_net, out_net in resistor_connections:
        parts.append(generate_resistor(ref, pos, in_net, out_net))

    # LEDs with charlieplex connections
    for i, (pos, (anode, cathode)) in enumerate(zip(LED_POSITIONS, LED_CONNECTIONS)):
        ref = f"D{i + 1}"
        parts.append(generate_led(ref, pos, anode, cathode))

    parts.append(")")  # Close kicad_pcb

    return "\n".join(parts)


def main():
    """Generate the PCB file."""
    output_file = sys.argv[1] if len(sys.argv) > 1 else "charlieplex_3x3.kicad_pcb"
    output_path = Path(__file__).parent / output_file

    pcb_content = generate_pcb()
    output_path.write_text(pcb_content)

    print(f"Generated: {output_path}")
    print(f"  Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print(f"  Components: 1 MCU, 4 resistors, 9 LEDs")
    print(f"  Nets: {len([n for n in NETS.values() if n > 0])} (4 LINE + 4 NODE + VCC + GND)")


if __name__ == "__main__":
    main()
