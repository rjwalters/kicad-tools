# 3D model coverage: demo boards today, the JLCPCB catalog next

**Date**: 2026-10-21
**Trigger**: the October site publish rendered no body for board 03 J2 (JST PH),
or for board 09 J1 (USB-C) and RSH1 (shunt), under stock KiCad 10.0.7
libraries.
**Toolchains measured**: host KiCad 10.0.7 (macOS app, Homebrew cask
symlink), and the pinned CI image
`kicad/kicad@sha256:18693567…dd3ec` (kicad-cli 10.0.6).
**Scope**: measurement and planning only. No product code changed.

## TL;DR

1. **The fleet is not at full coverage, and the existing check reports green
   on the worst board.** On the host, the in-repo boards 00–09 resolve
   **242 of 249** footprints to a body file (97%). Board 00 has **no
   `(model …)` refs at all**: it lost them in the 2026-10-08 regeneration.
   `kct render`'s model check still reports `passed`, because it only
   inspects refs that exist (`checked_models: 0`).
2. **The pinned image ships no 3D models.** `/usr/share/kicad/3dmodels` is
   absent from the 10.0.6 image; the only STEP/WRL files in it are 43 demo
   assets. So **0 of 249** footprints have a body under the toolchain that
   produces committed evidence. Every render made so far depends on the host.
3. **Half the stock footprint library points at bodies that do not exist.**
   7,337 of the 15,464 stock KiCad 10.0.7 footprints reference a STEP file
   that is in neither the macOS bundle nor upstream `kicad-packages3D`. This
   is KiCad policy (KLC F9.3), not a packaging bug. All 7 dangling refs on
   our boards are of this kind. `add-3d-models` tiers 1–3 copy them without
   checking that the file exists.
4. **Generators never emit models.** Bodies are added after the fact by
   `kct pcb add-3d-models`. No generator, recipe or pipeline calls it, so a
   regeneration silently drops them. This happened to board 00, and #6354
   reported it for board 01. The exception is board 09, which loads stock
   `.kicad_mod` files.
