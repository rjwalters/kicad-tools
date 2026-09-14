# Board07 electrical review evidence

The physical source is a six-layer SDR SDRAM bench demonstration. A native
manufacturing DRC pass alone does not approve its electrical interface.

`package-escape-spacing.json` measures the authored front-side bus escape paths
used by the physical source. The longest is 5.1199 mm; the two SDCLK escapes are
3.3279 and 4.6491 mm. No clock-to-other-bus escape pair is closer than 0.54 mm
edge-to-edge. There are 64 data/control escape pairs closer than 5 mm. These
are measured deviations from the same-layer separation recommendation, not
an automatic footprint exemption or a transient signal-integrity pass.

[ST AN4488 §8.4.2](https://www.st.com/resource/en/application_note/an4488-getting-started-with-stm32f4xxxx-mcu-hardware-development-stmicroelectronics.pdf)
recommends separate data/control routing layers, or 5 mm separation on a shared
layer, and at least three trace widths around the clock. The note does not
explicitly establish a package-escape exception. The source uses separate
layers for the long groups and conservatively interprets clock spacing as
0.54 mm edge-to-edge for 0.18 mm traces. Final routing must separately measure
new copper; this source-only report cannot cover later routing changes.

The source's reserved reference planes, short package escapes and local power
connections reduce the coupled run length. The short data/control escape deviation is accepted for this bench-demo
manufacturing release based on its measured extent, reserved reference planes,
separate long-run routing layers and the documented load/clock screens.
This is a bounded engineering judgment; it does not establish literal blanket
compliance with the application note. No IBIS transient/corner qualification or
hardware operation has been claimed.

The independent receiver/via load review is in `load-review.md`. Its 1 pF per
complete through-via allowance is an engineering reserve over an approximate
nominal model. DQ receiver load needs an explicit reserve because STM32 lists
5 pF typical without a maximum. The validator reports that assumption; it
must not describe it as a guaranteed MCU capacitance bound.

Reproduce the source spacing census with:

```sh
uv run python boards/07-matchgroup-test/real_design/engineering/measure_escapes.py
```

`clock_spacing.py PCB OUTPUT.json` separately measures the final candidate's
clock tracks and complete through vias against all other signal copper, in both
directions. It also reports package pad interactions separately. The native
TSOP-II's adjacent pad geometry cannot establish three-width spacing between
pads; the clock-to-trace requirement must not be silently generalized into an
impossible package requirement. Final traces and vias remain subject to the
explicit 0.54 mm screen.

`escape-coupling-scenarios.json` records illustrative existing-model results for
one aggressor, the measured 0.320000 mm minimum gap and the full 5.1199 mm escape length treated as
parallel. Rise times of 1, 0.1 and 0.05 ns are assumptions, not measured or
manufacturer-guaranteed minima. These scenarios give context for the short
coupled length; their automatic `acceptable` labels are not a board sign-off.
Multiple aggressors, package parasitics and receiver/driver corners remain
outside that calculation.

The issue #5044 angle repair subdivides fourteen authored clock/neighbor/data escape chords into four
45-degree doglegs to retain the clock-spacing screen and the original 0.320 mm minimum data/control gap. These measurements and
the illustrative coupling scenarios were rerun on that geometry; placement,
trace widths, layer assignments and through-via construction are unchanged.
