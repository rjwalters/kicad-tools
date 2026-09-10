# BLDC Motor Controller

Three-phase brushless DC motor controller for validating kicad-tools thermal analysis, zone generation, and high-current routing capabilities.

## Quick Start

```bash
# Build schematic + PCB + auto-route in one shot
uv run python boards/05-bldc-motor-controller/design.py
```

> **Status**: Schematic, PCB layout (with STM32G431K8Tx MCU + complete
> DRV8301 QFN-56 footprint) and autorouting are all implemented.  After
> regeneration, every net has at least two pads so the autorouter can
> attempt full connectivity.  **All three motor-phase nets (PHASE_A,
> PHASE_B, PHASE_C) are now routed** on the committed artifact as 2.0mm
> high-current trees, added surgically (no regen; `design.py` unchanged)
> per issues #3766/#3906/#3775.  The completing increment (#3775)
> **relocated the C12-C14 bootstrap caps** from the far-west column to
> each phase's MOSFET-column node (C12 -> (14.5,63) by Q1, C13 -> (29,73)
> by Q3, C14 -> (46.5,64.5) by Q5), which shortened BST_A/B/C to local
> runs and cleared the y=59 BST_A wall that had blocked PHASE_B's
> corridor.  PHASE_B escapes U3.41 (F.Cu neck -> In2.Cu, HS/LS phase node
> Q3.3<->Q4.2 -> deep-south lane -> J2.2); PHASE_C escapes the tight
> 0.45mm U3.36 channel via a short F.Cu stub to a tagged micro-via that
> drops just north of the GATE_DRV_CH diagonal into a clear In2.Cu column
> (Q1.3<->Q2.2 -> deep lane -> J2.1).  All new copper is 45-legal
> (`tests/test_fleet_45_census.py` passes).  **Blocking signal nets: 6 ->
> 0** for PHASE; the U3-south current-sense cluster is now down to three
> open nets.
>
> **ISENSE_B+ is now routed** (artifact-first, #3766, EE-unblocked
> 2026-07-09): U3.30 escapes south on F.Cu to a through-via at (17.55,
> 55.55) into the open west In1.Cu field, which carries the spine down to
> the R11.1 shunt tap (via adjacent-north of the pad, F.Cu into the pad
> metal) and branches at (17.55,66.99) on a 45deg diagonal to Q4.3; the
> north leg to U3.26 runs on In2.Cu (crossing the PHASE_A In1 wall on a
> different layer, per the Kelvin rule "no same-layer PHASE crossing").
> No part moves (escalation level 1) and no ripped copper.  Referee
> (`kicad-cli pcb drc --refill-zones`) adds 0 violations; zones refilled;
> 45-census clean.
>
> **Three ISENSE nets remain open** (`ISENSE_A-`, `ISENSE_B-`,
> `ISENSE_C-`): each has a U3-south pin (A-: U3.33/U3.44; B-: U3.39; C-:
> U3.34) that cannot via-escape without moving the adjacent GATE_DRV_CH /
> GATE_CL / PHASE_C copper -- the all-layer via window directly south of
> the 0.5mm-pitch sense pins fits only three of the five sense pins even
> after ripping all six ISENSE nets.  Completing them requires escalation
> **level 2** (re-dress GATE_CL U3.35->Q2.1 + GATE_DRV_CH U3.37->R20.1 so
> they vacate the x19-20,y55-58 cells).  Tracked on #3766 with the exact
> escape geometry; the negotiated auto-router does not converge on this
> 0.5mm-pitch band, so it is a hand-finish task.

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

## Components (~46 total)

| Section | Key Components | Count |
|---------|----------------|-------|
| Power Input | J1 screw terminal, F1 fuse, D1 TVS, C1-C2 bulk caps | 5 |
| 5V Supply | U1 LM2596, L1 inductor, D2 Schottky, C3-C4 | 5 |
| 3.3V Supply | U2 AMS1117, C5-C6 | 3 |
| MCU | U10 STM32G431K8Tx (LQFP-32), C7-C9 bypass, Y1 8MHz crystal, C10-C11 load caps | 7 |
| Gate Driver | U3 DRV8301 (QFN-56), C12-C14 bootstrap, C15-C16 bypass | 6 |
| Power Stage | Q1-Q6 IRLZ44N MOSFETs (3 half-bridges) | 6 |
| Current Sense | R10-R12 5mR 2512 shunts | 3 |
| LEDs | D3-D4 status/power, R3-R4 1k limiter | 4 |
| Connectors | J1 power, J2 motor, J3 hall, J4 SWD | 4 |
| Mechanical | 4x M3 mounting holes | 4 |

## Design Challenges

This board exercises kicad-tools capabilities that simpler boards don't:

### 1. Thermal Management

- 6 power MOSFETs dissipating 1-2W each at full load
- Requires thermal vias under each MOSFET
- Ground plane for heat spreading
- Tests `ThermalAnalyzer` hotspot detection

### 2. High-Current Traces

- Motor phase traces carry 10A continuous
- **PHASE_A/B/C connectivity is placement-bound (issue #3766).** They are
  classified `HIGH_CURRENT_SIGNAL` (`is_pour_net=False`, "phase outputs
  must NOT be poured"), so a polygon pour is not the right tool -- and the
  FET->motor phase pads are scattered across the layout, so a bounding-box
  pour island would span the whole board and short against the rail pours.
  Routing them as traces *does* connect PHASE_A/B/C, but on the current
  70x90 mm placement the extra high-current traces consume the U3-south
  escape channels the ISENSE / PWM / GATE_DRV / BST nets also need: two
  full seed-7 regens measured 9 blocking signal nets (vs the committed 7),
  i.e. fixing PHASE breaks ~4 previously-complete nets.  Closing PHASE
  cleanly therefore needs a targeted U3-south / power-stage relayout
  (tracked as a follow-up) rather than a recipe change.
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

### Debug Header (J4) Placement

J4 (SWD-6) stays in the top-right corner at board offset (65, 22).
Issue #3424 proposed moving it east of the MCU because the four SWD
nets (SWDIO/SWCLK/SWO/NRST) were unrouteable on every 4-layer
configuration tested at curation time, but the router grace-pass fix
(#3452/#3466) resolved that at the original position before the move
landed. A/B measurements at the production recipe (4L, cpp backend,
jlcpcb-tier1, seed 42, 900s) show the corner position routes all four
SWD nets at 28/35 reach, while every relocation candidate — (72, 50),
(72, 45), and (55, 47) — strands NRST and drops reach to 27/35: NRST
leaves U10's west edge, and from there the empty NE quadrant is the
only uncongested corridor to a header. Do not move J4 east or south
without re-measuring reach at the production recipe (measurement log
in issue #3424); the (55, 50) candidate additionally overlaps R31's
pad and the y=54-56 HALL routing corridor.

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
