# Reviewed physical routing

`sdram_demo.kicad_pcb` is the explicit routing/length-tuning artifact for the
43-part, 216-binding real SDRAM circuit. `manifest.json` binds its SHA-256 and
the exact authored source manifest. It includes the completed copper and
operator silkscreen; it is not a generic autorouter cache entry.

Run `../build.py OUTPUT` through `uv run python` to regenerate the independent
electrical source and repeat every design gate. A new logical circuit, physical
package placement or source-rule manifest requires a new reviewed routing
artifact and fresh validation; updating a hash alone is not a design review.

The build intentionally distinguishes design validation from manufacturing
export. The final order package must be generated afterward and its manifest
must pass before manufacturing readiness is claimed. Hardware has not yet been
tested.

## Class-clearance repair (Issue #6396)

The reviewed copper was first validated against the 0.15 mm native floor only.
Once #6390 declared the board's own 0.175 mm `AUX`/`SDRAM_*` classes, 42
track-to-via/pad gaps fell 0.2–25 µm short. `../repair_class_clearance_6396.py`
is the complete record of the fix. It moves 24 vias by 10–47 µm, re-attaches
their stubs at 0/45/90°, and moves one GND escape by 7 µm, then re-pins both
manifests. No class value changed. Run it with `--verify-from-git 810f833f` to
reproduce both committed PCBs byte for byte from the pre-repair artifacts.

## Evidence refresh (Issue #6404)

`build.py` fills the planes under the published router net classes, the same
`.kicad_pro`/`.kicad_dru` that sit beside the routed board. Before this, the
fills used the fab floor alone, and the saved fills failed native DRC without
`--refill-zones`. The files under `evidence/` come from that build of the
repaired copper. `scripts/boards/refresh_bundles_6404.py` regenerates them,
the routed board's fills and the manufacturing bundle together.

## Same-net copper-gap repair (Issue #9091)

Once #8938 graded same-net `physical_copper_gap` findings as errors, the
reviewed copper had 25 same-net slits: nine vias 0.07–0.12 mm from their own
pads, and narrow same-net wedges at U2.50 (DQ13), U2.39 (UDQM) and U2.53
(DQ15). `../repair_copper_gap_9091.py` is the complete record of the fix.
- It moves the nine vias 53–87 µm clear of their pads.
- It replaces a +5V staircase beside C26 with a plain T, which frees one of
  those vias.
- At DQ13 and UDQM, it removes the loops. Each trunk ran through its own pad
  and doubled back along the pad's escape, so the loop counted about 10 mm of
  length the signal never travelled. The script restores that length with
  `SerpentineGenerator` trombones on the trunks.
- It turns DQ15's shallow staircase join into a right angle and puts the
  1.5 mm it removes back into DQ15's existing meander.

Every `SDRAM_BYTE1` length is unchanged to 1 µm. DQ7 gains 0.145 mm. Run the
script with `--verify-from-git 89c2ee058` to reproduce both PCBs byte for byte.

The files under `evidence/` and the published `../../output/` release still
describe the copper as it was before the November 2026 prune (95c4c7eaf).
`build.py` cannot re-issue them yet. Its full `kct check` gate still fails on
`mask_to_copper` (#8858) and on warnings that already existed.
