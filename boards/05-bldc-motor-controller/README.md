# BLDC Motor Controller

Three-phase brushless DC motor controller for validating kicad-tools thermal analysis, zone generation, and high-current routing capabilities.

## Quick Start

```bash
# Build schematic (PCB layout not yet implemented)
kct build boards/05-bldc-motor-controller --step schematic

# Or run directly
uv run python boards/05-bldc-motor-controller/design.py
```

> **Status**: Schematic generation implemented. PCB layout and routing pending.

## Overview

This board drives a 3-phase BLDC motor with:

- **Input**: 12-24V DC, up to 15A
- **Output**: 3-phase to motor, 10A continuous per phase
- **Control**: STM32G4 MCU with hardware PWM
- **Feedback**: Hall sensor inputs, phase current sensing

## Block Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  12-24V ──┬── Buck ── 5V ── LDO ── 3.3V ── MCU                 │
│           │           │                      │                  │
│           │           └─── Gate Driver ◄─────┘                  │
│           │                    │                                │
│           │            ┌───────┴───────┐                        │
│           │            │   HS    HS    HS  │  Half-bridges     │
│           └────────────┤   LS    LS    LS  │  (6 MOSFETs)      │
│                        │   │     │     │   │                    │
│                        │  Shunt Shunt Shunt│  Current sense    │
│                        └───┴─────┴─────┴───┘                    │
│                             │     │     │                       │
│                             U     V     W ─── Motor             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Components (~42 total)

| Section | Key Components | Count |
|---------|----------------|-------|
| Power Input | J1 screw terminal, F1 fuse, D1 TVS, C1-C3 bulk caps | 5 |
| 5V Supply | U1 LM2596, L1 inductor, D2 Schottky, C4-C5 | 5 |
| 3.3V Supply | U2 AMS1117, C6-C7 | 3 |
| MCU | U3 STM32G431, C8-C11 bypass, Y1 crystal | 6 |
| Gate Driver | U4 DRV8301, C12-C17 bootstrap/bypass | 8 |
| Power Stage | Q1-Q6 MOSFETs (3 half-bridges) | 6 |
| Current Sense | R1-R3 shunts, U5 amplifier, C18 | 5 |
| Connectors | J2 motor, J3 hall, J4 SWD, J5 aux | 4 |

## Design Challenges

This board exercises kicad-tools capabilities that simpler boards don't:

### 1. Thermal Management

- 6 power MOSFETs dissipating 1-2W each at full load
- Requires thermal vias under each MOSFET
- Ground plane for heat spreading
- Tests `ThermalAnalyzer` hotspot detection

### 2. High-Current Traces

- Motor phase traces carry 10A continuous
- Requires 2mm+ trace width or polygon pours
- Power input traces carry 15A
- Tests net class differentiation

### 3. Zone Generation

- Ground plane on bottom layer
- Motor power island on top layer
- Thermal relief patterns
- Tests `ZoneGenerator` API

### 4. Multiple Power Domains

- VMOTOR: 12-24V (motor power)
- VDD_5V: 5V (gate drivers)
- VDD_3V3: 3.3V (MCU, logic)
- Requires careful power routing

### 5. Mixed Signal Routing

- High-side gate drive (bootstrap)
- Low-noise current sense signals
- Fast PWM switching nodes
- Separation between power and signal

## Schematic Organization

The schematic is organized into functional sections:

```
┌─────────────────────────────────────────────────────────────────┐
│                         TITLE BLOCK                             │
├───────────────────┬───────────────────┬─────────────────────────┤
│   POWER INPUT     │   POWER SUPPLY    │         MCU             │
│   (12-24V DC)     │   (Buck + LDO)    │     (STM32G431)         │
├───────────────────┴───────────────────┴─────────────────────────┤
│                      GATE DRIVER (DRV8301)                      │
├─────────────────────────────────────────────────────────────────┤
│   PHASE A         │    PHASE B        │      PHASE C            │
│   (HS + LS FET)   │    (HS + LS FET)  │      (HS + LS FET)      │
├─────────────────────────────────────────────────────────────────┤
│                    CURRENT SENSING                              │
├─────────────────────────────────────────────────────────────────┤
│   CONNECTORS: Motor Output, Hall Sensors, Debug, Power         │
└─────────────────────────────────────────────────────────────────┘
```

## PCB Layout Guidelines

### Power Stage Placement

```
        Motor Connector (J2)
             │ │ │
        ┌────┴─┴─┴────┐
        │ Q1   Q3   Q5 │  High-side MOSFETs
        │ Q2   Q4   Q6 │  Low-side MOSFETs
        │ R1   R2   R3 │  Shunt resistors
        └──────────────┘
             │
        Input Connector (J1)
```

### Thermal Via Pattern

Each MOSFET pad should have thermal vias:
```
┌─────────────────┐
│  ● ● ● ● ● ●   │  ● = thermal via (0.3mm drill)
│  ● ● ● ● ● ●   │  Min 6 vias per MOSFET
│  ● ● ● ● ● ●   │  Connected to ground plane
└─────────────────┘
```

### Trace Width Requirements

| Net Class | Min Width | Current |
|-----------|-----------|---------|
| Motor Phase | 2.0mm | 10A |
| Power Input | 2.5mm | 15A |
| Gate Drive | 0.4mm | 0.5A |
| Signal | 0.2mm | mA |

## Testing kicad-tools Features

After generating the board, run these analyses:

```bash
# Thermal analysis
kct analyze thermal output/bldc_controller.kicad_pcb

# Check net classes
kct analyze nets output/bldc_controller.kicad_pcb

# Validate DRC with 2oz copper
kct check output/bldc_controller_routed.kicad_pcb --copper-weight 2oz
```

## Future Enhancements

- [ ] Add regenerative braking support
- [ ] Add encoder input (differential)
- [ ] Add CAN bus interface
- [ ] 4-layer version with dedicated power planes

## Related Examples

- [examples/07-design-feedback/thermal_demo.py](../../examples/07-design-feedback/thermal_demo.py) - Thermal analysis API
- [boards/04-stm32-devboard](../04-stm32-devboard/) - MCU schematic patterns
