# kicad-tools Demo Boards

Complete PCB designs demonstrating kicad-tools capabilities. Each board includes a `.kct` project specification file describing the design intent, requirements, and progress.

## Boards by Complexity

| # | Board | Components | Nets | Key Concepts |
|---|-------|------------|------|--------------|
| 01 | [Voltage Divider](01-voltage-divider/) | 4 | 3 | Simplest possible design, workflow validation |
| 02 | [Charlieplex LED](02-charlieplex-led/) | 14 | 8 | Dense topology, crossing nets, Monte Carlo routing |
| 03 | [USB Joystick](03-usb-joystick/) | ~20 | 13 | Mixed signals, USB differential pairs, analog inputs |
| 04 | [STM32 Dev Board](04-stm32-devboard/) | ~30 | - | Programmatic schematic generation (layout pending) |

## Quick Start

```bash
# Run any board's generation script
cd boards/01-voltage-divider
python generate_design.py

# Or use uv
uv run python boards/01-voltage-divider/generate_design.py
```

## Project Files (.kct)

Each board includes a `project.kct` file with:

- **Project metadata**: Name, revision, author, description
- **Intent**: Use cases, interfaces, constraints
- **Requirements**: Electrical, mechanical, manufacturing specs
- **Suggestions**: Component choices, layout guidelines
- **Decisions**: Design choices with rationale
- **Progress**: Current phase and checklist

Example:
```yaml
kct_version: "1.0"

project:
  name: "Voltage Divider"
  revision: "A"
  artifacts:
    schematic: "output/voltage_divider.kicad_sch"
    pcb: "output/voltage_divider.kicad_pcb"

intent:
  summary: |
    Minimal test circuit for validating the complete
    kicad-tools pipeline.
```

## Board Details

### 01 - Voltage Divider (Simplest)

A minimal 4-component design for validating the complete kicad-tools workflow:
- 2 connectors (input/output)
- 2 resistors (10k/10k divider)
- 5V → 2.5V conversion

**Demonstrates**: Schematic generation, PCB creation, autorouting, DRC checking

### 02 - Charlieplex LED (Medium)

3x3 LED matrix using charlieplexing to drive 9 LEDs with 4 GPIO pins:
- Dense interconnected topology
- Many crossing nets requiring vias
- Intentionally challenging routing

**Demonstrates**: Monte Carlo routing, congestion handling, dense layouts

### 03 - USB Joystick (Complex)

USB HID game controller with mixed signal types:
- USB Type-C with differential pairs
- Analog joystick inputs
- Digital button inputs
- ATmega32U4 MCU

**Demonstrates**: Mixed-signal routing, impedance requirements, complex placement

### 04 - STM32 Dev Board (Most Complex)

STM32F103 "Blue Pill" style development board:
- Power regulation (USB → 3.3V LDO)
- Crystal oscillator
- SWD debug interface
- User LED

**Demonstrates**: Programmatic schematic generation with circuit blocks

## See Also

- [examples/](../examples/) - Feature-specific demos (BOM, DRC, placement, etc.)
- [examples/04-autorouter/](../examples/04-autorouter/) - Routing strategy comparisons
