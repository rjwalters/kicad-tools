---
title: "usb_joystick_routed"
subtitle: "Design Report"
author: "kicad-tools 0.20.0"
date: "Rev 1 | 2026-09-10 | jlcpcb-tier1"
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
| Footprints | 38 (37 SMD, 1 THT, 0 other) |
| Nets | 27 |
| Traces | 2048 segments |
| Vias | 151 |
| Board Size | 80.0 x 60.0 mm |

## Stackup

| Layer | Type | Thickness (mm) | Material |
|-------|------|----------------|----------|
| F.Cu | copper | 0.035 | -- |
| dielectric 1 | prepreg | 0.2104 | 7628 |
| In1.Cu | copper | 0.0152 | -- |
| dielectric 2 | core | 1.065 | FR4 |
| In2.Cu | copper | 0.0152 | -- |
| dielectric 3 | prepreg | 0.2104 | 7628 |
| B.Cu | copper | 0.035 | -- |

## Design Overview

### Theory of Operation

USB joystick controller — ATmega32U4

USB bus powered, 8MHz crystal, AVR ISP programming

GPIO and ADC map is fixed to Microchip TQFP-44 pinout

### Communication Interfaces

| Protocol | Signals |
|----------|---------|
| SPI | ISP_MISO, ISP_MOSI, ISP_SCK |
| USB | USB_D+, USB_D-, VBUS |

### Power Architecture

**Power Rails**: PWR_FLAG

## Assembly Notes

1 fine-pitch component

- **Fine-pitch components**: 1 (U1)

## Hand-Solder (THT) Components

The following 1 through-hole component is **excluded from the SMT pick-and-place file** and must be hand-soldered (or wave/selective-soldered) after SMT assembly. It appears in the BOM for sourcing.

| Value | Package | Qty | References |
|-------|---------|-----|------------|
| USB4085-GF-A | USB_C_Receptacle_GCT_USB4085 | 1 | J1 |

## ERC Status

| Metric | Count |
|--------|-------|
| Errors | 0 |
| Warnings | 0 |

**Status**: PASS — independent native ERC: zero errors and warnings; see native-erc.json.


\newpage

## Schematic Overview

### Schematic: usb_joystick

![Schematic: usb_joystick](images/schematic_usb_joystick.png)


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

| Value | Package | Qty | References | MPN | LCSC |
|-------|---------|-----|------------|-----|------|
| 100nF | C_0603_1608Metric | 7 | C1, C2, C3, C4, C5, C7, C13 | GRM188R71C104KA01D | C45000 |
| 10nF | C_0603_1608Metric | 2 | C10, C11 | GRM188R71H103KA01D | C77053 |
| 15pF | C_0603_1608Metric | 2 | C8, C9 | GRM1885C1H150JA01D | C71651 |
| 1uF | C_0603_1608Metric | 1 | C6 | GRM188R71A105KA61D | C97888 |
| 4.7uF | C_0603_1608Metric | 1 | C12 | GRM188R61A475KE15D | C71633 |
| 350mA | Fuse_1206_3216Metric | 1 | F1 | 1206L035/16YR | C126817 |
| AVR ISP | Samtec_TSM-103-01-T-DV | 1 | J3 | TSM-103-01-T-DV-P-TR | C21285200 |
| Joystick 5V X Y SW | JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal | 1 | J2 | S5B-PH-SM4-TB(LF)(SN) | C265104 |
| USB4085-GF-A | USB_C_Receptacle_GCT_USB4085 | 1 | J1 | USB4085-GF-A | C7095263 |
| 10k | R_0603_1608Metric | 7 | R5, R6, R12, R13, R14, R15, R16 | RC0603FR-0710KL | C98220 |
| 1k | R_0603_1608Metric | 2 | R10, R11 | RC0603FR-071KL | C22548 |
| 22 | R_0402_1005Metric | 2 | R3, R4 | RC0402FR-0722RL | C114765 |
| 5.1k | R_0603_1608Metric | 2 | R1, R2 | RC0603FR-075K1L | C105580 |
| TL3342F160QG | SW_SPST_TL3342 | 5 | SW1, SW2, SW3, SW4, SW5 | TL3342F160QG | C2886898 |
| ATMEGA32U4-AU | TQFP-44_10x10mm_P0.8mm | 1 | U1 | ATMEGA32U4-AU | C44854 |
| USBLC6-2SC6 | SOT-23-6 | 1 | U2 | USBLC6-2SC6 | C7519 |
| 8MHz | Crystal_SMD_3225-4Pin_3.2x2.5mm | 1 | Y1 | ECS-80-12-33-JGN-TR | C2442665 |


\newpage

## DRC Status

| Metric | Count |
|--------|-------|
| Errors | 0 |
| Warnings | 14 |
| Blocking | 0 |

**Status**: PASS
### Violations by Type

| Violation Type | Count |
|----------------|-------|
| hole_to_hole_clearance | 14 |


\newpage

## Reviewed Factory Verification

The generic report above uses a conservative 0.50 mm pad-hole floor and reports
14 hole-spacing warnings. The selected factory process explicitly permits
0.45 mm; native project rules and the reviewed checker use that same floor.
No findings are suppressed. Fresh check-report.json records 51 evaluated rules,
zero errors/warnings, and label plus copper LVS with 129 bound pads and zero
mismatches. Independent native DRC/refill and ERC report zero violations and
zero opens. Per-net, per-layer filled copper matches the independent refill
exactly (fill-consistency.json). All 23 repaired off-angle segments are now
45-degree aligned; the USB branch skew is 0.165685 mm, within the reviewed bound.

## Manufacturing Readiness

**Verdict**: READY for the specified fabrication and assembly process.

Order the reviewed four-layer construction and Epoxy-filled & Capped POFV/VIPPO
option in manufacturing-requirements.json. The exact supplier BOM covers 37 SMT
placements; J1 is a separate through-hole assembly operation in manual-assembly-bom.csv.
The editable project includes the custom ISP footprint library and native rules.
Unchanged compiled firmware and its source are supplied with programming instructions.
Physical bring-up, USB electrical qualification, and first-article inspection
remain unperformed; this is not a USB certification claim.

\newpage

## Routing Status

| Metric | Value |
|--------|-------|
| Signal Net Completion | 100.0% (25/25) |
| Overall Completion | 100.0% |
| Complete Nets | 27 / 27 |
| Zone-Connected Nets | 2 |
| Incomplete Nets | 0 |
| Unconnected Pads | 0 |

### Zone-Connected Nets

- GND
- VCC


## Cost Estimate

| Metric | Per Board (estimated) |
|--------|-------|
| PCB Fabrication | ~2.96 USD |
| Components (estimated) | ~2.11 USD |
| Assembly (estimated) | ~2.16 USD |
| **Total (estimated)** | **~7.23 USD** |
| Batch Quantity | 5 |
| Batch Total (estimated) | ~36.14 USD |

