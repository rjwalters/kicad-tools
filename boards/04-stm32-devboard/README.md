# End-to-End PCB Design Example

This example demonstrates the complete workflow for creating a PCB design programmatically using kicad-tools.

## Quick Start

```bash
# One-command build (recommended)
kct build boards/04-stm32-devboard

# Or run specific steps
kct build boards/04-stm32-devboard --step schematic

# Preview what would happen
kct build boards/04-stm32-devboard --dry-run
```

## Overview

We create a simple **STM32 Development Board** (Blue Pill style) with both
schematic and a fully-placed PCB:

- 5V → 3.3V LDO voltage regulator (AMS1117-3.3) with input/output decoupling
- **STM32F103C8T6 MCU** (LQFP-48, 0.5mm pitch) -- placed and wired:
  - PA13/PA14 → SWDIO/SWCLK
  - PB3       → SWO
  - PD0/PD1   → OSC_IN/OSC_OUT (HSE crystal)
  - PB12      → USER_LED (active-low)
  - NRST      → SWD reset pin
  - BOOT0     → 10k pull-down (R2) for flash boot
  - VDD/VDDA/VBAT → +3.3V; VSS/VSSA → GND
  - Bypass: C12-C15 (100nF per VDD pin) + C16 (4.7uF bulk)
- 8MHz HSE crystal Y1 with 20pF load caps C10/C11
- 6-pin SWD debug header J1 (+3.3V, SWDIO, SWCLK, SWO, NRST, GND)
- User LED D1 with 330R series resistor R1 (active-low on PB12)

## Circuit Blocks Used

| Block | Purpose | Components Created |
|-------|---------|-------------------|
| `LDOBlock` | 5V to 3.3V voltage regulation | U1, C1-C3 |
| `CrystalOscillator` | 8MHz HSE crystal with load caps | Y1, C10-C11 |
| `DebugHeader` | SWD programming interface | J1 |
| `LEDIndicator` | User LED with current-limiting resistor | D1, R1 |

## Advanced: Manual Build

For more control, run the Python script directly:

```bash
# From repository root
uv run python boards/04-stm32-devboard/generate_design.py

# Or specify output directory
uv run python boards/04-stm32-devboard/generate_design.py /path/to/output
```

## Output Files

The script generates:

```
output/
├── stm32_devboard.kicad_pro          # KiCad project file
├── stm32_devboard.kicad_sch          # Schematic
├── stm32_devboard.kicad_pcb          # Unrouted PCB with all footprints placed
└── stm32_devboard_routed.kicad_pcb   # Auto-routed PCB
```

Open the schematic in KiCad to view and continue the design.

## Manufacturing readiness status

For the current readiness state, run:

```bash
uv run kct fleet status --boards-dir boards/04-stm32-devboard
```

**Last verified 2026-06-17 (#3765)**: the schematic and PCB now share one
canonical 12-net model — `compare_netlists(schematic, routed_pcb)` is **clean
(0 mismatches)** and `schematic_net_count == pcb_net_count == 12` with no drift.
The schematic was brought into pad-for-pad agreement with the PCB's `NETS`
table: the 3.3 V rail uses the `+3.3V` spelling (synthesized power symbol),
`BOOT0` and `LED_K` are named nets (no KiCad `Net-(...)` placeholders), and the
U1 (AMS1117) and J1 (SWD header) pinouts follow the PCB pad order. ERC passes.

The committed routed PCB **routes NRST cleanly** (`NRST` `U2.7` → `J1.5`, 2/2
connected) and has **0 blocking DRC errors** against `--mfr jlcpcb-tier1`
(`over_tolerance: false`, ship-ready `passed: true`, 52/55 pads). One
**non-blocking `connectivity` advisory** remains (filtered from the CI gate per
#3074):

- `GND` (`U2.23`): the LQFP-48 west-corner VSS pad whose `OSC_OUT` B.Cu escape
  window is too tight for even a 0.3 mm micro-via stitch (the documented
  #2834 / #3033 case). The MCU VSS rail still bonds to plane through the other
  stitched VSS pads.

**Routed-PCB regeneration note (#3773):** the zone-filler regression described
in #3773 (~30 `clearance_*_zone` shorts on fresh regen) **no longer reproduces**
as of the 2026-07-05 fresh-build sweep: a from-scratch `generate_design.py` run
produced a fully routed board with copper-LVS 0 shorts / 0 opens and
`kicad-cli pcb drc --refill-zones` reporting 0 violations. Caveats that remain:
the recipe is run-twice non-idempotent (first run from a clean dir exits 1 via
a constraint-sidecar interaction, second run passes — #3919) and `kct build`
still cannot reproduce the board (#3918), so regenerate via
`generate_design.py` directly.

The 2026-05-11 audit table (router 8/9, #2695/#2696/#3075/#3080) was removed
when those issues closed — see issue #3212 for the rationale.

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

The generator already builds a complete schematic + placed PCB and runs the
auto-router.  After generating, you can:

1. **Open in KiCad** - inspect the schematic and PCB visually
2. **Run ERC** - `kct erc boards/04-stm32-devboard/output/stm32_devboard.kicad_sch`
3. **Run DRC** - `kct check boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb --mfr jlcpcb`
4. **Re-route** - `kct route boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb -o ./routed.kicad_pcb --timeout 240`
5. **Export Files** - generate Gerbers, BOM, and CPL via `kct export`

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

- [examples/01-schematic-analysis](../../examples/01-schematic-analysis/) - Parse existing schematics
- [examples/02-bom-generation](../../examples/02-bom-generation/) - Extract BOM from designs
- [examples/03-drc-checking](../../examples/03-drc-checking/) - Validate against manufacturer rules
- [examples/04-autorouter](../../examples/04-autorouter/) - PCB autorouting strategies
