# USB Joystick Controller Demo

This demo demonstrates the kicad-tools autorouter by routing a USB game controller PCB
with mixed signal types: USB differential pairs, analog inputs, and digital I/O.

## Quick Start

```bash
# One-command build (recommended)
kct build boards/03-usb-joystick

# Or run specific steps
kct build boards/03-usb-joystick --step schematic
kct build boards/03-usb-joystick --step pcb
kct build boards/03-usb-joystick --step route
kct build boards/03-usb-joystick --step verify

# Preview what would happen
kct build boards/03-usb-joystick --dry-run
```

## Circuit Overview

```
                          +------------------+
    +--------+            |                  |
    | USB-C  |---D+/D--->| MCU (32-pin QFP) |<---[Y1 Crystal]
    |  J1    |---VBUS--->|                  |
    +--------+            |   ATmega32U4    |<---[C1-C4 Decoupling]
                          |                  |
                          +--------+---------+
                                   |
          +------------------------+------------------------+
          |            |           |           |            |
      +---+---+   +----+----+  +---+---+   +---+---+   +---+---+
      | JOY_X |   | JOY_Y   |  | BTN1  |   | BTN2  |   | BTN3  |
      +-------+   +---------+  +-------+   +-------+   +-------+
         Analog Joystick              Tactile Buttons
```

## Components

| Reference | Description | Footprint |
|-----------|-------------|-----------|
| U1 | Microcontroller (ATmega32U4-style) | TQFP-32 7x7mm |
| J1 | USB Type-C Connector | USB-C Receptacle |
| JOY1 | 2-axis Analog Joystick | 5-pin module |
| SW1-SW4 | Tactile Buttons | 6x6mm SMD |
| Y1 | 16MHz Crystal | HC49 Vertical |
| C1-C4 | Decoupling Capacitors (100nF) | 0402 SMD |

## Signal Types

This design demonstrates routing of different signal classes:

### USB Signals (High Priority)
- **USB_D+, USB_D-**: Differential pair, should be length-matched
- **VBUS**: 5V power from USB host
- **USB_CC1, USB_CC2**: Configuration channel for USB-C

### Analog Signals
- **JOY_X, JOY_Y**: Analog joystick axis outputs (0-VCC)
- **JOY_BTN**: Joystick button (active low)

### Digital Signals
- **BTN1-BTN4**: Button inputs (active low with GND)
- **XTAL1, XTAL2**: Crystal oscillator connections

### Power
- **VCC**: 3.3V/5V regulated power
- **GND**: Ground reference
- **VBUS**: USB 5V input

## Files

| File | Description |
|------|-------------|
| `generate_pcb.py` | Script to generate the unrouted PCB file |
| `generate_schematic.py` | Script to generate the schematic file |
| `route_demo.py` | Script to run the autorouter |
| `output/usb_joystick.kicad_sch` | Generated schematic |
| `output/usb_joystick.kicad_pcb` | Generated unrouted PCB |
| `output/usb_joystick_routed.kicad_pcb` | Routed PCB output |

## Advanced: Manual Build

For more control over individual steps, you can run Python scripts directly. See [Prerequisites](../README.md#prerequisites-for-manual-build) for environment setup.

### Step 1: Generate the Schematic

```bash
# From repository root (default output)
uv run python boards/03-usb-joystick/generate_schematic.py

# Or specify an output directory (auto-appends filename)
uv run python boards/03-usb-joystick/generate_schematic.py output/

# Or specify an explicit file path
uv run python boards/03-usb-joystick/generate_schematic.py output/usb_joystick.kicad_sch
```

Creates `output/usb_joystick.kicad_sch` with all components and wiring.

### Step 2: Generate the PCB

```bash
# From repository root
uv run python boards/03-usb-joystick/generate_pcb.py
```

Creates `output/usb_joystick.kicad_pcb` with all components placed and nets defined.

### Step 3: Run the Autorouter

```bash
# From repository root
uv run python boards/03-usb-joystick/route_demo.py
```

This:
1. Loads the unrouted PCB
2. Configures net classes (USB gets high priority)
3. Routes signal nets (power nets are skipped, assuming planes)
4. Saves the result to `output/usb_joystick_routed.kicad_pcb`

**Note:** This is a challenging routing problem with a dense 32-pin QFP and multiple
signal types. The autorouter demonstrates its capabilities but may not complete all
routes on a 2-layer board. This is realistic for complex designs requiring manual
intervention or additional layers.

### Step 4: View in KiCad (Optional)

Open `usb_joystick_routed.kicad_pcb` in KiCad to visualize the routes.

## Net Class Configuration

The demo configures net classes for priority-based routing:

```python
net_class_map = create_net_class_map(
    power_nets=["VCC", "VBUS", "GND"],      # Highest priority
    high_speed_nets=["USB_D+", "USB_D-"],   # USB differential pair
    clock_nets=["XTAL1", "XTAL2"],          # Crystal oscillator
)
```

This ensures:
1. Power nets route first (if not skipped)
2. USB differential pair routes early for best path
3. Crystal traces stay short
4. Button/joystick signals route last

## Design Considerations

### USB Routing
- D+ and D- should be routed as a differential pair
- Keep traces short and matched in length
- Avoid sharp bends (use 45-degree angles)

### Analog Signals
- JOY_X and JOY_Y carry analog voltages
- Keep away from high-speed digital signals
- Consider guard traces if noise is an issue

### Crystal Routing
- XTAL1 and XTAL2 should be short
- Place crystal close to MCU pins
- Avoid routing other signals under crystal

### Decoupling Capacitors
- C1-C3 placed close to MCU VCC pins
- C4 on VBUS near USB connector
- Short traces to GND plane

## Customization

### Change Design Rules

```python
rules = DesignRules(
    grid_resolution=0.25,  # Routing grid
    trace_width=0.25,      # Default trace width
    trace_clearance=0.15,  # Minimum clearance
    via_drill=0.3,         # Via drill diameter
    via_diameter=0.6,      # Via pad diameter
)
```

### Route Power Nets

By default, VCC/GND/VBUS are skipped (assuming copper pours). To route them:

```python
skip_nets = []  # Route everything
```

### Adjust Component Placement

Edit `generate_pcb.py` to modify:
- Board dimensions
- Component positions
- Pin assignments
