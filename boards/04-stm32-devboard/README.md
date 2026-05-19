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

As of the 2026-05-11 audit, board 04 routes **8/9 nets (89%)** with two residuals
tracked as upstream issues — both with concrete, documented root causes:

| Residual | Pads / nets affected | Tracking issue | Status |
|---|---|---|---|
| OSC_OUT 2/3 pad completion | Y1.2 + C11.1 route; U2.6 (LQFP-48 0.5mm-pitch pin) defers | [#2695](https://github.com/rjwalters/kicad-tools/issues/2695) | In-pad via escape for fine-pitch LQFP/QFP (extends the #2605/#2608 pattern from 0.65mm SSOP to 0.5mm QFP) |
| 5 ImpedanceRule errors on SWCLK | 50Ω target on a 2-layer stackup requires ~2.812mm width on F.Cu | [#2696](https://github.com/rjwalters/kicad-tools/issues/2696) | Either upgrade board 04 to 4-layer (Option A) or suppress validator's default `*CLK*` 50Ω spec for 2-layer hobbyist boards (Option C) |
| U2.8 stranded GND pad (17/18 stitched) | LQFP-48 west-side VSS pin; OSC_OUT B.Cu escape stub at `(126.8375, 121.75..122.4)` blocks any micro-via at U2.8 `(126.8375, 122.75)` (gap=0.10mm vs jlcpcb-tier1 min 0.20mm) | [#3075](https://github.com/rjwalters/kicad-tools/issues/3075) / [#3080](https://github.com/rjwalters/kicad-tools/issues/3080) | Advisory only: connectivity rule is in `DRCChecker.ADVISORY_RULE_IDS` (filtered from CI gate per #3074). The other 3 of 4 VSS pads (U2.23, U2.35, U2.47) are stitched, so the MCU VSS rail is bonded to plane through three independent paths. Resolution depends on extending the PR #3079 surface-stub channel-fit necking from strict-mode to the default escape path (router-side, tracked under #3080), or on the #2834 OSC_OUT escape rework. |

Board 04's `generate_design.py` does **not** set `target_single_impedance` on
any net class. The 5 ImpedanceRule errors come from
`ImpedanceRule._get_default_specs()` matching `SWCLK` via the regex `.*CLK.*`
and asserting a 50Ω default. Per PR #2680's caveat, that target is physically
infeasible on the 2-layer JLCPCB default stackup.

Both #2695 and #2696 must resolve before board 04 reaches 0 DRC + 0 ERC.
Until then, board 04 is **partial-mfg-ready**: the routed PCB is structurally
valid except for the OSC_OUT escape gap, and the impedance errors reflect a
spec-vs-stackup mismatch rather than a wiring or sizing bug.

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
