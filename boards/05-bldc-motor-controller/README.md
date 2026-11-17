# Sensored BLDC Controller

Revision B is a real 42-component, 70 × 90 mm, four-layer small-motor controller:
ATmega328P-AU, DRV8313PWPR integrated triple half bridge, and TPS7A1650DGNR
5 V supply. It includes reverse-input protection, fuse, transient clamp,
charge-pump capacitors, current comparator, Hall inputs, AVR ISP, RUN switch
input, speed trimmer and working commutation firmware.

Start at 12 V with a 0.5 A current-limited bench supply, an unloaded motor,
RUN open and the speed input at its measured low-voltage end. The initial
phase-current target is 0.5 A; the nominal comparator trip is 1 A and firmware
caps duty at 25%. Physical motor operation and thermal performance have not
yet been measured. Read [HARDWARE.md](redesign/HARDWARE.md) before assembly,
programming or power-up; it contains all connector pinouts and verified BOM identities.

Generate the real circuit and reviewed route, run native refill/DRC/ERC and
manufacturer/LVS gates, and export its assembly bundle:

```sh
uv run python boards/05-bldc-motor-controller/design.py [output-directory]
```

Failed reconstruction leaves the preceding output intact. The route is explicit
reviewed copper in `redesign/routing.json`, guarded by a hash of the actual
component/pad/placement/net geometry. It combines automatic routing with manual
physical corrections; changing circuit geometry requires a new route and fresh
gates. No synthetic routing pads are present in the generated board.

The active revision-B recipe calls the generic physical power-stitch transaction
before its final native and manufacturing gates. It accepts existing complete
power copper or a safely completed candidate after real refill, preserving all
previous pad bonds and rejecting new native findings. Its retained `readiness/stitch`
ledger contains saved, refilled-before, proposed and refilled-candidate bytes,
native reports and physical pad components. Failed stage evidence is retained in
the `bldc-power-stitch-*` temporary directory named by the error.

Fresh reconstruction at main `7368bf597eaefb2dbe171f79db3c3216843a2ae6` already had
zero native opens/errors and clean copper LVS (149 bound pads); the historical
57-open legacy audit does not describe this active recipe. The generic split-plane
regression in `tests/fixtures/stitch-5388` demonstrates why added vias alone cannot
prove completion. Issue #4410 remains open for its later routing/Kelvin/QFN phases.

The manufacturing bundle has 36 SMT placements; hand-solder J1–J5 and RV1.
Solder both driver/regulator exposed pads. Use the included local symbol and
footprint libraries, native drawings, BOM/CPL and firmware. Fresh reconstruction
passed native KiCad DRC/ERC with zero findings and manufacturer checks with
zero errors or warnings. These fabrication checks do not replace motor and
thermal bench validation.

The original DRV8301 circuit is retained in [legacy_drv8301](legacy_drv8301/ARCHIVE.md)
as an electrically invalid historical regression fixture, documented in
[issue #4993](https://github.com/rjwalters/kicad-tools/issues/4993). Its generator
writes `regression-output`; the default command above builds revision B.
