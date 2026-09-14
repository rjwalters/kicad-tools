# Board07 fabrication and assembly requirements

This document describes the assembled SDRAM redesign. It is not an order
release: use only a final package whose readiness evidence passes all gates.

- Six layers, nominal 1.6 mm, JLC06161H-2116A stack. Preserve every dielectric
  sublayer in the native project, including the central prepreg/core/prepreg.
- Copper order: F signal / In1 ground / In2 signal / In3 signal / In4 3.3 V /
  B signal. Do not replace the reference planes with routed signal layers.
- Outer copper 0.035 mm, inner copper 0.0152 mm. Finished bus trace width
  0.18 mm; reviewed 50 ohm ±10% single-ended geometry. Factory stack/material
  substitutions or width changes require recalculation and routing review.
- Ordinary full-span plated through vias, 0.5 mm diameter and 0.2 mm drill.
  No microvias, blind/buried vias, or via-in-pad processing is intended.
- Two-sided SMT assembly: 40 SMT components, including 25 back-side capacitors.
  Follow native package pin-1 and LED cathode marks and the generated placement
  drawings. Bottom placement rotations must come from the validated CPL.
- Manually solder J1/J2/J3, the three 2.54 mm through-hole headers. Exclude
  them from the SMT placement file; keep their exact selected part numbers in
  the manual assembly BOM. Check pin-1 orientation against the drawings.
- Supply exact BOM parts. A distributor listing is not a reservation of
  assembly inventory; confirm availability when ordering, especially U2.

Use regulated 5 V ±5% through J1. The initial bench operating envelope is
0–30 °C ambient and at most 230 mA continuous board current. Inspect soldering
and check resistance between power and ground before applying current-limited
power. Confirm 3.3 V and both VCAP rails before SWD programming; then run the
complete memory test and measure current and regulator temperature. The SWD
probe's target-voltage pin is a sense input, not a second board supply.

Firmware build/programming and UART connections are in `firmware/README.md`.
An exported package can be fabrication/assembly-ready while hardware validation
remains pending. Do not describe this design as bench-proven until an assembled
unit passes those checks.
