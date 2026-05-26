---
title: "bldc_controller_routed"
subtitle: "Design Report"
author: "kicad-tools 0.13.0"
date: "Rev 1 | 2026-05-26 | jlcpcb"
geometry: "margin=1in"
fontsize: 11pt
colorlinks: true
header-includes:
  - \usepackage{longtable}
  - \usepackage{booktabs}
  - \usepackage{array}
  - \usepackage{float}
---

## Board Summary

| Property | Value |
|----------|-------|
| Layers | 2 copper (F.Cu, B.Cu) |
| Footprints | 55 (0 SMD, 0 THT, 55 other) |
| Nets | 40 |
| Traces | 190 segments |
| Vias | 38 |
| Board Size | 80.0 x 100.0 mm |

## Design Overview

### Theory of Operation

BLDC Motor Controller

3-Phase Brushless DC Motor Driver

Thermal analysis and high-current routing demo

### Power Architecture

**Power Rails**: +24V, +3V3, +5V, GND, PWR_FLAG

| Regulator | Device |
|-----------|--------|
| U1 | LM2596-5.0 |
| U2 | AMS1117-3.3 |

## Assembly Notes

1 fine-pitch component; 4 polarized components

- **Fine-pitch components**: 1 (U10)
- **Polarized components**: 4 -- check orientation markings

## ERC Status

| Metric | Count |
|--------|-------|
| Errors | 0 |
| Warnings | 0 |

**Status**: SKIPPED -- ERC skipped by user request


\newpage

## Bill of Materials

| Value | Package | Qty | References |
|-------|---------|-----|------------|
| 100nF | C_0805_2012Metric | 7 | C2, C7, C8, C12, C13, C14, C15 |
| 100uF |  | 1 | C3 |
| 10nF |  | 3 | C30, C31, C32 |
| 10uF | C_0805_2012Metric | 3 | C5, C6, C16 |
| 20pF |  | 2 | C10, C11 |
| 220uF |  | 1 | C4 |
| 4.7uF | C_0805_2012Metric | 1 | C9 |
| 470uF | C_0805_2012Metric | 1 | C1 |
| PWR |  | 1 | D3 |
| SMBJ24A | D_SMA | 1 | D1 |
| SS34 |  | 1 | D2 |
| STATUS |  | 1 | D4 |
| 15A | Fuse_1206_3216Metric | 1 | F1 |
| Hall Sensors | PinHeader_1x05_P2.54mm_Vertical | 1 | J3 |
| Motor Output | PinHeader_1x03_P2.54mm_Vertical | 1 | J2 |
| Power Input | PinHeader_1x02_P2.54mm_Vertical | 1 | J1 |
| SWD-6 |  | 1 | J4 |
| 33uH |  | 1 | L1 |
| IRLZ44N |  | 6 | Q1, Q2, Q3, Q4, Q5, Q6 |
| 10k |  | 3 | R30, R31, R32 |
| 1k |  | 2 | R3, R4 |
| 22 |  | 3 | R20, R21, R22 |
| 5mR |  | 3 | R10, R11, R12 |
| AMS1117-3.3 | SOT-223-3_TabPin2 | 1 | U2 |
| DRV8301 |  | 1 | U3 |
| LM2596-5.0 |  | 1 | U1 |
| STM32G431K8Tx | LQFP-32_7x7mm_P0.8mm | 1 | U10 |
| 8MHz |  | 1 | Y1 |


\newpage

## DRC Status

| Metric | Count |
|--------|-------|
| Errors | 9 |
| Warnings | 56 |
| Blocking | 9 |

**Status**: FAIL
### Violations by Type

| Violation Type | Count |
|----------------|-------|
| pad_grid | 56 |
| connectivity | 23 |
| clearance_segment_segment | 5 |
| clearance_segment_via | 3 |
| clearance_pad_segment | 1 |


\newpage

## Manufacturing Readiness

**Verdict**: NOT_READY

### Action Items

- **[CRITICAL]** Fix 9 blocking DRC violations (clearance_segment_segment (5), clearance_segment_via (3), clearance_pad_segment (1))
- **[OPTIONAL]** Verify zone fill in KiCad: 22 nets appear incomplete but may be connected via zone fills
- **[OPTIONAL]** Verify zone fill in KiCad for 5 zone-connected nets
- **[OPTIONAL]** Review 56 DRC warnings


\newpage

## Routing Status

| Metric | Value |
|--------|-------|
| Signal Net Completion | 37.1% (13/35) |
| Overall Completion | 42.5% |
| Complete Nets | 17 / 40 |
| Zone-Connected Nets | 5 |
| Incomplete Nets | 23 |
| Unconnected Pads | 80 |

### Zone-Connected Nets

- +3.3V
- +5V
- GND
- PWR_LED
- VMOTOR

### Unrouted Signal Nets

- GATE_AL
- GATE_BL
- GATE_CL
- GATE_DRV_AH
- GATE_DRV_CH
- HALL_A
- HALL_B
- HALL_C
- ISENSE_A+
- ISENSE_A-
- ISENSE_B+
- ISENSE_B-
- ISENSE_C-
- OSC_OUT
- PHASE_A
- PHASE_B
- PHASE_C
- PWM_AL
- PWM_BH
- PWM_BL
- PWM_CH
- PWM_CL

### Unrouted Signal Nets

- GATE_AL
- GATE_BL
- GATE_CL
- GATE_DRV_AH
- GATE_DRV_CH
- HALL_A
- HALL_B
- HALL_C
- ISENSE_A+
- ISENSE_A-
- ISENSE_B+
- ISENSE_B-
- ISENSE_C-
- OSC_OUT
- PHASE_A
- PHASE_B
- PHASE_C
- PWM_AL
- PWM_BH
- PWM_BL
- PWM_CH
- PWM_CL


## Cost Estimate

| Metric | Per Board (estimated) |
|--------|-------|
| PCB Fabrication | ~2.0 USD |
| Components (estimated) | ~3.2 USD |
| Assembly (estimated) | ~2.35 USD |
| **Total (estimated)** | **~7.56 USD** |
| Batch Quantity | 5 |
| Batch Total (estimated) | ~37.78 USD |

