# Charlieplexed LED Grid Demo

This demo demonstrates the kicad-tools autorouter by routing a 3x3 LED grid PCB.

## What is Charlieplexing?

Charlieplexing is a technique for driving many LEDs with fewer GPIO pins. With N pins,
you can control N*(N-1) LEDs by using the tri-state capability of GPIO pins:

- Each LED is connected between two GPIO pins
- To light an LED, set one pin HIGH (anode) and one LOW (cathode)
- Other pins are set to high-impedance (input mode) so they don't interfere

With 4 GPIO pins, we can drive 4*(4-1) = 12 LEDs. This demo uses 9 for a 3x3 grid.

## Circuit Design

```
                    +----[R1]----+----[R2]----+----[R3]----+----[R4]----+
                    |            |            |            |            |
                 LINE_A       LINE_B       LINE_C       LINE_D          |
                    |            |            |            |            |
                +---+---+    +---+---+    +---+---+    +---+---+        |
                |  U1   |    (GPIO)       (GPIO)       (GPIO)           |
                | (MCU) |                                               |
                +-------+                                               |
                                                                        |
                             LED Grid (9 LEDs)                          |
               +----------------+----------------+----------------+     |
               |     D1         |     D2         |     D3         |     |
               |   A -> B       |   B -> A       |   A -> C       |     |
               +----------------+----------------+----------------+     |
               |     D4         |     D5         |     D6         |     |
               |   C -> A       |   A -> D       |   D -> A       |     |
               +----------------+----------------+----------------+     |
               |     D7         |     D8         |     D9         |     |
               |   B -> C       |   C -> B       |   B -> D       |     |
               +----------------+----------------+----------------+     |
```

## Files

| File | Description |
|------|-------------|
| `generate_pcb.py` | Script to generate the unrouted PCB file |
| `charlieplex_3x3.kicad_pcb` | Generated unrouted PCB (after running generate_pcb.py) |
| `route_demo.py` | Script to run the autorouter on the PCB |
| `charlieplex_3x3_routed.kicad_pcb` | Routed PCB (after running route_demo.py) |

## Usage

### Step 1: Generate the PCB

```bash
python generate_pcb.py
```

This creates `charlieplex_3x3.kicad_pcb` with:
- 1 MCU (U1) - 8-pin DIP footprint
- 4 Resistors (R1-R4) - 0805 SMD
- 9 LEDs (D1-D9) - 0805 SMD, arranged in 3x3 grid
- Board outline (50mm x 55mm)
- Net definitions for all connections

### Step 2: Run the Autorouter

```bash
python route_demo.py
```

This:
1. Loads the unrouted PCB
2. Parses components and net assignments
3. Uses A* pathfinding to route connections
4. Saves the routed result to `charlieplex_3x3_routed.kicad_pcb`

**Note:** Due to the dense charlieplex topology, some nets may not be routable on a
2-layer board. This is expected behavior and demonstrates real-world routing challenges.
The autorouter successfully routes approximately 5-6 of the 8 signal nets.

### Step 3: View in KiCad (Optional)

Open `charlieplex_3x3_routed.kicad_pcb` in KiCad to visualize the routes.

## Routing Strategy

The autorouter uses:

1. **Minimum Spanning Tree (MST)** - Connects multi-pin nets by finding the shortest
   total connection distance, reducing wire length.

2. **Net Priority** - Routes nets in priority order (power nets first, then signals).

3. **A* Pathfinding** - Finds optimal paths around obstacles with minimal length.

4. **Via Management** - Can place vias to route on bottom layer when needed.

## Customization

### Change Design Rules

Edit `route_demo.py` to adjust:

```python
rules = DesignRules(
    grid_resolution=0.25,  # Routing grid (finer = more options, slower)
    trace_width=0.3,       # Trace width in mm
    trace_clearance=0.2,   # Minimum clearance in mm
    via_drill=0.3,         # Via drill diameter
    via_diameter=0.6,      # Via pad diameter
)
```

### Change Board Layout

Edit `generate_pcb.py` to adjust component positions, board size, etc.

### Skip Different Nets

By default, VCC and GND are skipped (assuming power planes). To route them:

```python
skip_nets = []  # Route everything
```

## Technical Details

### Net Assignments

| Net | Purpose | Components |
|-----|---------|------------|
| LINE_A - LINE_D | MCU GPIO to resistor | U1.1-4 → R1-4.1 |
| NODE_A - NODE_D | Resistor to LED matrix | R1-4.2 → D1-9 anodes/cathodes |
| VCC | Power supply | U1.7 |
| GND | Ground | U1.8 |

### LED Charlieplex Mapping

| LED | Anode | Cathode | To Light |
|-----|-------|---------|----------|
| D1 | NODE_A | NODE_B | A=HIGH, B=LOW, C=HiZ, D=HiZ |
| D2 | NODE_B | NODE_A | B=HIGH, A=LOW, C=HiZ, D=HiZ |
| D3 | NODE_A | NODE_C | A=HIGH, C=LOW, B=HiZ, D=HiZ |
| D4 | NODE_C | NODE_A | C=HIGH, A=LOW, B=HiZ, D=HiZ |
| D5 | NODE_A | NODE_D | A=HIGH, D=LOW, B=HiZ, C=HiZ |
| D6 | NODE_D | NODE_A | D=HIGH, A=LOW, B=HiZ, C=HiZ |
| D7 | NODE_B | NODE_C | B=HIGH, C=LOW, A=HiZ, D=HiZ |
| D8 | NODE_C | NODE_B | C=HIGH, B=LOW, A=HiZ, D=HiZ |
| D9 | NODE_B | NODE_D | B=HIGH, D=LOW, A=HiZ, C=HiZ |
