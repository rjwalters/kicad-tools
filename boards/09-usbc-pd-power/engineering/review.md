# Board 09 development review — 2026-09-10

This is a circuit/placement checkpoint, not an assembly release. The generated
schematic passes native ERC and label LVS. The first route failed both
connectivity and native DRC. An analytical screen is separate from measured
performance and from final-copper validation.

## Electrical decisions

- STUSB4500 factory sink profiles are 5 V / 1.5 A, 15 V / 1.5 A and 20 V / 1 A
  ([ST datasheet, table 18](https://www.st.com/resource/en/datasheet/stusb4500.pdf)).
  Verify actual NVM/PDOs and the negotiated contract on assembled units before
  applying load. An arbitrary reprogrammed controller is not equivalent.
- Q1/Q2 are source-connected AO4407A P-channel MOSFETs. Their gates have a
  pull-up, sink-drive resistor, 12 V gate clamp and slew capacitor. Review
  turn-on current and MOSFET SOA with the final input capacitance. Off-state
  reverse isolation is not a guarantee against externally powering the output.
- TPS54302 uses the manufacturer's 10 µH, 2 × 22 µF output-filter topology,
  100 kΩ / 13.3 kΩ feedback and 75 pF feedforward capacitor. The EN divider is
  680 kΩ / 100 kΩ to keep ordinary 5 V input below the intended start threshold.
  Internal EN currents are typical specifications: calculated start/stop
  thresholds are not guaranteed production bounds.
- R3/R4 were increased from 0603 to 1206 high-power pulse-rated parts:
  CRCW12061K00FKEAHP. At 21 V, 1 kΩ would initially dissipate 0.441 W.
  The [Vishay HP e3 datasheet](https://www.vishay.com/docs/20043/crcwhpe3.pdf)
  and actual discharge duration/temperature still govern the thermal review.
- TLV76033DBZR pinout is **1 OUT, 2 IN, 3 GND**. Its 3.3 V rail only supplies
  local telemetry and pull-ups; J3 pin 4 is a voltage-reference observation
  point, not an external host power supply.
- INA226 uses the four-terminal Vishay WSK2512R0100FEA 10 mΩ shunt. The
  `T1.19mm` stock footprint matches this resistance range; the `T2.66mm`
  alternative does not. Force current crosses pins 1/4; pins 2/3 are independent
  Kelvin taps. The Bourns inductor has a local manufacturer land pattern.

## Analytical results and limits

Run `engineering/calculate.py` after generation. The screen is bound to the
actual generated component values and refuses unreviewed value changes.
Nominal pre-shunt output is 5.077 V. Reference/divider tolerance and shunt drop
alone give 4.910 V at 3 A through 5.214 V unloaded. These bounds omit load/line
regulation, ripple, transients, PCB drop and temperature drift.

At 21 V input, minimum switching frequency and −20% inductance, calculated
peak current is 3.845 A against a 4 A minimum switch limit. This is a narrow
margin, not a guaranteed hot-board rating: inductance under DC bias and at
operating temperature still needs checking. Estimated winding dissipation is
0.637 W using maximum 25°C DCR; hot DCR increases it. Shunt loss is about 0.091 W.
The input budget uses maximum screened output voltage, 3 A, assumed 85%
efficiency and 0.1 W logic reserve. Efficiency remains unmeasured.

Capacitor effective capacitance under DC bias, buck compensation/transients,
input surge protection, fuse coordination, connector current capability,
MOSFET slew/SOA and PD detach/discharge timing remain release gates. These
checks must precede a 3 A claim or manufacturing package.

## Layout and tool findings

The first routing experiment connected 18/31 non-ground nets. Native KiCad
reported 18 violations and 68 unconnected items; ground planes were not yet
added. Its snapshots are diagnostic artifacts only. C5/L1 courtyard overlap
and a feedback-capacitor reference collision were subsequently moved in the
placement source. The subsequent audit found that edits to pad rotation on
newly added footprints were lost on save. This changed the physical L1 pad
orientation. Linking new pads to their serialization nodes fixes the defect;
native library-difference warnings are now zero.

The current generated checkpoint has zero native geometry violations and
109 unconnected items. C5/C6 are rotated beside U2 and directly connect VIN
and GND. A 0.35 mm, approximately 2.71 mm long SW escape passes between C6's
lands to three 0.6/0.3 mm vias. A 2 mm bottom-layer trunk reaches three vias
outside L1's solder lands. At nominal 35 µm copper and 20°C resistivity, the
neck alone is approximately 3.8 mΩ and 35 mW at the screened 3.039 A RMS;
this omits etch tolerance, hot copper, vias and heat spreading and is not an
ampacity qualification. Final return-plane geometry and switch-loop EMI remain
review gates. The boot capacitor's SW branch is still unconnected.

RSH1 pins 2/3 now route independently to R10/R11. C11 was moved near U3's
inputs and all four Kelvin/filter nets are connected in native KiCad's report,
without force-pad shorts. Routing is not symmetric; input pickup, final force
copper proximity and filter placement still need a noise/layout review.

- [#4980](https://github.com/rjwalters/kicad-tools/issues/4980): added concrete
  force-current, feedback, LED and Kelvin branch requirements for this board.
- [#4991](https://github.com/rjwalters/kicad-tools/issues/4991): added native
  shunt/monitor escape shorts observed with strict pad-clearance mode.
- [#5032](https://github.com/rjwalters/kicad-tools/issues/5032): filed loss of
  the source project's preservation flag and native rule floors when routing
  to a new output basename/directory.
- [#5049](https://github.com/rjwalters/kicad-tools/issues/5049): filed and locally
  fixed lost pad geometry edits on newly added footprints, with round-trip
  tests covering rotations, positions, layers and repeated/blank pad numbers.
- [#5055](https://github.com/rjwalters/kicad-tools/issues/5055): requested gallery
  metrics for development PCBs before manufacturing export. The current
  producer attaches renders/readiness but omits PCB dimensions and counts
  when no manufacturing directory exists.

Next layout work: finish the boot/feedback loops and power trunks
with explicit endpoint/current assumptions, review the Kelvin routing,
complete signal routing and ground planes, then rerun native DRC and physical
copper LVS. Routing percentage alone cannot close these gates.

## Primary component references

- [TPS54302](https://www.ti.com/lit/ds/symlink/tps54302.pdf)
- [INA226](https://www.ti.com/lit/ds/symlink/ina226.pdf)
- [TLV760](https://www.ti.com/lit/ds/symlink/tlv760.pdf)
- [AO4407A](https://www.aosmd.com/res/data_sheets/AO4407A.pdf)
- [SRP7050TA](https://www.bourns.com/docs/product-datasheets/srp7050ta.pdf)
- [WSK2512](https://www.vishay.com/docs/30108/wsk2512.pdf)

The finer-grid experiment was stopped after more than four minutes despite a
90 s requested total budget. Its initial 23-net count reduced to 16 after
validation, and only an unvalidated partial snapshot was saved. Filed
[#5035](https://github.com/rjwalters/kicad-tools/issues/5035) for deadline
propagation through post-pass work. Those old snapshots retain their original
violations; the current generator/checker results described above supersede
the earlier placement checkpoint. Zero geometry violations with opens is not
a complete native DRC pass.
