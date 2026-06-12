---
title: "stm32_devboard_routed"
subtitle: "Design Report"
author: "kicad-tools 0.13.0"
date: "Rev 1 | 2026-06-12 | jlcpcb"
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
| Footprints | 17 (15 SMD, 2 THT, 0 other) |
| Nets | 12 |
| Traces | 182 segments |
| Vias | 28 |
| Board Size | 60.0 x 40.0 mm |

## Design Overview

### Theory of Operation

STM32F103C8 Development Board

End-to-end design example

Demonstrates circuit blocks API

### Power Architecture

**Power Rails**: +3V3, +5V, GND, PWR_FLAG

| Regulator | Device |
|-----------|--------|
| U1 | AMS1117-3.3 |

## Assembly Notes

1 fine-pitch component; 1 polarized component

- **Fine-pitch components**: 1 (U2)
- **Polarized components**: 1 -- check orientation markings

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
| 100nF | C_0805_2012Metric | 5 | C3, C12, C13, C14, C15 |
| 10uF | C_0805_2012Metric | 2 | C1, C2 |
| 20pF | C_0805_2012Metric | 2 | C10, C11 |
| 4.7uF | C_0805_2012Metric | 1 | C16 |
| USER | LED_0805_2012Metric | 1 | D1 |
| SWD-6 | PinHeader_1x06_P2.54mm_Vertical | 1 | J1 |
| 10k | R_0805_2012Metric | 1 | R2 |
| 330R | R_0805_2012Metric | 1 | R1 |
| AMS1117-3.3 | SOT-223-3_TabPin2 | 1 | U1 |
| STM32F103C8T6 | LQFP-48_7x7mm_P0.5mm | 1 | U2 |
| 8MHz | Crystal_HC49-4H_Vertical | 1 | Y1 |


\newpage

## DRC Status

| Metric | Count |
|--------|-------|
| Errors | 4 |
| Warnings | 0 |
| Blocking | 4 |

**Status**: FAIL
### Violations by Type

| Violation Type | Count |
|----------------|-------|
| via_in_pad | 4 |
| connectivity | 1 |


\newpage

## Manufacturing Readiness

**Verdict**: NOT_READY

### Action Items

- **[CRITICAL]** Fix 4 blocking DRC violations (via_in_pad (4))
- **[CRITICAL]** Increase min via drill: 0.150mm < 0.300mm required
- **[OPTIONAL]** Verify zone fill in KiCad for 3 zone-connected nets


\newpage

## Routing Status

| Metric | Value |
|--------|-------|
| Signal Net Completion | 100.0% (9/9) |
| Overall Completion | 91.7% |
| Complete Nets | 11 / 12 |
| Zone-Connected Nets | 3 |
| Incomplete Nets | 1 |
| Unconnected Pads | 3 |

### Zone-Connected Nets

- +3.3V
- +5V
- GND

### Incomplete Nets

- GND


## Cost Estimate

| Metric | Per Board (estimated) |
|--------|-------|
| PCB Fabrication | ~0.88 USD |
| Components (estimated) | ~1.36 USD |
| Assembly (estimated) | ~2.02 USD |
| **Total (estimated)** | **~4.26 USD** |
| Batch Quantity | 5 |
| Batch Total (estimated) | ~21.3 USD |

