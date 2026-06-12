---
title: "diffpair_test_routed"
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
| Layers | 4 copper (F.Cu, In1.Cu, In2.Cu, B.Cu) |
| Footprints | 7 (5 SMD, 2 THT, 0 other) |
| Nets | 26 |
| Traces | 936 segments |
| Vias | 172 |
| Board Size | 100.0 x 80.0 mm |

## Design Overview

### Theory of Operation

Differential Pair Test Board

Multi-protocol HSDI regression testbench

Epic #2556 Phase 4L (issue #2658)

### Communication Interfaces

| Protocol | Signals |
|----------|---------|
| UART | PCIE_RX-, USB3_TX2+ |
| USB | USB2_D+, USB2_D-, VBUS_USB |

### Power Architecture

**Power Rails**: PWR_FLAG

## Assembly Notes

4 fine-pitch components

- **Fine-pitch components**: 4 (U4, U2, U1, U3)

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
| FFC4 | FFC_4P_0.5mm | 1 | J4 |
| MiniPCIe | PCIE_Mini_Edge | 1 | J3 |
| USB-C | USB_C_Receptacle_USB2.0 | 1 | J1 |
| BGA49_USB3 | BGA-49_5.0x5.0mm_Layout7x7_P0.5mm | 1 | U2 |
| QFN24_MIPI | QFN-24-1EP_4x4mm_P0.5mm | 1 | U4 |
| QFN32_USB2 | QFN-32-1EP_5x5mm_P0.5mm | 1 | U1 |
| QFP48_PCIe | LQFP-48_7x7mm_P0.5mm | 1 | U3 |


\newpage

## DRC Status

| Metric | Count |
|--------|-------|
| Errors | 0 |
| Warnings | 0 |
| Blocking | 0 |

**Status**: PASS


\newpage

## Manufacturing Readiness

**Verdict**: NOT_READY

### Action Items

- **[CRITICAL]** Increase min trace width: 0.100mm < 0.102mm required
- **[OPTIONAL]** Verify zone fill in KiCad for 5 zone-connected nets


\newpage

## Routing Status

| Metric | Value |
|--------|-------|
| Signal Net Completion | 100.0% (21/21) |
| Overall Completion | 92.3% |
| Complete Nets | 24 / 26 |
| Zone-Connected Nets | 5 |
| Incomplete Nets | 2 |
| Unconnected Pads | 15 |

### Zone-Connected Nets

- +1V2
- +1V8
- +3V3
- GND
- VBUS_USB

### Incomplete Nets

- +1V2
- GND


## Cost Estimate

| Metric | Per Board (estimated) |
|--------|-------|
| PCB Fabrication | ~3.6 USD |
| Components (estimated) | ~2.3 USD |
| Assembly (estimated) | ~2.05 USD |
| **Total (estimated)** | **~7.95 USD** |
| Batch Quantity | 5 |
| Batch Total (estimated) | ~39.74 USD |

