# Independent inner-layer impedance cross-check

The proposed six-layer JLC06161H-2116A construction uses In1.Cu and In4.Cu
as reference planes, with isolated In2.Cu/In3.Cu signal traces. Factory vertical
geometry and dielectric constants come from
[JLCPCB's published stack](https://jlcpcb.com/impedance).

`stripline_fdm.py` independently solves the quasi-static cross-sectional
capacitance in vacuum and with the actual dielectric strata, then calculates
Z0 = 1 / (c sqrt(Cvacuum Cdielectric)). The trace is 0.0152 mm thick; its
nearest reference gap is 0.13 mm and its far reference gap is 1.078 mm.
The trace-adjacent resin is modeled with relative permittivity 4.16.
It models an isolated trace, without nearby signal coupling or return-plane
voids. The board still requires route-spacing and uninterrupted-reference
checks, plus the fabricator's controlled-impedance process.

At 0.16 mm width, grid refinement from 0.01 to 0.005 to 0.0025 mm gives
49.871, 50.178 and 50.302 ohms. Doubling lateral half-domain from 2.5 to 5 mm
changes the 0.005 mm result by less than 0.00004 ohm. `results.json` contains
actual measured solver outputs, including widths 0.15, 0.18 and 0.19 mm.
These are numerical design estimates, not fabrication impedance certification.

```sh
uv run --with scipy --with numpy python \
  boards/07-matchgroup-test/real_design/impedance-evidence/stripline_fdm.py .16 .005
```

The existing analytic asymmetric-stripline routine produces about 71.7 ohms
for this 0.16 mm geometry; that discrepancy is tracked in
[#5016](https://github.com/rjwalters/kicad-tools/issues/5016). No acceptance
threshold has been widened to accommodate it.
