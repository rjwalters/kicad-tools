# Historical joystick routing fixture

Frozen from commit `06fb5e30`, before the real ATmega32U4 revision-B redesign.
SHA-256 of `usb_joystick.kicad_pcb`: `6c4292a95345a7670ebc3879262d192edf40c90335ba6bee83c3c88441d18c98`.

This synthetic 32-pad controller layout preserves the geometry exercised by
legacy #2760/#3183/#3308 escape-routing capability tests. It is not a working
circuit or a manufacturing release. Do not select it for gallery or export.

`tests/test_board_03_regression.py::routed_board_03` uses this snapshot;
real circuit, native DRC and manufacturing tests use the revision-B files in
`output/` and `tests/test_board_03_real_hardware.py`.

Tracked in [#5042](https://github.com/rjwalters/kicad-tools/issues/5042).

Historical silkscreen witness `usb_joystick_routed.kicad_pcb` from `06fb5e30` preserves known defects for detector parity tests. It is not manufacturing data. SHA-256: `f4ee2d9b1cf82f10aa86d5ac8277118518d0d9a9ba9de2c3abdafe69eee14397`.
