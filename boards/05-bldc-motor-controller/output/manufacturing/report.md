---
title: "bldc_controller_routed"
subtitle: "Design Report"
author: "kicad-tools 0.14.0"
date: "Rev 1 | 2026-07-08 | jlcpcb"
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
| Layers | 4 copper (F.Cu, In1.Cu, In2.Cu, B.Cu) |
| Footprints | 55 (40 SMD, 11 THT, 4 other) |
| Nets | 52 |
| Traces | 951 segments |
| Vias | 79 |
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

## Schematic Overview

### Schematic: bldc_controller

![Schematic: bldc_controller](images/schematic_bldc_controller.png)


\newpage

## PCB Layout

![PCB Front](images/pcb_front.png)

![PCB Back](images/pcb_back.png)

### Copper

![PCB Copper](images/pcb_copper.png)

### Assembly

![Assembly](images/assembly.png)


\newpage

## Copper Layers

### F.Cu

![F.Cu](images/layer_F_Cu.png)

### In1.Cu

![In1.Cu](images/layer_In1_Cu.png)

### In2.Cu

![In2.Cu](images/layer_In2_Cu.png)

### B.Cu

![B.Cu](images/layer_B_Cu.png)


\newpage

## Bill of Materials

| Value | Package | Qty | References |
|-------|---------|-----|------------|
| 100nF | C_0805_2012Metric | 7 | C2, C7, C8, C12, C13, C14, C15 |
| 10nF | C_0805_2012Metric | 3 | C30, C31, C32 |
| 10uF | C_0805_2012Metric | 3 | C5, C6, C16 |
| 20pF | C_0805_2012Metric | 2 | C10, C11 |
| 220uF | C_0805_2012Metric | 2 | C3, C4 |
| 4.7uF | C_0805_2012Metric | 1 | C9 |
| 470uF | C_0805_2012Metric | 1 | C1 |
| PWR | LED_0805_2012Metric | 1 | D3 |
| SMBJ24A | D_SMA | 1 | D1 |
| SS34 | D_SMA | 1 | D2 |
| STATUS | LED_0805_2012Metric | 1 | D4 |
| 15A | Fuse_1206_3216Metric | 1 | F1 |
| Hall Sensors | PinHeader_1x05_P2.54mm_Vertical | 1 | J3 |
| Motor Output | PinHeader_1x03_P2.54mm_Vertical | 1 | J2 |
| Power Input | PinHeader_1x02_P2.54mm_Vertical | 1 | J1 |
| SWD-6 | PinHeader_1x06_P2.54mm_Vertical | 1 | J4 |
| 33uH | L_1210_3225Metric | 1 | L1 |
| IRLZ44N | TO-220-3_Vertical | 6 | Q1, Q2, Q3, Q4, Q5, Q6 |
| 10k | R_0805_2012Metric | 3 | R30, R31, R32 |
| 1k | R_0805_2012Metric | 2 | R3, R4 |
| 22 | R_0805_2012Metric | 3 | R20, R21, R22 |
| 5mR | R_2512_6332Metric | 3 | R10, R11, R12 |
| AMS1117-3.3 | SOT-223-3_TabPin2 | 1 | U2 |
| DRV8301 | HTSSOP-56-1EP_6.1x14mm_P0.5mm_EP3.61x6.35mm | 1 | U3 |
| LM2596-5.0 | TO-263-5_TabPin3 | 1 | U1 |
| STM32G431K8Tx | LQFP-32_7x7mm_P0.8mm | 1 | U10 |
| 8MHz | Crystal_HC49-4H_Vertical | 1 | Y1 |


\newpage

## DRC Status

| Metric | Count |
|--------|-------|
| Errors | 24 |
| Warnings | 11 |
| Blocking | 24 |

**Status**: FAIL
### Violations by Type

| Violation Type | Count |
|----------------|-------|
| via_in_pad | 20 |
| single_pad_net | 8 |
| silk_over_copper | 6 |
| copper_sliver | 3 |
| silk_edge_clearance | 2 |
| kicad-cli:hole_clearance | 2 |
| clearance_segment_via | 1 |
| kicad-cli:clearance | 1 |


\newpage

## Manufacturing Readiness

**Verdict**: NOT_READY

### Action Items

- **[CRITICAL]** Fix 24 blocking DRC violations (via_in_pad (20), clearance_segment_via (1); kicad-cli: hole_clearance (2), clearance (1))
- **[CRITICAL]** Increase min via drill: 0.150mm < 0.200mm required
- **[OPTIONAL]** Verify zone fill in KiCad for 5 zone-connected nets
- **[OPTIONAL]** Review 11 DRC warnings
- **[OPTIONAL]** Analog net: ISENSE_A+ — analog signal; noise-sensitive, avoid crossing digital signals
- **[OPTIONAL]** Analog net: ISENSE_A- — analog signal; noise-sensitive, avoid crossing digital signals
- **[OPTIONAL]** Analog net: ISENSE_B+ — analog signal; noise-sensitive, avoid crossing digital signals
- **[OPTIONAL]** Analog net: ISENSE_B- — analog signal; noise-sensitive, avoid crossing digital signals
- **[OPTIONAL]** Analog net: ISENSE_C+ — analog signal; noise-sensitive, avoid crossing digital signals
- **[OPTIONAL]** Analog net: ISENSE_C- — analog signal; noise-sensitive, avoid crossing digital signals


\newpage

## Routing Status

| Metric | Value |
|--------|-------|
| Signal Net Completion | 100.0% (39/39) |
| Overall Completion | 90.4% |
| Complete Nets | 47 / 52 |
| Zone-Connected Nets | 5 |
| Single-Pad Nets | 8 (no routing needed) |
| Incomplete Nets | 5 |
| Unconnected Pads | 53 |

### Zone-Connected Nets

- +24V
- +3V3
- +5V
- GND
- VIN

### Single-Pad Nets

8 single-pad nets (no routing needed) -- not listed individually.

### Incomplete Nets

- +24V
- +3V3
- +5V
- GND
- VIN


## Cost Estimate

| Metric | Per Board (estimated) |
|--------|-------|
| PCB Fabrication | ~3.6 USD |
| Components (estimated) | ~3.2 USD |
| Assembly (estimated) | ~2.35 USD |
| **Total (estimated)** | **~9.16 USD** |
| Batch Quantity | 5 |
| Batch Total (estimated) | ~45.78 USD |

