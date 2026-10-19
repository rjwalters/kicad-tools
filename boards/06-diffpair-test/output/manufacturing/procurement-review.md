# Four-channel LVDS link demonstrator

Revision C replaces the synthetic USB/PCIe/MIPI endpoints with four working
LVTTL → LVDS → LVTTL links. The original multi-protocol files remain the
historical routing regression; they are not this assembly's circuit.

Each channel uses **SN65LVDS1DR + SN65LVDS2DR**, TI's actual SOIC-8 packages,
with a 100 Ω receiver termination. The pin maps and typed schematic symbols
come from [TI SLLS373M, tables 5-1 and 5-2](https://www.ti.com/lit/ds/symlink/sn65lvds1.pdf).
The SOIC receiver and transmitter have different power pin assignments:
driver VCC/GND = 1/4; receiver VCC/GND = 8/5. NC pins remain unconnected.
Each IC has 1 nF and 100 nF bypass capacitors, following TI §9.2.1.2.2;
the input has 10 µF bulk capacitance. A 100 kΩ pulldown defines every idle input.

## Use

Supply regulated **3.3 V** to J1 pin 1, ground to pin 2. There is no onboard
regulator. Set a 250 mA current limit for initial bench checks.
J2 pins 1/3/5/7 are IN1/2/3/4; J3 pins 1/3/5/7 are OUT1/2/3/4.
All even pins on J2/J3 are ground. Apply 0–3.3 V logic to one input and observe
the corresponding output with a high-impedance instrument. Start with static
levels, then a 1 MHz square wave; test all four channels simultaneously.
No firmware is needed. Do not load the logic outputs with 50 Ω termination.

The four-layer design uses F.Cu signals, In1.Cu ground, In2.Cu 3.3 V,
and B.Cu auxiliary signals. Differential traces are 0.26 mm wide with
0.15 mm edge separation over a JLC04161H-7628 0.2104 mm prepreg reference spacing (Dk 4.6).
An assembly release still requires actual fabricator stackup/impedance review,
complete native DRC after refill, matching netlists, and current exports.
The explicit [manufacturer stackup](https://cart.jlcpcb.com/client/template/placeOrder/impedance.html)
uses 35 µm outer copper, 15.2 µm inner copper and a 1.065 mm core.
The tool predicts 97.51 Ω differential for the authored pair geometry.
The IC speed ratings are not a measured board bandwidth or a protocol
compliance claim. This board is an LVDS demonstration, not a USB/PCIe/MIPI device.

## Procurement

| References | Manufacturer part | Supplier identity | Package |
|---|---|---|---|
| U1/U3/U5/U7 | TI SN65LVDS1DR | [C2671256](https://www.lcsc.com/product-detail/C2671256.html) | SOIC-8, 3.9 × 4.9 mm, 1.27 mm pitch |
| U2/U4/U6/U8 | TI SN65LVDS2DR | [C2671054](https://www.lcsc.com/product-detail/C2671054.html) | SOIC-8, 3.9 × 4.9 mm, 1.27 mm pitch |
| Odd C1–C15 | Samsung CL10B102KB8NNNC, 1 nF | [C1588](https://www.lcsc.com/product-detail/C1588.html) | 0603, X7R, 50 V |
| Even C2–C16 | Yageo CC0603KRX7R9BB104, 100 nF | [C14663](https://www.lcsc.com/product-detail/C14663.html) | 0603, X7R, 50 V |
| C17 | Samsung CL21A106KAYNNNE, 10 µF | [C15850](https://www.lcsc.com/product-detail/C15850.html) | 0805, X5R, 25 V |
| R1–R4 | UNI-ROYAL 0603WAF1000T5E, 100 Ω | [C22775](https://www.lcsc.com/product-detail/C22775.html) | 0603, 1% |
| R5–R8 | UNI-ROYAL 0603WAF1003T5E, 100 kΩ | [C25803](https://www.lcsc.com/product-detail/C25803.html) | 0603, 1% |
| J1 | XFCN PZ254V-11-02P | [C492401](https://www.lcsc.com/product-detail/C492401.html) | 1 × 2, 2.54 mm THT, straight male pin header |
| J2/J3 | XFCN PZ254V-11-08P | [C492407](https://www.lcsc.com/product-detail/C492407.html) | 1 × 8, 2.54 mm THT, straight male pin header |

Supplier identities were checked 2026-09-10. Inventory and assembly-provider
availability must be checked when ordering; a listing is not a reservation.
The headers are assembled by JLCPCB (through-hole assembly) and are in `bom_jlcpcb.csv`/`cpl_jlcpcb.csv`.

## Reproduce

```sh
uv run python boards/06-diffpair-test/assembled-demo/generate.py /tmp/lvds-demo
uv run python -m kicad_tools.cli.route_cmd /tmp/lvds-demo/diffpair_test.kicad_pcb \
  --output /tmp/lvds-demo/diffpair_test_routed.kicad_pcb \
  --nets IN1,IN2,IN3,IN4,OUT1,OUT2,OUT3,OUT4 --preserve-existing \
  --layers 4 --no-auto-layers --no-auto-pour --strict-layers --no-cache \
  --net-class-map /tmp/lvds-demo/net_class_map.json \
  --strategy negotiated --seed 42 --iterations 20 --timeout 120 --grid 0.075
```

After routing, run `finish.py /tmp/lvds-demo /tmp/lvds-demo/diffpair_test_routed.kicad_pcb`
with `uv run python` to retain reviewed differential geometry, remove zero-length
routing artifacts and refill planes.

The generator pre-routes symmetric differential links and explicit plane vias.
The router completes the eight LVTTL nets. A generated file or successful route
command alone does not establish manufacturing readiness.

The sidecar explicitly excludes both inner reference layers from auxiliary
routing. The finalizer applies four reviewed off-land via moves and rejects
any signal track on a reference plane, non-through via span, or drill overlap
with an SMT land. Ordinary via processing is used; POFV is not required.

## 2026-10-18 CPL rotation correction (issue #7200)

`cpl_jlcpcb.csv` is regenerated with the JLCPCB rotation profile
(`jlcpcb_rotations.yaml`) and in the Gerber/drill-origin frame (#7059). The
2026-09-10 package shipped these rotations uncorrected. Rotation changes are
placement changes, so this section records them explicitly:

| Reference | Footprint | Old rotation | New rotation |
|---|---|---|---|
| U1 | SOIC-8_3.9x4.9mm_P1.27mm | 0 | 270 |
| U2 | SOIC-8_3.9x4.9mm_P1.27mm | 0 | 270 |
| U3 | SOIC-8_3.9x4.9mm_P1.27mm | 0 | 270 |
| U4 | SOIC-8_3.9x4.9mm_P1.27mm | 0 | 270 |
| U5 | SOIC-8_3.9x4.9mm_P1.27mm | 0 | 270 |
| U6 | SOIC-8_3.9x4.9mm_P1.27mm | 0 | 270 |
| U7 | SOIC-8_3.9x4.9mm_P1.27mm | 0 | 270 |
| U8 | SOIC-8_3.9x4.9mm_P1.27mm | 0 | 270 |

Raw KiCad `pos` reports 0 for all eight (U1–U8); the new values come from the JLCPCB
profile, not from the board. **Pending human orientation review:** no person
has yet checked these against the JLC part footprints / datasheet pin-1 marks.
Do not order assembly from this CPL until that review is recorded here.

## 2026-10-19 Samtec headers replaced by LCSC-stocked headers (issue #7422)

By operator ruling (#7268, Q2 = 3) the Samtec TSW headers J1-J3 are replaced
with straight male 2.54 mm pin headers that LCSC/JLCPCB stock, so JLCPCB can
assemble them. J1-J3 are now in `bom_jlcpcb.csv` and `cpl_jlcpcb.csv`, and
`manual-assembly-bom.csv` is retired. Copper, schematic and footprints are
unchanged: the board already used the generic
`PinHeader_1xNN_P2.54mm_Vertical` footprints, not Samtec-specific ones.

Stock and price were read with `kct parts lookup --no-cache` on 2026-10-19.
Stock changes daily and a listing is not a reservation.

| Ref | Was | Now | Pins | Stock | 1 pc |
|-----|-----|-----|------|-------|------|
| J1 | Samtec TSW-102-07-G-S | XFCN PZ254V-11-02P, C492401 | 1 × 2 | 1,082,699 | $0.0187 |
| J2, J3 | Samtec TSW-108-07-G-S | XFCN PZ254V-11-08P, C492407 | 1 × 8 | 330,352 | $0.0523 |

Equivalence for this board: same pin counts, single row, 2.54 mm pitch,
straight male. Pin-to-net assignment is set by the unchanged footprint. The
headers carry 3.3 V, ground and the LVTTL inputs/outputs; the LVDS pairs run
between the ICs and do not touch them, so the headers are outside the 100 ohm
differential structure.

Not verified: pin cross-section and hole-size fit against the footprint's drill
(taken from the LCSC listing text only, no datasheet check), plating and
mating-cycle rating relative to the Samtec parts, and JLCPCB through-hole
assembly eligibility of these two part numbers. Procurement must review these
rows before ordering.