5. **Licenses split the sources into three classes:**
   - **KiCad stock models** (CC-BY-SA-4.0 plus the KiCad design exception)
     may be embedded in boards and manufacturing packages freely.
   - **Models we generate ourselves** are ours to ship.
   - **EasyEDA/LCSC STEP files** have no redistribution grant. The 2026-09-21
     EasyEDA Terms expressly forbid copying, distributing, and "script or
     other automated means" of downloading. So they must stay fetch-at-use,
     cache-only and never committed. Even our existing opt-in fetch tier
     (#4072) is in tension with the automated-access clause. That is an
     operator decision.
6. **Embedding works, but one piece is undocumented.** kicad-cli 10.0.7
   renders `kicad-embed://` bodies from a board-level `(embedded_files …)`
   block, which makes a board self-contained. But **kicad-cli has no embed
   command, and the 128-bit `checksum` is undocumented**: it is not MD5,
   SHA-256/16, BLAKE2b-128, XXH128 or MurmurHash3. A wrong checksum
   silently drops the body (one file) or fails the board load (two or more
   files). Embedding is therefore blocked on a clean-room spike.
7. **DRC is unaffected.** Black-box probes on both 10.0.7 and the pinned
   10.0.6 show that removing or re-pathing a `(model …)` node does **not**
   raise `lib_footprint_mismatch`, while a pad change does. Model work cannot
   trip DRC or the `lib_footprint_issues` refusal.

The plan is an epic of 8 issues (drafted separately). It covers:
- an honest coverage audit;
- dangling-ref-safe insertion;
- a pinned model set;
- making generators own their models;
- fixing every demo board;
- a C-number-keyed LCSC pipeline that respects the terms;
- parametric generation for standard families;
- embedding;
- site renders on the pinned set.

## 1. Measurement

### Method

A scratch script parsed each board's routed `.kicad_pcb` with
`kicad_tools.sexp`. For each footprint it recorded the reference, lib_id and
every `(model …)` path. It expanded path variables the same way
`kct render` does (`_configured_model_variables` plus `_render_env`:
`KICADn_3DMODEL_DIR` and then the app bundle, `KCT_LCSC_3D_DIR`, and
`KIPRJMOD`), and checked that the file exists.

- **Host libraries.** `KICAD10_3DMODEL_DIR` is unset and the user
  `kicad_common.json` has no path vars, so every ref resolves to
  `/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels`. That
  directory holds 7,252 STEP files, 3.2 GB, and no WRL. Homebrew's
  `kicad-cli` is a symlink into the same app bundle.
- **Pinned config.** `src/kicad_tools/data/kicad-config/10.0/` holds only
  `fp-lib-table` and `sym-lib-table`. It defines no 3D model path, so it
  neither helps nor hurts resolution.
- **Pinned image.** A `docker run` of the CI digest showed that
  `/usr/share/kicad/` contains `footprints`, `symbols` and `demos`, but no
  `3dmodels`. `find / -name '*.step' -o -name '*.wrl'` found 43 files, all
  under `demos/`. No `${KICAD10_3DMODEL_DIR}` ref can resolve there.
- **LCSC cache.** `~/.cache/kicad-tools/lcsc-3d/` does not exist on this
  host. No committed board currently carries a `${KCT_LCSC_3D_DIR}` ref. The
  board 03 and 06 `lcsc_models.json` sidecars name lib_ids
  (`Module:Joystick_Analog`, `Connector_PCIE:PCIE_Mini_Edge`) that are no
  longer on those boards, so they are stale.
- **Board 08** has no PCB yet: `output/` is empty.
- **External boards.** `boards/external/*` are symlinks to sibling repos.
  For chorus-test-revA, the latest `v24` file was measured. For softstart,
  both the routed rev A and the placed rev C were measured.

### Totals

"Dangling" means the footprint has a model ref, but the file it names
exists nowhere.

| Board | Footprints | With model ref | No model ref | Dangling ref | Resolved (host 10.0.7) | Resolved (pinned image 10.0.6) |
|---|---|---|---|---|---|---|
| 00-simple-led | 3 | 0 | 3 | 0 | 0 (0%) | 0 |
| 01-voltage-divider | 4 | 4 | 0 | 0 | 4 (100%) | 0 |
| 02-charlieplex-led | 19 | 19 | 0 | 0 | 19 (100%) | 0 |
| 03-usb-joystick | 38 | 38 | 0 | 1 | 37 (97%) | 0 |
| 04-stm32-devboard | 17 | 17 | 0 | 0 | 17 (100%) | 0 |
| 05-bldc-motor-controller | 42 | 42 | 0 | 0 | 42 (100%) | 0 |
| 06-diffpair-test | 36 | 36 | 0 | 0 | 36 (100%) | 0 |
| 07-matchgroup-test | 43 | 43 | 0 | 0 | 43 (100%) | 0 |
| 08-precision-acquisition | (no PCB) | - | - | - | - | - |
| 09-usbc-pd-power | 47 | 46 | 1 | 2 | 44 (94%) | 0 |
| **In-repo total (00–09)** | **249** | **245** | **4** | **3** | **242 (97%)** | **0** |
| external/chorus-test-revA (v24) | 90 | 89 | 1 | 2 | 87 (97%) | 0 |
| external/softstart (rev A routed) | 80 | 0 | 80 | 0 | 0 (0%) | 0 |
| external/softstart (rev C placed) | 137 | 136 | 1 | 1 | 135 (99%) | 0 |

**Bodies missing on the host (in-repo):**
- **Board 00:** R1, D1, J1 (no ref).
- **Board 03:** J2 (dangling).
- **Board 09:** J1 and RSH1 (dangling), L1 (custom footprint, no ref).

**Bodies missing on the host (external):**
- **chorus:** NT3 (net tie, exempt), plus Y2 and J3 (dangling).
- **softstart rev A:** every footprint. Running `add-3d-models --dry-run`
  would patch 71 of them, leave 4 mounting holes bare (exempt), and fail on
  the 5 terminal blocks and the fuse holder.
- **softstart rev C:** L1 (dangling), plus TP1 (test point, arguably exempt).

**"Resolved" is a file-existence result, not a correctness result.** Two
examples:
- Board 03 J3 (`Joystick:Samtec_TSM-103-01-T-DV`) uses a generic SMD pin
  header body as a proxy.
- chorus J4 (`PinHeader_1x05`) points at the **1x04** body. That is a wrong
  part, and it counts as resolved.

The per-part `rotate`/`offset` problems described in
[`lcsc-3d-models.md`](../guides/lcsc-3d-models.md) are likewise invisible
to this measurement.

### Why the stock refs dangle

`stock.py`, a scratch script, walked every `*.kicad_mod` in the 10.0.7
bundle. Of 15,464 footprints:
- 612 carry no model ref;
- 7,515 carry a ref that resolves;
- **7,337 carry a ref to a file that does not exist.**

The worst libraries:

| Library | Dangling / footprints |
|---|---|
| Connector_Molex | 724 / 812 |
| Package_DFN_QFN | 504 / 779 |
| Connector_JST | 400 / 558 |
| Connector_Wire | 324 / 330 |
| Connector_Samtec_HLE_SMD | 290 / 290 |
| Connector_FFC-FPC | 260 / 372 |
| Connector_Audio | 207 / 211 |
| Package_BGA | 168 / 235 |
| Inductor_SMD | 163 / 692 |
| Package_SO | 162 / 401 |

Upstream `kicad-packages3D` tag 10.0.7 also contains exactly 7,252 STEP and
0 WRL, so the macOS bundle is complete. The three files from the publish
report return 404 on master, on 10.0 and on 9.0.

KLC rule F9.3 requires that "all non-virtual footprints must have 3D model
references, even if the 3D model is missing (does not yet exist)… (It is not
required to supply a 3d model file.)" The packages3D `TODO.md` says the
library "is incomplete".

**Consequence:** for roughly half of all stock footprints, upgrading or
reinstalling KiCad will never supply the body.

<details><summary>Per-board tables (grouped by lib_id + model path; <code>$K10</code> = <code>${KICAD10_3DMODEL_DIR}</code>)</summary>

#### 00-simple-led

`boards/00-simple-led/output/simple_led_routed.kicad_pcb`: 3 footprints, 0 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| J1 | `Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical` | `(none)` | no model | - |
| D1 | `LED_THT:LED_D5.0mm` | `(none)` | no model | - |
| R1 | `Resistor_SMD:R_0805_2012Metric` | `(none)` | no model | - |

#### 01-voltage-divider

`boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb`: 4 footprints, 4 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| J1, J2 | `Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x02_P2.54mm_Vertical.step` | yes | no |
| R1, R2 | `Resistor_SMD:R_0805_2012Metric` | `$K10/Resistor_SMD.3dshapes/R_0805_2012Metric.step` | yes | no |

#### 02-charlieplex-led

`boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb`: 19 footprints, 19 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| C1, C2 | `Capacitor_SMD:C_0805_2012Metric` | `$K10/Capacitor_SMD.3dshapes/C_0805_2012Metric.step` | yes | no |
| J1 | `Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x02_P2.54mm_Vertical.step` | yes | no |
| J2 | `Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_2x03_P2.54mm_Vertical.step` | yes | no |
| D1, D2, D3, D4, ... (9) | `LED_SMD:LED_0805_2012Metric` | `$K10/LED_SMD.3dshapes/LED_0805_2012Metric.step` | yes | no |
| U1 | `Package_DIP:DIP-8_W7.62mm` | `$K10/Package_DIP.3dshapes/DIP-8_W7.62mm.step` | yes | no |
| R1, R2, R3, R4, R5 | `Resistor_SMD:R_0805_2012Metric` | `$K10/Resistor_SMD.3dshapes/R_0805_2012Metric.step` | yes | no |

#### 03-usb-joystick

`boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb`: 38 footprints, 37 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| SW1, SW2, SW3, SW4, SW5 | `Button_Switch_SMD:SW_SPST_TL3342` | `$K10/Button_Switch_SMD.3dshapes/SW_SPST_TL3342.step` | yes | no |
| C1, C2, C3, C4, ... (13) | `Capacitor_SMD:C_0603_1608Metric` | `$K10/Capacitor_SMD.3dshapes/C_0603_1608Metric.step` | yes | no |
| J2 | `Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal` | `$K10/Connector_JST.3dshapes/JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal.step` | **NO** | no |
| J1 | `Connector_USB:USB_C_Receptacle_GCT_USB4085` | `$K10/Connector_USB.3dshapes/USB_C_Receptacle_GCT_USB4085.step` | yes | no |
| Y1 | `Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm` | `$K10/Crystal.3dshapes/Crystal_SMD_3225-4Pin_3.2x2.5mm.step` | yes | no |
| F1 | `Fuse:Fuse_1206_3216Metric` | `$K10/Fuse.3dshapes/Fuse_1206_3216Metric.step` | yes | no |
| J3 | `Joystick:Samtec_TSM-103-01-T-DV` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_2x03_P2.54mm_Vertical_SMD.step` | yes | no |
| U1 | `Package_QFP:TQFP-44_10x10mm_P0.8mm` | `$K10/Package_QFP.3dshapes/TQFP-44_10x10mm_P0.8mm.step` | yes | no |
| U2 | `Package_TO_SOT_SMD:SOT-23-6` | `$K10/Package_TO_SOT_SMD.3dshapes/SOT-23-6.step` | yes | no |
| R3, R4 | `Resistor_SMD:R_0402_1005Metric` | `$K10/Resistor_SMD.3dshapes/R_0402_1005Metric.step` | yes | no |
| R1, R2, R5, R6, ... (11) | `Resistor_SMD:R_0603_1608Metric` | `$K10/Resistor_SMD.3dshapes/R_0603_1608Metric.step` | yes | no |

#### 04-stm32-devboard

`boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb`: 17 footprints, 17 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| C1, C2, C3, C10, ... (10) | `Capacitor_SMD:C_0805_2012Metric` | `$K10/Capacitor_SMD.3dshapes/C_0805_2012Metric.step` | yes | no |
| J1 | `Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x06_P2.54mm_Vertical.step` | yes | no |
| Y1 | `Crystal:Crystal_HC49-4H_Vertical` | `$K10/Crystal.3dshapes/Crystal_HC49-4H_Vertical.step` | yes | no |
| D1 | `LED_SMD:LED_0805_2012Metric` | `$K10/LED_SMD.3dshapes/LED_0805_2012Metric.step` | yes | no |
| U2 | `Package_QFP:LQFP-48_7x7mm_P0.5mm` | `$K10/Package_QFP.3dshapes/LQFP-48_7x7mm_P0.5mm.step` | yes | no |
| U1 | `Package_TO_SOT_SMD:SOT-223-3_TabPin2` | `$K10/Package_TO_SOT_SMD.3dshapes/SOT-223.step` | yes | no |
| R1, R2 | `Resistor_SMD:R_0805_2012Metric` | `$K10/Resistor_SMD.3dshapes/R_0805_2012Metric.step` | yes | no |

#### 05-bldc-motor-controller

`boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb`: 42 footprints, 42 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| C2, C3, C4, C5, ... (15) | `Capacitor_SMD:C_0805_2012Metric` | `$K10/Capacitor_SMD.3dshapes/C_0805_2012Metric.step` | yes | no |
| J1, J5 | `Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x02_P2.54mm_Vertical.step` | yes | no |
| J2 | `Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x03_P2.54mm_Vertical.step` | yes | no |
| J3 | `Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x06_P2.54mm_Vertical.step` | yes | no |
| J4 | `Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_2x03_P2.54mm_Vertical.step` | yes | no |
| D1 | `Diode_SMD:D_SMA` | `$K10/Diode_SMD.3dshapes/D_SMA.step` | yes | no |
| D2 | `Diode_SMD:D_SMB` | `$K10/Diode_SMD.3dshapes/D_SMB.step` | yes | no |
| F1 | `Fuse:Fuse_1206_3216Metric` | `$K10/Fuse.3dshapes/Fuse_1206_3216Metric.step` | yes | no |
| D3 | `LED_SMD:LED_0805_2012Metric` | `$K10/LED_SMD.3dshapes/LED_0805_2012Metric.step` | yes | no |
| U1 | `Package_QFP:TQFP-32_7x7mm_P0.8mm` | `$K10/Package_QFP.3dshapes/TQFP-32_7x7mm_P0.8mm.step` | yes | no |
| U3 | `Package_SO:HVSSOP-8-1EP_3x3mm_P0.65mm_EP1.57x1.89mm_ThermalVias` | `$K10/Package_SO.3dshapes/MSOP-8-1EP_3x3mm_P0.65mm_EP1.5x1.8mm.step` | yes | no |
| RV1 | `Potentiometer_THT:Potentiometer_Bourns_3296W_Vertical` | `$K10/Potentiometer_THT.3dshapes/Potentiometer_Bourns_3296W_Vertical.step` | yes | no |
| R2, R3, R4, R5, ... (12) | `Resistor_SMD:R_0805_2012Metric` | `$K10/Resistor_SMD.3dshapes/R_0805_2012Metric.step` | yes | no |
| R1 | `Resistor_SMD:R_2512_6332Metric` | `$K10/Resistor_SMD.3dshapes/R_2512_6332Metric.step` | yes | no |
| C1 | `board05_revB:CP_Elec_8x10.5_PolarityMark` | `$K10/Capacitor_SMD.3dshapes/CP_Elec_8x10.5.step` | yes | no |
| U2 | `board05_revB:TI_PWP0028C_EP3.4x9.7_Mask3.1x5.18_ThermalVias` | `$K10/Package_SO.3dshapes/HTSSOP-28-1EP_4.4x9.7mm_P0.65mm_EP3.4x9.5mm.step` | yes | no |

#### 06-diffpair-test

`boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb`: 36 footprints, 36 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| C1, C2, C3, C4, ... (16) | `Capacitor_SMD:C_0603_1608Metric` | `$K10/Capacitor_SMD.3dshapes/C_0603_1608Metric.step` | yes | no |
| C17 | `Capacitor_SMD:C_0805_2012Metric` | `$K10/Capacitor_SMD.3dshapes/C_0805_2012Metric.step` | yes | no |
| J1 | `Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x02_P2.54mm_Vertical.step` | yes | no |
| J2, J3 | `Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x08_P2.54mm_Vertical.step` | yes | no |
| U1, U2, U3, U4, ... (8) | `Package_SO:SOIC-8_3.9x4.9mm_P1.27mm` | `$K10/Package_SO.3dshapes/SOIC-8_3.9x4.9mm_P1.27mm.step` | yes | no |
| R1, R2, R3, R4, ... (8) | `Resistor_SMD:R_0603_1608Metric` | `$K10/Resistor_SMD.3dshapes/R_0603_1608Metric.step` | yes | no |

#### 07-matchgroup-test

`boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb`: 43 footprints, 43 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| C1, C2, C3, C4, ... (30) | `Capacitor_SMD:C_0603_1608Metric` | `$K10/Capacitor_SMD.3dshapes/C_0603_1608Metric.step` | yes | no |
| J1 | `Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x02_P2.54mm_Vertical.step` | yes | no |
| J3 | `Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x03_P2.54mm_Vertical.step` | yes | no |
| J2 | `Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x06_P2.54mm_Vertical.step` | yes | no |
| D1, D2 | `LED_SMD:LED_0603_1608Metric` | `$K10/LED_SMD.3dshapes/LED_0603_1608Metric.step` | yes | no |
| U1 | `Package_QFP:LQFP-144_20x20mm_P0.5mm` | `$K10/Package_QFP.3dshapes/LQFP-144_20x20mm_P0.5mm.step` | yes | no |
| U2 | `Package_SO:TSOP-II-54_22.2x10.16mm_P0.8mm` | `$K10/Package_SO.3dshapes/TSOP-II-54_22.2x10.16mm_P0.8mm.step` | yes | no |
| U3 | `Package_TO_SOT_SMD:SOT-23-5` | `$K10/Package_TO_SOT_SMD.3dshapes/SOT-23-5.step` | yes | no |
| R1, R2, R3, R4, R5 | `Resistor_SMD:R_0603_1608Metric` | `$K10/Resistor_SMD.3dshapes/R_0603_1608Metric.step` | yes | no |

#### 09-usbc-pd-power

`boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb`: 47 footprints, 44 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| C1, C2, C3, C4, ... (12) | `Capacitor_SMD:C_0603_1608Metric` | `$K10/Capacitor_SMD.3dshapes/C_0603_1608Metric.step` | yes | no |
| C5, C8, C9 | `Capacitor_SMD:C_1206_3216Metric` | `$K10/Capacitor_SMD.3dshapes/C_1206_3216Metric.step` | yes | no |
| J3 | `Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x06_P2.54mm_Vertical.step` | yes | no |
| J1 | `Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12` | `$K10/Connector_USB.3dshapes/USB_C_Receptacle_HRO_TYPE-C-31-M-12.step` | **NO** | no |
| D1 | `Diode_SMD:D_SOD-123` | `$K10/Diode_SMD.3dshapes/D_SOD-123.step` | yes | no |
| F1 | `Fuse:Fuse_1206_3216Metric` | `$K10/Fuse.3dshapes/Fuse_1206_3216Metric.step` | yes | no |
| D2 | `LED_SMD:LED_0603_1608Metric` | `$K10/LED_SMD.3dshapes/LED_0603_1608Metric.step` | yes | no |
| U1 | `Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm` | `$K10/Package_DFN_QFN.3dshapes/QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm.step` | yes | no |
| U3 | `Package_SO:MSOP-10_3x3mm_P0.5mm` | `$K10/Package_SO.3dshapes/MSOP-10_3x3mm_P0.5mm.step` | yes | no |
| Q1, Q2 | `Package_SO:SOIC-8_3.9x4.9mm_P1.27mm` | `$K10/Package_SO.3dshapes/SOIC-8_3.9x4.9mm_P1.27mm.step` | yes | no |
| U4 | `Package_TO_SOT_SMD:SOT-23` | `$K10/Package_TO_SOT_SMD.3dshapes/SOT-23.step` | yes | no |
| U2 | `Package_TO_SOT_SMD:TSOT-23-6` | `$K10/Package_TO_SOT_SMD.3dshapes/TSOT-23-6.step` | yes | no |
| R1, R2, R5, R6, ... (16) | `Resistor_SMD:R_0603_1608Metric` | `$K10/Resistor_SMD.3dshapes/R_0603_1608Metric.step` | yes | no |
| R3, R4 | `Resistor_SMD:R_1206_3216Metric` | `$K10/Resistor_SMD.3dshapes/R_1206_3216Metric.step` | yes | no |
| RSH1 | `Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm` | `$K10/Resistor_SMD.3dshapes/R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm.step` | **NO** | no |
| J2 | `TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal` | `$K10/TerminalBlock_Phoenix.3dshapes/TerminalBlock_Phoenix_MKDS-1,5-2-5.08_1x02_P5.08mm_Horizontal.step` | yes | no |
| L1 | `board09:SRP7050TA` | `(none)` | no model | - |

#### external/chorus-test-revA (v24)

`boards/external/chorus-test-revA/kicad/chorus-test-revA_v24.kicad_pcb`: 90 footprints, 87 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| C3, C6, C7, C8, ... (34) | `C_0402_1005Metric` | `$K10/Capacitor_SMD.3dshapes/C_0402_1005Metric.step` | yes | no |
| C45 | `C_0603_1608Metric` | `$K10/Capacitor_SMD.3dshapes/C_0603_1608Metric.step` | yes | no |
| C1, C2, C4, C5, ... (15) | `C_0805_2012Metric` | `$K10/Capacitor_SMD.3dshapes/C_0805_2012Metric.step` | yes | no |
| D2, D3 | `D_SOD-323` | `$K10/Diode_SMD.3dshapes/D_SOD-323.step` | yes | no |
| J3 | `Jack_3.5mm_CUI_SJ-3523-SMT_Horizontal` | `$K10/Connector_Audio.3dshapes/Jack_3.5mm_CUI_SJ-3523-SMT_Horizontal.step` | **NO** | no |
| D1, LED3, LED4, LED5, LED6 | `LED_0603_1608Metric` | `$K10/LED_SMD.3dshapes/LED_0603_1608Metric.step` | yes | no |
| FB1 | `L_0603_1608Metric` | `$K10/Inductor_SMD.3dshapes/L_0603_1608Metric.step` | yes | no |
| NT3 | `NetTie-2_SMD_Pad0.5mm` | `(none)` | no model | - |
| Y2 | `Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm` | `$K10/Oscillator.3dshapes/Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm.step` | **NO** | no |
| J5 | `PinHeader_1x03_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x03_P2.54mm_Vertical.step` | yes | no |
| J6 | `PinHeader_1x04_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x04_P2.54mm_Vertical.step` | yes | no |
| J4 | `PinHeader_1x05_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x04_P2.54mm_Vertical.step` | yes | no |
| J2 | `PinSocket_2x20_P2.54mm_Vertical` | `$K10/Connector_PinSocket_2.54mm.3dshapes/PinSocket_2x20_P2.54mm_Vertical.step` | yes | no |
| R1, R2, R3, R4, ... (17) | `R_0402_1005Metric` | `$K10/Resistor_SMD.3dshapes/R_0402_1005Metric.step` | yes | no |
| U1, U2, U3, U4, U7, U10 | `SOT-23-5` | `$K10/Package_TO_SOT_SMD.3dshapes/SOT-23-5.step` | yes | no |
| U8 | `TSSOP-20_4.4x6.5mm_P0.65mm` | `$K10/Package_SO.3dshapes/TSSOP-20_4.4x6.5mm_P0.65mm.step` | yes | no |
| U5 | `TSSOP-28_4.4x9.7mm_P0.65mm` | `$K10/Package_SO.3dshapes/TSSOP-28_4.4x9.7mm_P0.65mm.step` | yes | no |

#### external/softstart (rev A routed)

`boards/external/softstart/output_reva/softstart_routed.kicad_pcb`: 80 footprints, 0 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| SW1 | `Button_Switch_THT:SW_PUSH_6mm` | `(none)` | no model | - |
| C1, C2, C3, C4, ... (19) | `Capacitor_SMD:C_0805_2012Metric` | `(none)` | no model | - |
| J5 | `Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical` | `(none)` | no model | - |
| D_TVS1, D_TVS2, D_TVS3, D_TVS4 | `Diode_SMD:D_SMA` | `(none)` | no model | - |
| D1 | `Diode_THT:Diode_Bridge_DIP-4_W7.62mm_P5.08mm` | `(none)` | no model | - |
| F1 | `Fuse:Fuseholder_Cylinder-5x20mm_Schurter_0031.8201_Horizontal_Open` | `(none)` | no model | - |
| D2 | `LED_SMD:LED_0805_2012Metric` | `(none)` | no model | - |
| MH1, MH2, MH3, MH4 | `MountingHole:MountingHole_3.2mm_M3` | `(none)` | no model | - |
| U2 | `Package_DIP:DIP-6_W7.62mm` | `(none)` | no model | - |
| U1 | `Package_QFP:LQFP-32_7x7mm_P0.8mm` | `(none)` | no model | - |
| U5, U6, U7 | `Package_SO:SOIC-8_3.9x4.9mm_P1.27mm` | `(none)` | no model | - |
| Q5, Q6, Q7, Q8, U4 | `Package_TO_SOT_SMD:SOT-23` | `(none)` | no model | - |
| U3, U8 | `Package_TO_SOT_SMD:SOT-23-5` | `(none)` | no model | - |
| Q1A, Q1B, Q2A, Q2B, U9 | `Package_TO_SOT_THT:TO-220-3_Vertical` | `(none)` | no model | - |
| R1, R2, R3, R4, ... (22) | `Resistor_SMD:R_0805_2012Metric` | `(none)` | no model | - |
| R9 | `Resistor_SMD:R_2512_6332Metric` | `(none)` | no model | - |
| R6, R20, R21 | `Resistor_THT:R_Axial_DIN0617_L17.0mm_D6.0mm_P25.40mm_Horizontal` | `(none)` | no model | - |
| J1, J2, J3, J4 | `TerminalBlock:TerminalBlock_bornier-2_P5.08mm` | `(none)` | no model | - |
| RV1 | `Varistor:RV_Disc_D12mm_W4.2mm_P7.5mm` | `(none)` | no model | - |

#### external/softstart (rev C placed)

`boards/external/softstart/output_revc/softstart_revc.kicad_pcb`: 137 footprints, 135 resolved on host, 0 in pinned image.

| Refs | lib_id | Model path | Host | Pinned image |
|---|---|---|---|---|
| C1, C2, C5, C13, ... (11) | `C_0402_1005Metric` | `$K10/Capacitor_SMD.3dshapes/C_0402_1005Metric.step` | yes | no |
| C42 | `C_0603_1608Metric` | `$K10/Capacitor_SMD.3dshapes/C_0603_1608Metric.step` | yes | no |
| C4, C20, C21, C41, C43, C45 | `C_0805_2012Metric` | `$K10/Capacitor_SMD.3dshapes/C_0805_2012Metric.step` | yes | no |
| C9, C10, C47, C48 | `C_1206_3216Metric` | `$K10/Capacitor_SMD.3dshapes/C_1206_3216Metric.step` | yes | no |
| PS1 | `Converter_ACDC_Hi-Link_HLK-PMxx` | `$K10/Converter_ACDC.3dshapes/Converter_ACDC_Hi-Link_HLK-PMxx.step` | yes | no |
| U2 | `DIP-6_W7.62mm` | `$K10/Package_DIP.3dshapes/DIP-6_W7.62mm.step` | yes | no |
| D5, D6, D_TVS1, D_TVS2, D_TVS3, D_TVS4 | `D_SMA` | `$K10/Diode_SMD.3dshapes/D_SMA.step` | yes | no |
| D3, D4 | `D_SMC` | `$K10/Diode_SMD.3dshapes/D_SMC.step` | yes | no |
| Z1, Z2 | `D_SOD-123` | `$K10/Diode_SMD.3dshapes/D_SOD-123.step` | yes | no |
| U14 | `ESP32-C3-WROOM-02` | `$K10/RF_Module.3dshapes/ESP32-C3-WROOM-02.step` | yes | no |
| F1 | `Fuseholder_Cylinder-5x20mm_Schurter_0031_8201_Horizontal_Open` | `$K10/Fuse.3dshapes/Fuseholder_Cylinder-5x20mm_Schurter_0031_8201_Horizontal_Open.step` | yes | no |
| F2 | `Fuseholder_TR5_Littelfuse_No560_No460` | `$K10/Fuse.3dshapes/Fuseholder_TR5_Littelfuse_No560_No460.step` | yes | no |
| D2 | `LED_0805_2012Metric` | `$K10/LED_SMD.3dshapes/LED_0805_2012Metric.step` | yes | no |
| U1 | `LQFP-32_7x7mm_P0.8mm` | `$K10/Package_QFP.3dshapes/LQFP-32_7x7mm_P0.8mm.step` | yes | no |
| L1 | `L_Taiyo-Yuden_NR-40xx` | `$K10/Inductor_SMD.3dshapes/L_Taiyo-Yuden_NR-40xx.step` | **NO** | no |
| J5, J6 | `PinHeader_1x06_P2.54mm_Vertical` | `$K10/Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x06_P2.54mm_Vertical.step` | yes | no |
| RV1 | `RV_Disc_D12mm_W3.9mm_P7.5mm` | `$K10/Varistor.3dshapes/RV_Disc_D12mm_W3.9mm_P7.5mm.step` | yes | no |
| R42, R43, R58 | `R_0603_1608Metric` | `$K10/Resistor_SMD.3dshapes/R_0603_1608Metric.step` | yes | no |
| R1, R2, R3, R4, ... (54) | `R_0805_2012Metric` | `$K10/Resistor_SMD.3dshapes/R_0805_2012Metric.step` | yes | no |
| R9, R40, R41 | `R_2512_6332Metric` | `$K10/Resistor_SMD.3dshapes/R_2512_6332Metric.step` | yes | no |
| R6, R20, R21 | `R_Axial_DIN0617_L17.0mm_D6.0mm_P25.40mm_Horizontal` | `$K10/Resistor_THT.3dshapes/R_Axial_DIN0617_L17.0mm_D6.0mm_P25.40mm_Horizontal.step` | yes | no |
| U5, U7 | `SOIC-8_3.9x4.9mm_P1.27mm` | `$K10/Package_SO.3dshapes/SOIC-8_3.9x4.9mm_P1.27mm.step` | yes | no |
| U10, U11 | `SOP-4_4.4x2.6mm_P1.27mm` | `$K10/Package_SO.3dshapes/SOP-4_4.4x2.6mm_P1.27mm.step` | yes | no |
| U12 | `SOT-223-3_TabPin2` | `$K10/Package_TO_SOT_SMD.3dshapes/SOT-223.step` | yes | no |
| Q5, Q6, Q7, Q8, ... (9) | `SOT-23` | `$K10/Package_TO_SOT_SMD.3dshapes/SOT-23.step` | yes | no |
| U8 | `SOT-23-5` | `$K10/Package_TO_SOT_SMD.3dshapes/SOT-23-5.step` | yes | no |
| U3 | `SOT-23-6` | `$K10/Package_TO_SOT_SMD.3dshapes/SOT-23-6.step` | yes | no |
| SW1 | `SW_SPST_TL3342` | `$K10/Button_Switch_SMD.3dshapes/SW_SPST_TL3342.step` | yes | no |
| Q1A, Q1B, Q2A, Q2B, ... (8) | `TO-220-3_Vertical` | `$K10/Package_TO_SOT_THT.3dshapes/TO-220-3_Vertical.step` | yes | no |
| U13 | `TSOT-23-6` | `$K10/Package_TO_SOT_SMD.3dshapes/TSOT-23-6.step` | yes | no |
| J1, J2, J3, J4 | `TerminalBlock_Phoenix_MKDS-3-2-5.08_1x02_P5.08mm_Horizontal` | `$K10/TerminalBlock_Phoenix.3dshapes/TerminalBlock_Phoenix_MKDS-3-2-5.08_1x02_P5.08mm_Horizontal.step` | yes | no |
| TP1 | `TestPoint_Pad_D1.5mm` | `(none)` | no model | - |


</details>

### Two false greens in today's tooling

- **`inspect_pcb_models`** (`src/kicad_tools/cli/runner.py`), used by
  `kct render`, iterates only over existing `(model …)` nodes. Results:
  - board 00: `{"status": "passed", "checked_models": 0}`;
  - board 09: flags J1 and RSH1, but not L1;
  - board 03: flags J2.

  A footprint with no ref, or a ref that `(hide yes)` hides, is never
  reported.
- **`add_model_refs`** (`src/kicad_tools/pcb/models3d.py`), tiers 1–3, copies
  the library's `(model …)` node verbatim. There is no existence check (no
  `is_file`/`exists` call in the module). That is how J2, J1 and RSH1 got
  dangling refs. The LCSC tier is the only one that already refuses to emit
  a ref to a known-absent file.

## 2. How footprints, and their models, get onto boards

| Path | Emits `(model …)`? | Evidence |
|---|---|---|
| Hand-written footprint f-strings in `boards/0[0-7]/generate*.py` | **No** | Board 00's `generate_design.py` builds R1/D1/J1 as text; #6354 found the same for board 01 |
| `PCB.add_footprint_from_file` on a stock `.kicad_mod` (board 09) | Yes, verbatim from the library, including dangling refs | `boards/09-usbc-pd-power/generate_design.py:946` |
| Project `.pretty` libs (board 05 `board05_revB.pretty`, board 03 `Joystick.pretty`) | Yes, where the author added one | 2 of 2 and 1 of 1 carry a model; board 09's `SRP7050TA.kicad_mod` has none |
| `kicad_tools.library.generators` (chip/soic/qfn/qfp/sot/bga/dfn/through_hole) | **No** | No `model` token anywhere in `library/` |
| `kicad_tools.parts` (LCSC client, jlcparts catalog, importer) | **No**: the importer matches a stock footprint by package name, and no 3D model is involved | `parts/importer.py::_match_footprint` |
| `kct pcb add-3d-models` (4 tiers: exact → same-library variant → curated substitution → LCSC C-number) | Yes, **post hoc and manual** | No generator, recipe, `kct build`/`pipeline` step or CI job calls it; it was last run fleet-wide in #4012/#4585 |

Committed bodies are therefore an **unowned artifact**. They survive only as
long as nobody regenerates the board. Board 00 shows this: the `(model`
count is 3 in commit `d95b6eff3` (2026-09-10) and 0 from `84e437a4d`
(2026-10-08, #5764) onward.

### Where models should come from: a source ladder

Each tier is tried in order until one yields an existing file.

1. **Stock KiCad library body**, but only if the file exists. Exact name,
   then same-library variant, then curated substitution: these are today's
   tiers 1–3, plus the existence check.
2. **Project or recipe library body.** The board's own `.pretty` and a
   `3dshapes/` directory next to it. This is where commit-safe models live:
   ones we generated, or KiCad CC-BY-SA models we vendored with attribution.
3. **Parametric generation** for standard families: chip passives,
   SOIC/SSOP/QFN/QFP/SOT, pin headers, JST-style shrouded headers, box-shaped
   shunts and inductors. Run KiCad's CadQuery generators as an *external
   tool*, or use our own CadQuery (Apache-2.0) code. Output is cached, and
   committed only under a decided policy.
4. **LCSC C-number fetch at use time** (EasyEDA STEP), keyed by part,
   cache-only, never committed or shipped.
5. **Labelled fallback.** A courtyard-extruded placeholder box, reported as
   `placeholder`, never as `resolved`. A render shows *something*, and the
   audit still counts the gap.

For **"the whole JLCPCB catalog"**, tiers 1 and 3 cover the volume: the
catalogue is dominated by standard chip and IC packages. Tier 4 covers the
long tail of vendor-specific connectors, switches and modules, and only at
use time. We could not measure the catalogue split here, because the
jlcparts SQLite is not cached on this host. Measuring it is the first
acceptance criterion of the pipeline issue.

## 3. Ecosystem registry and external options

The registry check (`kct ecosystem list` / `show`) found these entries:

- **kicad-jlcpcb-tools** (MIT, `watch`) and **jlcparts** (MIT, `watch`).
  Neither downloads 3D models. Bouni's tool reads EasyEDA footprint pads only
  to correct CPL rotation, which is an idea relevant to transform
  auto-registration. jlcparts is a catalogue we already consume offline
  (`parts/jlcparts_catalog.py`).
- **KiBot** (AGPL-3.0, ideas only) is the only entry with a 3D output
  surface.
- **component-importer** (MIT, `evaluated-not-adopted`) has a 3D-path
  matcher, already noted in
  [component-importer-for-kicad-evaluation.md](component-importer-for-kicad-evaluation.md).
- **Faebryk/atopile** fetch EasyEDA STEP bodies
  ([faebryk-component-library.md](faebryk-component-library.md)).

No entry exists for the 3D-specific projects below. Adding them is part of
the epic.

| Project | License | Code reuse here | What it tells us |
|---|---|---|---|
| [kicad-packages3D](https://gitlab.com/kicad/libraries/kicad-packages3D) (10.0.7, 2026-09-13) | CC-BY-SA-4.0 + KiCad design exception | Data, not code | Exception: "the copyright holder waives article 3 of the license with respect to these designs and any generated files". Redistributing "as a collection" must stay CC-BY-SA and "retain attribution information, including the license documents" ([kicad.org/libraries/license](https://www.kicad.org/libraries/license/), [LICENSE.md](https://gitlab.com/kicad/libraries/kicad-packages3D/-/blob/master/LICENSE.md)) |
| [kicad-packages3D-generator](https://gitlab.com/kicad/libraries/kicad-packages3D-generator) | GPL-3.0; **archived** (last commit 2025-01-25) | No | Superseded by [kicad-library-tools](https://gitlab.com/kicad/libraries/kicad-library-tools) |
| [kicad-library-tools](https://gitlab.com/kicad/libraries/kicad-library-tools) (active 2026-10-10) | GPL-3.0-or-later, some files MIT/LGPL | **Run as an external tool only**; never import or vendor | Headless CadQuery generators (`pip install -e .[3d]`: `cadquery==2.6.1`, `cadquery-ocp`) for DIP, chip R/C/L, diodes, crystals, connectors, QFN/QFP/BGA, terminal blocks and more. Its CI job runs on a plain `python:3.13` image with mesa |
| [CadQuery](https://github.com/CadQuery/cadquery) 2.8.0 / [OCP](https://github.com/CadQuery/OCP) 8.0.1.1 | Apache-2.0 / Apache-2.0 (bundled OCCT believed LGPL-2.1 + exception, **unverified**) | Yes, as an optional dependency | Our own parametric bodies |
| [cq-electronics](https://github.com/sethfischer/cq-electronics) 0.2.0 | MIT | Yes | Small CadQuery parts set |
| [cqparts](https://github.com/cqparts/cqparts) | Apache-2.0, dormant since 2020 | Yes, but stale | - |
| [easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py) 1.0.1 | **AGPL-3.0** | **Ideas only** | Documented flow (docs, not code): model uuid from the footprint SVGNODE; OBJ from `modules.easyeda.com/3dmodel/{uuid}` converted to WRL; STEP passed through from the same bucket our tier uses |
| [JLC2KiCadLib](https://github.com/TousstNicolas/JLC2KiCad_lib) 1.3.2 | MIT | Yes, with notice | Symbol, footprint and STEP/WRL from EasyEDA; maintenance-only. Its code license does not grant rights to EasyEDA content |
| [kicadStepUpMod](https://github.com/easyw/kicadStepUpMod) | AGPLv3 per README; no LICENSE file | Ideas only | FreeCAD model-to-footprint alignment, the manual analogue of our transform calibration |
| [kicad-3d-models-in-freecad](https://github.com/easyw/kicad-3d-models-in-freecad) | GPL-2.0 | No | Ancestor of the KiCad generators |

### LCSC/EasyEDA terms: can we redistribute downloaded STEP files?

**No.** Only fetch them at use time, and even that carries residual risk.

**[EasyEDA Terms](https://easyeda.com/legal)** (effective 2026-09-21,
JiaLiChuang (HongKong) Co., Limited), as retrieved from the JS-rendered
page:
- Content "is owned by EasyEDA, its licensors, vendors, and/or its Content
  providers".
- Users "may not copy, distribute, transmit, display, perform, reproduce,
  publish, license, create derivative works…".
- Downloads are "for personal, informational, and noncommercial purposes
  only".
- Users "shall not, directly or through any third party, use any robot,
  spider, scraper, crawler, script or other automated means to access,
  scrape, download…". Bulk or systematic mirroring is barred "unless
  expressly authorized by EasyEDA in writing". So are text/data mining and
  AI training.
- A carve-out preserves rights in individual projects under their own
  open-source or EasyEDA Project License.
- **No clause names components, footprints or 3D models, and no public API
  programme is mentioned.**

**[LCSC T&C](https://www.lcsc.com/faqs/lcsc-information/terms-and-conditions)**
(updated 2026-08-27): "Nothing herein shall be construed to grant any rights
or license to use any software or other intellectual property."

**JLCPCB user agreement:** the page body could not be retrieved
(**unverified**).

**Model authorship is unverified.** The terms' user-content section, plus a
forum report of models sourced from 3DContentCentral, suggest that many
bodies are contributed by users or vendors, with no per-model license shown.

**Conclusions** (not legal advice):
- **Never commit or ship EasyEDA STEP files** in git, wheels, manufacturing
  packages or embedded board files that we publish.
- **Per-user, per-part, on-request fetch with a local cache** is the most
  defensible posture. It is still in tension with the "script or other
  automated means" wording. That applies to the existing #4072 tier and to
  any catalogue-scale pipeline, which is why it needs an **operator
  decision**.
- **Embedding an EasyEDA body into a board we publish** (the site's
  downloadable `.kicad_pcb`, or `kicad_project.zip`) would be
  redistribution. It must be excluded or gated.
- **Other vendor portals are no better.** SnapMagic (CC-BY-SA-4.0 plus a
  Design Exception for symbols and footprints; 3D terms unverified), Ultra
  Librarian and SamacSys (redistribution terms unverified), and
  manufacturer sites such as Toshiba's ("Customer's own use and not for
  sale, lease or other transfer") all point the same way: fetch at use
  time, never commit.

### Operator ruling and implementation (#7686)

Operator ruling Q44 = 1 (2026-10-21): EasyEDA/LCSC 3D models are per-user,
opt-in and fetched at use time only, including for the existing #4072 tier.
They are never committed, packaged or embedded. Written permission from
LCSC/EasyEDA is an operator action; agents do not contact them. The code
guards (cache metadata with `source=lcsc`, content pinning, terms notice,
fetch cap and rate limit, the no-commit guard test) are described in
[`docs/guides/lcsc-3d-models.md`](../guides/lcsc-3d-models.md).

### Catalogue measurement: method and status

`scripts/research/measure_3d_catalog_coverage.py` maps each in-stock
jlcparts `package` string to a KiCad footprint with the matcher that
`kct parts import` uses (`PartImporter._match_footprint`), then asks the
`add-3d-models` ladder (exact, same-library variant, curated substitution)
whether that footprint has an existing stock body. It reports the share with a
stock body, the share that would need the LCSC tier (a footprint with no
body), and the share with no footprint match, by Basic/Preferred vs Extended
and by category. It is offline and read-only.

**Status: not measured.** The jlcparts SQLite (`kct parts sync-catalog`, a
large download) was not cached on the worker that implemented #7686, so no
numbers are claimed here. The share with a *generatable* family (#7687) is also
not measured: there is no generator registry to ask yet. Run the script on a
host with the catalog and the pinned model set, and paste the table below.

| bucket | parts | stock body | needs LCSC | no footprint match |
|---|---|---|---|---|
| all | not measured | not measured | not measured | not measured |

## 4. Interaction with existing gates

### DRC and `lib_footprint_issues`

A black-box probe placed the stock `Resistor_SMD:R_0805_2012Metric` verbatim
on a scratch board, using the pinned library tables and a `.kicad_pro` with
`lib_footprint_mismatch` set to warning:

| Variant | 10.0.7 host | 10.0.6 pinned image |
|---|---|---|
| Verbatim | no finding | not run |
| `(model …)` removed | no finding | no finding |
| Model path changed | no finding | no finding |
| Pad 1 size changed (control) | `lib_footprint_mismatch` | `lib_footprint_mismatch` |

The library-parity check ignores 3D model nodes. The fleet's `.kicad_pro`
files set `lib_footprint_mismatch: ignore` anyway: 00–07 do, and 09
inherits the default. `lib_footprint_issues` is about missing library
*tables*, which model work never touches.

**So model insertion, substitution and embedding cannot change native DRC
evidence.** They do change the `.kicad_pcb` bytes, and those bytes are
fingerprinted in `readiness.json`. Any model change to a ship-ready board
must therefore re-issue readiness on the pinned toolchain
(`KCT_KICAD_DOCKER=1 kct readiness … --verify`), never by hand-editing
hashes.

### `kct render`, the site, and readiness (#7678, #7663)

- `kct render` resolves models from the host. The pinned image has none, and
  `deploy-site.sh` renders on the host (`kct render boards/`, with xvfb for
  3D). A render is therefore a function of whichever KiCad release and model
  set happen to be installed, which is exactly the class of
  host-dependence #6962 removed from DRC evidence.
- Boards 02, 04, 05, 06 and 07 fingerprint `output/renders/3d-*.png` and
  `output/manufacturing/renders/3d-*.png` in `readiness.json`. If images stay
  evidence (#7678's open decision), they need a pinned model set or embedded
  models, plus a pinned renderer. Otherwise the hashes drift with the host.
- Coverage must gate the render. Today `render` reports `partial` only for
  unresolved refs, so board 00 renders bare and passes.

### `kct export`, manufacturing packages, and STEP export

- `kct export` produces no STEP or GLB. Bodies matter for:
  - the `assembly-*.pdf` and `images/` renders;
  - `kicad_project.zip`, whose `.kicad_pcb` carries `${KICAD10_3DMODEL_DIR}`
    refs. A recipient sees bodies only if their KiCad has the same files,
    and never sees the 7,337-class dangling ones.
- `kicad-cli pcb export step` (10.0.7) has `--subst-models`,
  `--no-unspecified`, `--no-dnp`, `--no-components` and
  `--component-filter`, but **no option to embed, and none to fail on a
  missing model**. A mechanical STEP deliverable would silently omit
  missing bodies. Any future `kct export --step` must run the coverage
  audit first.

### Embedding: black-box findings

KiCad 10 docs:
- "Embedding a file stores a copy of the file inside the board file."
  Embedded references "begin with `kicad-embed://`".
- The GUI has **Tools → Collect and Embed 3D Models**.
- The [kicad-cli docs](https://docs.kicad.org/10.0/en/cli/cli.html) list no
  embed option, and `pcb upgrade` only has `--force`.

Format, as observed in a KiCad demo board file (data only; no KiCad source
was read):
- Model refs read `(model "kicad-embed://<name>")`.
- A footprint-level `(embedded_files (file (name …) (type model) (checksum …)))`
  carries no data.
- A board-level `(embedded_files (file (name …) (type model) (data |…|) (checksum "<32 hex>")))`
  carries `data` as base64 of a zstd frame (`KLUv/` = `28 b5 2f fd`). It
  decompresses to the raw `ISO-10303-21` STEP.

Probes on board 01 with host kicad-cli 10.0.7 `pcb render`:

| Variant | Result |
|---|---|
| Two demo `file` blocks with their original checksums, R1/R2 refs re-pointed at them | Renders the foreign FPC bodies on R1/R2: embedded models resolve **without any library path** |
| One self-built block (zstd + base64) with a zero checksum | Loads; the body is **silently dropped**, and the PNG is byte-identical to the no-model render |
| Two self-built blocks with zero checksums | `Failed to load board: Unknown token 'name'` |

The checksum is not MD5, SHA-256/16, BLAKE2b-128, XXH128 or MurmurHash3
(x64/x86, 0/common seeds, both half orders) of either the raw or the
compressed bytes. Its definition is undocumented in the
[s-expression format docs](https://dev-docs.kicad.org/en/file-formats/sexpr-intro/index.html).

Under the clean-room rule (black-box only, never KiCad source), the options
are:
1. ask upstream to document the field or to add a `kicad-cli pcb embed-models`;
2. find a documented, non-source spec;
3. a black-box differential probe that varies inputs and checks KiCad's
   accept or reject behaviour.

### Checksum spike outcome (#7688, 2026-10-22): blocked

The spike tried all three options, timeboxed to one session. It read KiCad
docs, issue titles and data files only, and ran the `kicad-cli` and `pcbnew`
binaries as black boxes. No KiCad source was read.
[`scripts/research/embedded_checksum_probe.py`](../../scripts/research/embedded_checksum_probe.py)
reproduces every hash and inversion result below. Its `verify` subcommand is
the accept/reject oracle.

**The corpus.** The host KiCad 10.0.7 demos ship 96 data-bearing blocks, with
51 distinct payloads (`royalblue54L_feather`, `vme-wren` and its `.pretty`).
Re-saving a scratch board with `(embedded_fonts yes)` and a TrueType face
makes kicad-cli embed the font and write its checksum. That yields further
KiCad-produced pairs over bytes we know exactly: the decompressed `data` is
byte-identical to the font file on disk.

**What the checksum covers.** `kicad-cli pcb upgrade --force` prints
`Warning: Checksum error in embedded file <name>` and drops the block on a
mismatch. That makes it a cheap oracle, with no render needed:

| Variant of a valid KiCad-written block | 10.0.7 host | 10.0.6 pinned image |
|---|---|---|
| Unchanged | accepted | accepted |
| Payload recompressed at zstd level 19 (different `data` bytes, same raw) | accepted | accepted |
| `name` changed (`SODFL1608X65N.step` to `x.step`) | accepted | not run |
| Raw payload plus one trailing byte | **rejected** | not run |
| Self-built block, zero, empty or missing `checksum` | **rejected** | **rejected** |

So the checksum is a function of the **decompressed bytes only**. It does not
cover the zstd frame, the base64 text or the name. The loader never
generates one: blocks with an empty or missing checksum are rejected, not
filled in.

**What it is not.** These were tested over the raw, compressed and base64
bytes, in five output byte orders (as-is, reversed, halves swapped,
big-endian u64 lanes, big-endian u32 lanes):
- MD5, SHA-1/16, SHA-256 (first or last 16 bytes), BLAKE2b-128, BLAKE2s-128;
- XXH3-128 (seeds 0, 1 and a few common ones), CityHash128, FarmHash128 and
  SpookyHash V2 (seeds 0 and 1);
- MurmurHash3 x86_128 by brute force over seeds 0–4095;
- CRC32, Adler-32, XXH32/64 or the zstd frame checksum as any 4- or 8-byte
  piece of it, plus the length.

None match.

**MurmurHash3 is excluded for every seed, not just common ones.** Both
x64_128 and x86_128 are invertible when the input is known, so the harness
runs them *backwards* from each observed checksum to their initial state.
- A seeded hash would come back with all lanes equal.
- A fixed non-standard initial state would come back identical across
  samples.

Neither happens, for any output byte order. The same holds with a 4- or
8-byte length prefix or suffix, a name prefix, a NUL terminator, a
zero-padded final block, or any of eight alternative finalizer length
values.

**Routes that would let KiCad compute it for us, both closed:**
- **The font oracle.** It hashes arbitrary bytes only if they load as a
  font. A STEP file overwritten in place under a cached fontconfig entry is
  silently not embedded.
- **The `pcbnew` SWIG module.** Bundled with KiCad 10, it exposes
  `BOARD.GetEmbeddedFiles()` only as an opaque `SwigPyObject`, with no
  methods.

**Docs and upstream.** The format docs ([s-expression intro](https://dev-docs.kicad.org/en/file-formats/sexpr-intro/index.html),
[board](https://dev-docs.kicad.org/en/file-formats/sexpr-pcb/index.html),
[footprint](https://dev-docs.kicad.org/en/file-formats/sexpr-footprint/index.html))
do not describe `embedded_files` at all. Searches of the KiCad issue tracker
for "embedded checksum", "embedded_files", "kicad-cli embed" and "embed 3d
models cli" (2026-10-22) find bug reports about embedding, but none that
documents the checksum or requests a CLI embed command.

**Conclusion.** The checksum cannot be produced clean-room with the means
available today. `kct pcb embed-models` and package embedding stay deferred,
because writing blocks that KiCad silently drops would be worse than
shipping `${KICAD10_3DMODEL_DIR}` refs. Two kinds of upstream answer would
unblock it:
- a documented checksum definition;
- a `kicad-cli pcb embed-models` (or an `--embed-models` flag on
  `pcb upgrade`). With that, kct shells out and never needs the algorithm.

Filing the request is operator decision D4 (#7690). Suggested text for the
KiCad issue tracker:

> **kicad-cli: embed 3D models, or document the `embedded_files` checksum**
>
> KiCad 10 can embed 3D models in a board (Tools → Collect and Embed 3D
> Models), which makes boards self-contained for fabrication packages and
> headless renders. kicad-cli has no equivalent, so CI pipelines cannot
> produce embedded boards. Third-party tools cannot write the blocks
> themselves either: the `(checksum "…")` field of an `embedded_files` entry
> is validated on load (a mismatch drops the file with "Checksum error in
> embedded file"), but the file-format documentation does not define it.
> Could kicad-cli gain `pcb embed-models [--output]` (or `pcb upgrade
> --embed-models`), and could the s-expression docs specify how the checksum
> is computed?

The license position is clear, though:
- **KiCad library bodies may be embedded in published boards.** The design
  exception covers "generated files".
- **Self-generated bodies** may be embedded.
- **EasyEDA bodies must not be embedded in anything we publish.**

## 5. Recommendations

The issues are drafted in the session scratchpad, not filed.

1. **Make the gap visible first.** Build a coverage audit that counts
   footprints rather than refs. Classes: `resolved`, `dangling`, `no-ref`,
   `unresolved-var`, `placeholder`, `exempt`. Exempt covers mounting holes,
   net ties, test points, fiducials and `board_only`. Surface it in
   `kct check`, `kct render`, `kct fleet status`, and a non-blocking CI
   report that runs against a pinned model set.
2. **Stop inserting dangling refs.** Make tiers 1–3 existence-checked and
   fall through. Report what is left as unresolved.
3. **Pin the model set.** Choose a `kicad-packages3D` tag that matches the
   pinned kicad-cli. Fetch it by tag and digest into a cache, and mount it
   into the Docker shim. This is a network and size decision for an
   operator: 3.2 GB in full, or a sparse per-board subset.
   *Status:* done in #7683 (ruling Q43 = 1). `kct models sync` fetches a
   55-file, ~14 MB subset at tag `10.0.6` against a committed sha256
   manifest; it excludes LCSC/EasyEDA bodies, which stay per-user (#7686).
4. **Generators own their models.** Add a shared post-generation step
   (the `add_model_refs` ladder) to the board recipe and pipeline. Add a
   regeneration guard so a regen that drops bodies fails, as board 00 and
   board 01 #6354 would have.
5. **Fix the demo boards to 100% on the pinned set:**
   - board 00 (re-insert refs);
   - board 03 J2, board 09 J1/RSH1/L1, and the chorus J4 wrong proxy (ladder
     tiers 2–4);
   - board 08 once it has a PCB.
6. **LCSC pipeline at catalogue scale, terms-respecting.** C-numbers come
   from the BOM, not stale lib_id sidecars. Sidecars carry content
   pinning (a STEP sha256). Transforms are auto-registered by matching the
   EasyEDA footprint pads to the KiCad pads (an idea from
   kicad-jlcpcb-tools' CPL rotation work). Cache only, never committed. The
   operator decides how far automated fetching may go.
7. **Parametric generation** for standard-family gaps, through an optional
   `[models]` extra on CadQuery. It may invoke kicad-library-tools only as
   an external process, because of the GPL.
8. **Embed for self-contained packages and stable renders**, once the
   checksum spike is resolved. Only KiCad and self-generated bodies; never
   EasyEDA bodies in published artifacts.
   *Status:* the spike (#7688, 2026-10-22) is **blocked**. The checksum
   covers the raw bytes, but no clean-room route produces it (see
   "Checksum spike outcome"). Embedding waits on an upstream answer, and
   filing that request is operator decision D4.
9. **Registry hygiene.** Add entries for easyeda2kicad (AGPL, ideas only),
   JLC2KiCadLib (MIT), kicad-library-tools (GPL, run only), kicadStepUpMod
   (AGPL) and cq-electronics (MIT), with this note as "our notes".

## Sources

- KiCad library license and exception: <https://www.kicad.org/libraries/license/>, <https://gitlab.com/kicad/libraries/kicad-packages3D/-/blob/master/LICENSE.md>
- kicad-packages3D README, TODO.md, tags and files API: <https://gitlab.com/kicad/libraries/kicad-packages3D>
- KLC F9.3: <https://gitlab.com/kicad/libraries/klc/-/blob/master/content/footprint/F9/F9.3.adoc>
- Missing-model work items: <https://gitlab.com/kicad/libraries/kicad-packages3D/-/work_items/778>, [/780](https://gitlab.com/kicad/libraries/kicad-packages3D/-/work_items/780), [/781](https://gitlab.com/kicad/libraries/kicad-packages3D/-/work_items/781), [/783](https://gitlab.com/kicad/libraries/kicad-packages3D/-/work_items/783)
- Generators: <https://gitlab.com/kicad/libraries/kicad-packages3D-generator> (archived), <https://gitlab.com/kicad/libraries/kicad-library-tools>
- CadQuery: <https://github.com/CadQuery/cadquery/blob/master/LICENSE>; OCP: <https://github.com/CadQuery/OCP>
- easyeda2kicad: <https://github.com/uPesy/easyeda2kicad.py> (README, docs/CMD_3D_MODEL.md, LICENSE)
- JLC2KiCadLib: <https://github.com/TousstNicolas/JLC2KiCad_lib/blob/master/LICENSE>
- kicad-jlcpcb-tools: <https://github.com/Bouni/kicad-jlcpcb-tools>; jlcparts: <https://github.com/yaqwsx/jlcparts>
- EasyEDA Terms: <https://easyeda.com/legal>; LCSC T&C: <https://www.lcsc.com/faqs/lcsc-information/terms-and-conditions>; JLCPCB user agreement (not retrievable): <https://jlcpcb.com/help/article/user-agreement>
- EasyEDA forum on model provenance: <https://easyeda.com/forum/topic/Using-non-jlc-lcsc-parts-660302c192c74dd68971d58cd221b208>
- kicadStepUpMod: <https://github.com/easyw/kicadStepUpMod>; cqparts: <https://github.com/cqparts/cqparts>; cq-electronics: <https://github.com/sethfischer/cq-electronics>; kicad-3d-models-in-freecad: <https://github.com/easyw/kicad-3d-models-in-freecad>
- KiCad 10 docs: <https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html>, <https://docs.kicad.org/10.0/en/cli/cli.html>; s-expression format: <https://dev-docs.kicad.org/en/file-formats/sexpr-intro/index.html>
- SnapMagic license: <https://support.snapmagic.com/en/articles/2957814-what-is-the-license-for-symbols-and-footprints>; Ultra Librarian (forum quote): <https://forum.kicad.info/t/where-to-start-with-3d-models-for-custom-parts/7185?page=2>; Toshiba/UL consent: <https://toshiba.semicon-storage.com/ap-en/semiconductor/product/agree-ul.html>
- In-repo: `src/kicad_tools/pcb/models3d.py`, `lcsc_models.py`, `lcsc_model_transforms.py`, `model_substitutions.py`, `src/kicad_tools/cli/runner.py` (`inspect_pcb_models`, `_render_env`), `src/kicad_tools/cli/render_cmd.py`, `src/kicad_tools/cli/kicad_toolchain.py`, [`docs/guides/lcsc-3d-models.md`](../guides/lcsc-3d-models.md); issues #4012, #4014, #4072, #4585, #5101, #6354, #6962, #7663, #7678
