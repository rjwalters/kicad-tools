# End-to-End PCB Design Example

This example demonstrates the complete workflow for creating a PCB design programmatically using kicad-tools.

## Overview

We create a simple **STM32 Development Board** (Blue Pill style) with:

- USB-C connector for power/programming
- 3.3V LDO voltage regulator
- STM32F103C8T6 MCU placeholder
- 8MHz crystal oscillator
- SWD debug header
- User LED with current-limiting resistor

## Circuit Blocks Used

| Block | Purpose | Components Created |
|-------|---------|-------------------|
| `USBConnector` | USB-C power input with ESD protection | J1, TVS diodes |
| `LDOBlock` | 5V to 3.3V voltage regulation | U1, C1-C3 |
| `CrystalOscillator` | 8MHz clock with load caps | Y1, C10-C11 |
| `DebugHeader` | SWD programming interface | J2 |
| `LEDIndicator` | User LED with resistor | D1, R1 |

## Running the Example

```bash
# From repository root
uv run python examples/05-end-to-end/design.py

# Or specify output directory
uv run python examples/05-end-to-end/design.py /path/to/output
```

## Output Files

The script generates:

```
output/
└── stm32_devboard.kicad_sch    # Complete schematic file
```

Open the schematic in KiCad to view and continue the design.

## Schematic Layout

```
    +5V ─────────────────────────────────────────────────────────
                    │
                    │
    USB-C ──────────┴───── LDO ──────┬───── MCU ───── Debug Header
     J1                    U1        │       U2          J2
                                     │
    +3.3V ───────────────────────────┴───────────────────────────
                                                    │
                                    Crystal         LED
                                      Y1           D1
                                       │            │
    GND ─────────────────────────────────────────────────────────
```

## Code Walkthrough

### 1. Create Schematic

```python
from kicad_tools.schematic.models.schematic import Schematic

sch = Schematic(
    title="STM32F103C8 Development Board",
    date="2025-01",
    revision="A",
)
```

### 2. Add Power Input

```python
from kicad_tools.schematic.blocks import USBConnector

usb = USBConnector(
    sch, x=30, y=100,
    connector_type="type-c",
    esd_protection=True,
)
```

### 3. Add Voltage Regulator

```python
from kicad_tools.schematic.blocks import LDOBlock

ldo = LDOBlock(
    sch, x=80, y=100,
    ref="U1",
    value="AMS1117-3.3",
    input_cap="10uF",
    output_caps=["10uF", "100nF"],
)

# Connect to power rails
ldo.connect_to_rails(
    vin_rail_y=30,   # 5V rail
    vout_rail_y=50,  # 3.3V rail
    gnd_rail_y=200,  # Ground rail
)
```

### 4. Add Crystal Oscillator

```python
from kicad_tools.schematic.blocks import CrystalOscillator

xtal = CrystalOscillator(
    sch, x=250, y=60,
    frequency="8MHz",
    load_caps="20pF",
)
```

### 5. Add Debug Header

```python
from kicad_tools.schematic.blocks import DebugHeader

debug = DebugHeader(
    sch, x=280, y=140,
    interface="swd",
    pins=6,
)
```

### 6. Add User LED

```python
from kicad_tools.schematic.blocks import LEDIndicator

led = LEDIndicator(
    sch, x=300, y=100,
    ref_prefix="D1",
    label="USER",
    resistor_value="330R",
)
```

### 7. Write Output

```python
sch.write("output/stm32_devboard.kicad_sch")
```

## Available Circuit Blocks

The `kicad_tools.schematic.blocks` module provides these reusable blocks:

### Power
- `LDOBlock` - LDO regulator with input/output capacitors
- `USBConnector` - USB Type-C/Micro-B/Mini-B with ESD protection
- `USBPowerInput` - USB power input with fuse protection
- `BarrelJackInput` - Barrel jack with reverse polarity protection
- `BatteryInput` - Battery connector with protection

### Oscillators
- `OscillatorBlock` - Active oscillator with decoupling
- `CrystalOscillator` - Passive crystal with load capacitors

### Passive Components
- `LEDIndicator` - LED with current-limiting resistor
- `DecouplingCaps` - Bank of decoupling capacitors

### MCU Support
- `MCUBlock` - MCU with bypass capacitors on power pins
- `DebugHeader` - SWD/JTAG/Tag-Connect debug interfaces

## Next Steps

After generating the schematic:

1. **Open in KiCad** - Review and complete the design
2. **Add MCU Symbol** - Add STM32F103C8Tx from KiCad library
3. **Complete Connections** - Wire MCU to peripherals
4. **Run ERC** - Check for electrical rule violations
5. **Create PCB** - Layout components on the board
6. **Route Traces** - Connect components with copper traces
7. **Run DRC** - Check design rules
8. **Export Files** - Generate Gerbers, BOM, and CPL

## Future API Features

The kicad-tools API is evolving to support the complete workflow:

```python
# Planned API (not yet implemented)
project = Project.create("stm32_devboard")

# Sync schematic to PCB
project.sync_to_pcb()

# Auto-route with manufacturer rules
project.route(strategy="negotiated", manufacturer="jlcpcb")

# Validate
result = project.check_drc(manufacturer="jlcpcb", layers=2)

# Export manufacturing files
project.export_gerbers("output/manufacturing/")
project.export_bom("output/manufacturing/bom.csv")
project.export_positions("output/manufacturing/positions.csv")
```

## Related Examples

- [01-schematic-analysis](../01-schematic-analysis/) - Parse existing schematics
- [02-bom-generation](../02-bom-generation/) - Extract BOM from designs
- [03-drc-checking](../03-drc-checking/) - Validate against manufacturer rules
- [04-autorouter](../04-autorouter/) - PCB autorouting strategies
