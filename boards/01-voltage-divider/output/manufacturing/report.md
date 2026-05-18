---
title: "voltage_divider_routed"
subtitle: "Design Report"
author: "kicad-tools 0.13.0"
date: "Rev 1 | 2026-05-18 | jlcpcb"
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
| Footprints | 4 (0 SMD, 0 THT, 4 other) |
| Nets | 3 |
| Traces | 29 segments |
| Vias | 6 |
| Board Size | 30.0 x 25.0 mm |

## Design Overview

### Theory of Operation

Voltage Divider Test

Simple 2-resistor voltage divider

5V -> 2.5V (10k/10k)

### Power Architecture

**Power Rails**: +5V, GND, PWR_FLAG

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
| IN | PinHeader_1x02_P2.54mm_Vertical | 1 | J1 |
| OUT | PinHeader_1x02_P2.54mm_Vertical | 1 | J2 |
| 10k | R_0805_2012Metric | 2 | R1, R2 |


\newpage

## DRC Status

| Metric | Count |
|--------|-------|
| Errors | 1 |
| Warnings | 0 |
| Blocking | 1 |

**Status**: FAIL
### Violations by Type

| Violation Type | Count |
|----------------|-------|
| via_in_pad | 1 |


\newpage

## Manufacturing Readiness

**Verdict**: NOT_READY

### Action Items

- **[CRITICAL]** Fix 1 blocking DRC violations (via_in_pad (1))
- **[OPTIONAL]** Verify zone fill in KiCad for 1 zone-connected nets
- **[OPTIONAL]** Add zone for GND on appropriate copper layer


\newpage

## Routing Status

| Metric | Value |
|--------|-------|
| Signal Net Completion | 66.7% (2/3) |
| Overall Completion | 66.7% |
| Complete Nets | 2 / 3 |
| Incomplete Nets | 1 |
| Unconnected Pads | 2 |

### Unrouted Signal Nets

- GND

### Unrouted Signal Nets

- GND


## Cost Estimate

| Metric | Per Board (estimated) |
|--------|-------|
| PCB Fabrication | ~0.55 USD |
| Components (estimated) | ~0.21 USD |
| Assembly (estimated) | ~1.93 USD |
| **Total (estimated)** | **~2.69 USD** |
| Batch Quantity | 5 |
| Batch Total (estimated) | ~13.44 USD |

