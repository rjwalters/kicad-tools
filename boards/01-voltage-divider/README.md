# Voltage Divider Demo

This demo demonstrates the complete kicad-tools workflow by creating the simplest possible PCB: a 4-component voltage divider.

## Quick Start

```bash
# One-command build (recommended)
kct build boards/01-voltage-divider

# Or run specific steps
kct build boards/01-voltage-divider --step schematic
kct build boards/01-voltage-divider --step pcb
kct build boards/01-voltage-divider --step route
kct build boards/01-voltage-divider --step verify

# Preview what would happen
kct build boards/01-voltage-divider --dry-run
```

## Circuit Overview

```
    +5V ─────┬─────────── J1 (Input)
             │
            [R1]
            10k
             │
    VOUT ────┼─────────── J2 (Output)
             │
            [R2]
            10k
             │
    GND ─────┴─────────── (Common)
```

Two 10k resistors divide the 5V input to produce 2.5V output (50% division ratio).

## Components

| Reference | Description | Footprint |
|-----------|-------------|-----------|
| J1 | Input connector (5V, GND) | 2.54mm pin header |
| J2 | Output connector (VOUT, GND) | 2.54mm pin header |
| R1 | Voltage divider (top) | 0805 SMD 10k |
| R2 | Voltage divider (bottom) | 0805 SMD 10k |

## Files

| File | Description |
|------|-------------|
| `generate_design.py` | Script to generate schematic, PCB, and route |
| `project.kct` | Project specification with requirements |
| `output/voltage_divider.kicad_sch` | Generated schematic |
| `output/voltage_divider.kicad_pcb` | Generated unrouted PCB |
| `output/voltage_divider_routed.kicad_pcb` | Routed PCB |

## Advanced: Manual Build

For more control, run the Python script directly. See [Prerequisites](../README.md#prerequisites-for-manual-build) for environment setup.

### Step 1: Generate the Design

```bash
# From repository root
uv run python boards/01-voltage-divider/generate_design.py
```

This creates:
- Schematic with all components and nets
- PCB with components placed on 30mm x 25mm board
- Routed PCB with all traces complete

### Step 2: Verify Output (Optional)

```bash
# Check for DRC violations
kct check output/voltage_divider_routed.kicad_pcb --mfr jlcpcb

# Generate BOM
kct bom output/voltage_divider.kicad_sch --format csv
```

### Step 3: View in KiCad (Optional)

Open `output/voltage_divider_routed.kicad_pcb` in KiCad to visualize the design.

## Design Specifications

| Parameter | Value |
|-----------|-------|
| Input Voltage | 5V |
| Output Voltage | 2.5V |
| Division Ratio | 0.5 (50%) |
| Board Size | 30mm x 25mm |
| Layers | 2 |
| Target Fab | JLCPCB |

## Why This Board?

This is the **simplest possible validation project** for kicad-tools. It exercises:

1. **Schematic Generation** - Creating symbols, wires, and net labels
2. **PCB Creation** - Placing footprints and defining the board outline
3. **Autorouting** - Connecting all nets with copper traces
4. **DRC Checking** - Validating against manufacturer rules

If the voltage divider routes successfully, the core workflow is verified.

## Related

- [02-charlieplex-led](../02-charlieplex-led/) - More complex routing challenge
- [03-usb-joystick](../03-usb-joystick/) - Mixed-signal design
- [04-stm32-devboard](../04-stm32-devboard/) - Programmatic schematic generation
