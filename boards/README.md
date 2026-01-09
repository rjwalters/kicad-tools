# kicad-tools Demo Boards

Complete PCB designs demonstrating kicad-tools capabilities. Each board includes a `.kct` project specification file describing the design intent, requirements, and progress.

## Board Status

| # | Board | Status | Components | Nets | Notes |
|---|-------|--------|------------|------|-------|
| 01 | [Voltage Divider](01-voltage-divider/) | ✅ Working | 4 | 3 | Simplest possible design, workflow validation |
| 02 | [Charlieplex LED](02-charlieplex-led/) | ⚠️ Needs optimization | 14 | 8 | Dense topology, may need trace optimization ([#659]) |
| 03 | [USB Joystick](03-usb-joystick/) | ⚠️ Complex routing | ~20 | 13 | Mixed signals, may not complete all routes on 2-layer |
| 04 | [STM32 Dev Board](04-stm32-devboard/) | 🚧 Schematic only | ~30 | - | Layout pending, demonstrates programmatic schematic generation |

**Status Legend:**
- ✅ Working - Generates manufacturable output
- ⚠️ Needs optimization - Works but may have routing challenges or require post-processing
- 🚧 Work in progress - Incomplete implementation

## Quick Start

```bash
# Run any board's generation script
cd boards/01-voltage-divider
python generate_design.py

# Or use uv
uv run python boards/01-voltage-divider/generate_design.py
```

## Complete Workflow

For a full design-to-manufacturing workflow:

```bash
# 1. Generate the design
cd boards/01-voltage-divider
python generate_design.py

# 2. Check for DRC violations (optional - use manufacturer rules)
kct check output/voltage_divider_routed.kicad_pcb --mfr jlcpcb

# 3. Generate BOM
kct bom output/voltage_divider.kicad_sch --format csv

# 4. Export Gerbers (via KiCad or kicad-cli)
# Open the .kicad_pcb file in KiCad and use File > Plot
```

See individual board READMEs for board-specific workflows.

## Known Issues

These are known limitations that may affect your experience:

| Issue | Affects | Description |
|-------|---------|-------------|
| [#659] | 02, 03 | Trace optimization may be needed for dense designs |
| [#661] | All | Router doesn't always warn about DRC violations |

[#659]: https://github.com/rjwalters/kicad-tools/issues/659
[#661]: https://github.com/rjwalters/kicad-tools/issues/661

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
