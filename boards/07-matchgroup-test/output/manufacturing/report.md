---
title: "matchgroup_test_routed"
subtitle: "Design Report"
author: "kicad-tools 0.13.0"
date: "Rev 1 | 2026-06-16 | jlcpcb-tier1"
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
| Footprints | 8 (5 SMD, 3 THT, 0 other) |
| Nets | 34 |
| Traces | 1269 segments |
| Vias | 243 |
| Board Size | 110.0 x 95.0 mm |

## Design Overview

### Theory of Operation

Match-Group Test Board

N-trace + group-of-pairs match-group regression testbench

Epic #2661 Phase 3L (issue #2724)

### Power Architecture

**Power Rails**: PWR_FLAG

## Assembly Notes

5 fine-pitch components

- **Fine-pitch components**: 5 (U2, U1, U5, U4, U3)

## ERC Status

| Metric | Count |
|--------|-------|
| Errors | 0 |
| Warnings | 0 |

**Status**: SKIPPED -- ERC skipped by user request


\newpage

## Schematic Overview

### Schematic: matchgroup_test

![Schematic: matchgroup_test](images/schematic_matchgroup_test.png)


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
| ADDR_HDR | PinHeader_1x09_P2.54mm_Vertical | 1 | J3 |
| FFC6_MIPI | FFC_6P_1.0mm | 1 | J1 |
| HDMI19 | HDMI_A_Receptacle | 1 | J2 |
| BGA49_HDMI | BGA-49_5.0x5.0mm_Layout7x7_P0.5mm | 1 | U4 |
| QFN24_MIPI | QFN-24-1EP_4x4mm_P0.5mm | 1 | U3 |
| QFN48_DDR_CTRL | QFN-48-1EP_7x7mm_P0.5mm | 1 | U1 |
| QFN48_DRAM | QFN-48-1EP_7x7mm_P0.5mm | 1 | U2 |
| QFP48_SRAM | LQFP-48_7x7mm_P0.5mm | 1 | U5 |


\newpage

## DRC Status

| Metric | Count |
|--------|-------|
| Errors | 125 |
| Warnings | 5 |
| Blocking | 125 |

**Status**: FAIL
### Violations by Type

| Violation Type | Count |
|----------------|-------|
| kicad-cli:clearance | 26 |
| clearance_segment_via | 23 |
| kicad-cli:hole_clearance | 18 |
| kicad-cli:shorting_items | 13 |
| clearance_pad_via | 12 |
| clearance_pad_segment | 10 |
| kicad-cli:solder_mask_bridge | 8 |
| clearance_segment_segment | 7 |
| silkscreen_text_height | 4 |
| kicad-cli:zones_intersect | 3 |
| clearance_via_via | 2 |
| kicad-cli:tracks_crossing | 2 |
| connectivity | 1 |
| silkscreen_over_pad | 1 |
| kicad-cli:starved_thermal | 1 |


\newpage

## Manufacturing Readiness

**Verdict**: NOT_READY

### Action Items

- **[CRITICAL]** Fix 125 blocking DRC violations (clearance_segment_via (23), clearance_pad_via (12), clearance_pad_segment (10); kicad-cli: clearance (26), hole_clearance (18), shorting_items (13))
- **[OPTIONAL]** Verify zone fill in KiCad for 3 zone-connected nets
- **[OPTIONAL]** Review 5 DRC warnings


\newpage

## Routing Status

| Metric | Value |
|--------|-------|
| Signal Net Completion | 100.0% (31/31) |
| Overall Completion | 97.1% |
| Complete Nets | 33 / 34 |
| Zone-Connected Nets | 3 |
| Incomplete Nets | 1 |
| Unconnected Pads | 6 |

### Zone-Connected Nets

- +1V2
- +1V8
- GND

### Incomplete Nets

- GND


## Cost Estimate

| Metric | Per Board (estimated) |
|--------|-------|
| PCB Fabrication | ~4.09 USD |
| Components (estimated) | ~2.8 USD |
| Assembly (estimated) | ~2.05 USD |
| **Total (estimated)** | **~8.94 USD** |
| Batch Quantity | 5 |
| Batch Total (estimated) | ~44.72 USD |

