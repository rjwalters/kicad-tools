# Issue #5362 witness — Board03 R13/R14 VCC open

The retained failing board from #5358's Board03 repair: native KiCad reports
one real `unconnected_items` result on it, while this repo's analyzers
reported it fully connected until #5362.

**Do not regenerate or reformat these files.** Their value is that they are the
exact bytes measured in #5362; the regression pins their SHA256s.

## Provenance

| item | value |
|---|---|
| `usb_joystick_routed.kicad_pcb` | `0141cb8e49f99aab13c005ca0b7431ca3e227bb934d7ed788b474b33578c6303` |
| `usb_joystick.kicad_sch` | `c715b4bd587bc4a2c36f8d58e9f40f4999cff6ca4891faaab65000e7369088d8` |
| `usb_joystick_routed.kicad_dru` | `9006a6ab3092e14891c1c8bfec0507caf7ce61d24a454e3954817144c42e49bb` |
| `usb_joystick_routed.kicad_pro` | `811c7ad8225b72a8a53183fb459acb77155050bd4b3a0fc13dd82e5978205efd` |
| source archive | `prototype3-witness.tar.gz`, `cbc9f38e0823328de231296c0c9825b5c4da6d1c82bcb80659d778fecf5473b7` |

Generated from main `dffd402e` plus #5358's intermediate routing-plan repairs.
It is **not** the final repaired board from #5361 and not a historical
committed PCB — it is a deliberately-retained *failing* witness.

## Native measurement

`kicad-cli pcb drc --format json`, **no** `--refill-zones`, **no**
`--save-board`, under `kicad/kicad:10.0` digest `sha256:182c8005cb77...`
(KiCad 10.0.5). Board SHA256 identical before and after:

```
Found 1 unconnected items
  Pad 1 [VCC] of R14 on F.Cu (155.675, 103.5)
    <-> Track [VCC] on F.Cu, length 1.9700 mm (151.68, 103.5)
```

This reproduces the report recorded in #5362 verbatim, including the track
UUID `71a5ed36-a820-581a-840f-0230239a3421`.

## Why the analyzers missed it

The board declares `(version 20260206)` and its zones omit
`filled_areas_thickness`. KiCad's parser initialises
`isStrokedFill = m_requiredVersion < 20250210`, so on this file the stored
`filled_polygon` outlines are **already solid copper** — two fill fragments of
one zone are therefore not bonded to each other by adjacency at all.

Both analyzers instead treated adjacent fragments as continuous
(pre-#5157/#5236 by zone identity, then by a bare `.intersects()` test), which
merged VCC fill fragments 2 and 5 — distance zero at board-relative
`(41.435, 59.5)` — and absorbed `R13.1` into the main VCC population.

See `tests/test_fill_fragment_bonding_5362.py` for the version-boundary
measurement series this fixture anchors.
