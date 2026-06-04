---
title: "usb_joystick_routed"
subtitle: "Design Report"
author: "kicad-tools 0.13.0"
date: "Rev 1 | 2026-06-04 | jlcpcb"
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
| Footprints | 12 (0 SMD, 0 THT, 12 other) |
| Nets | 16 |
| Traces | 96 segments |
| Vias | 22 |
| Board Size | 60.0 x 40.0 mm |

## Design Overview

### Theory of Operation

USB Joystick Controller

USB game controller with analog joystick

Demonstrates autolayout functionality

### Communication Interfaces

| Protocol | Signals |
|----------|---------|
| USB | USB_D+, USB_D- |

### Power Architecture

**Power Rails**: +5V, GND, PWR_FLAG

## Assembly Notes

1 fine-pitch component

- **Fine-pitch components**: 1 (U1)

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
| 100nF |  | 4 | C1, C2, C3, C4 |
| Joystick |  | 1 | J2 |
| USB-C |  | 1 | J1 |
| Button |  | 4 | SW1, SW2, SW3, SW4 |
| MCU |  | 1 | U1 |
| 16MHz |  | 1 | Y1 |


\newpage

## DRC Status

| Metric | Count |
|--------|-------|
| Errors | 9 |
| Warnings | 0 |
| Blocking | 9 |

**Status**: FAIL
### Violations by Type

| Violation Type | Count |
|----------------|-------|
| via_in_pad | 6 |
| clearance_segment_via | 1 |
| clearance_pad_via | 1 |
| connectivity | 1 |
| diffpair_clearance_intra | 1 |


\newpage

## Manufacturing Readiness

**Verdict**: NOT_READY

### Action Items

- **[CRITICAL]** Fix 9 blocking DRC violations (via_in_pad (6), clearance_segment_via (1), clearance_pad_via (1))
- **[OPTIONAL]** Verify zone fill in KiCad: 1 nets appear incomplete but may be connected via zone fills
- **[OPTIONAL]** Verify zone fill in KiCad for 3 zone-connected nets


\newpage

## Routing Status

| Metric | Value |
|--------|-------|
| Signal Net Completion | 92.3% (12/13) |
| Overall Completion | 93.8% |
| Complete Nets | 15 / 16 |
| Zone-Connected Nets | 3 |
| Incomplete Nets | 1 |
| Unconnected Pads | 1 |

### Zone-Connected Nets

- GND
- VBUS
- VCC

### Unrouted Signal Nets

- USB_D+

### Unrouted Signal Nets

- USB_D+


## Cost Estimate

| Metric | Per Board (estimated) |
|--------|-------|
| PCB Fabrication | ~0.88 USD |
| Components (estimated) | ~1.31 USD |
| Assembly (estimated) | ~1.98 USD |
| **Total (estimated)** | **~4.17 USD** |
| Batch Quantity | 5 |
| Batch Total (estimated) | ~20.87 USD |

