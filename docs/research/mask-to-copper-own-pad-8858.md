# `mask_to_copper` findings on boards 00-07: what is real (issue #8858)

Measured 2026-11-17 on `main` @ `89c2ee058` with the pinned toolchain
(`KCT_KICAD_DOCKER=1`; `kicad_toolchain --check` reports kicad-cli **10.0.6**,
isolated `KICAD_CONFIG_HOME`). Every assessment below ran to `coverage: complete`.
Policy: the interim JLCPCB default (0.09 mm error / 0.15 mm warning, #8842).

## Question

`mask_to_copper` reported 12, 127 and 385 errors on boards 01, 02 and 03 (and
1,542 across boards 00-07). Are these real mask openings exposing copper, or an
artifact of the threshold, the expansion handling or same-net treatment?

## Method

1. `kct check <routed pcb> --mfr jlcpcb --format json` per board, pinned image.
2. Each measurement was classified using the per-object native geometry the
   assessment carries (`geometry_provenance.source_objects`, read from pcbnew
   inside the pinned image).
3. **Black-box check against KiCad's own plot, not kct's reconstruction.** The
   pinned kicad-cli plotted `F.Cu,B.Cu,F.Mask,B.Mask` to Gerber. The
   independent Gerbonara reader (`tests/_mask_gerbonara_oracle.py`) gave the
   plotted mask (M) and copper (C). Each pad's own plotted copper (P) came from
   the flashes carrying KiCad's X2 attribute `TO.P,<ref>,<pad>`. For each finding
   the opening is O = the mask component over P. Two quantities were measured
   from the plot alone: `area(O - P)`, which is how much the opening extends
   beyond the pad, and `area(O ∩ C - P)`, which is copper the opening exposes
   beyond the pad's own land.
4. The pinned `kicad-cli pcb drc` was run for connectivity (0 unconnected items
   on boards 01-03).

Scratch scripts (not committed): plot, classify, oracle, union-distance. The
commands are given under "Reproduce" below.

## Result 1: every opening is exactly its pad

All fleet boards author `pad_to_mask_clearance 0`. On the plot, `area(O - P)` is
0 for every finding on boards 00-07. The consumer board behind #8914/#9038 is the
same: its pads carry a 0 mm margin, except 6 openings at 0.5 mm.

## Result 2: families and verdicts

Counts are errors (`violation` + `uncertain`) before this change.

| Family | 01 | 02 | 03 | Plot: copper beyond pad | Fab evidence | Verdict |
|---|---:|---:|---:|---|---|---|
| A. same-net track entering its own pad (conductor copper overlaps the pad) | 5 | 108 | 288 | 0 mm² in every case | #8914: plain entries were not flagged; consumer #8858: 655 entries, 0 flagged | **artifact**: `within_own_pad` (info) |
| B. own pour under its pad (solid connect) | 5 | 0 | 51 | 0 mm² | not flagged (the fab does not judge pour; #8938) | **artifact**: `within_own_pad` |
| C. same-net via whose annulus overlaps its pad, drill under mask | 2 | 2 | 1 | 0 mm² | none flagged | **artifact**: `within_own_pad` |
| D. same-net track beside its pad, not touching (0-0.09 mm) | 0 | 6 | 26 | (not in the opening) | **danger**: the fab's 5 danger were this shape at 0-0.08 mm (#8914) | **kept as error** |
| E. same-net via beside its pad, not touching | 0 | 11 | 19 | (not in the opening) | same band as D | **kept as error** |
| F. grazing contact: overlap shallower than the construction budget | 0 | 1 | 0 | ~0 | unknown | **kept** (connection unproven) |
| different-net | 0 | 0 | 0 | none of these were errors; warnings at 0.10-0.13 mm are pour clearance | | unchanged |
| **total errors** | **12** | **127** | **385** | | | |

Boards 00 and 04-07 have the same families. Family A' on board 05 is a same-net
pad stacked inside its owner pad's land (4 findings), and it reclassifies on the
same evidence. Every one of the 1,292 reclassified findings on boards 00-07 shows
0 mm² of copper beyond the pad in the plot. The same holds for the consumer
board's 720.

What families A-C measured was the conductor's copper lying *under* the pad. The
"exposed area" (e.g. 0.348 mm² at R1/1 on board 01) is pad land. The opening
exposes nothing that the pad does not already expose. There is no exposed trace
to report, whatever threshold is used.

## The rule change

A measurement becomes `within_own_pad` (info) only when **all** of these hold.
Otherwise it keeps its old verdict.

- There is a single pad owner (never a merged region), and the copper has the
  same identified net.
- Pad copper and conductor copper overlap beyond the construction budget. This
  is the same direct-connection proof the `MaskEscapeIntent` audit uses.
- The copper exposed beyond the pad's copper is empty once shrunk by the
  construction budget. The budget is `2 x max_error + 4 nm`, about 10 µm, the
  same one that decides "certain exposure". On the consumer board this absorbs
  plot-versus-polygon rings under 5 µm wide (up to 0.026 mm² on a 3 mm round
  pad). The plot shows no copper there.
- A via also needs its authored drill to lie wholly under mask, so a via-in-pad
  is never classified here.

No threshold moved.

## What stays as it is, and why

- **D/E (same-net copper beside a pad)** match the shape and the 0-0.08 mm band
  JLCDFM reported as danger. Fab parity outranks convenience, so these remain
  errors. They are the board defects left on boards 02 (17) and 03 (45). A
  follow-up must decide whether per-object distance overstates some of them. In
  a "union" view, the jog segment after a pad exit shares its end cap with the
  entry segment. Measuring the distance to that copper minus the entry
  conductor's copper puts 9 of the 62 at or above 0.15 mm, 13 at 0.09-0.15 mm
  and 40 still below 0.09 mm (02: 11/1/5, 03: 29/12/4). The fab's own exclusion
  rule for exits is not recorded, so no evidence here justifies changing them.
- **A positive expansion's entry corridor.** A track crossing the ring between
  the pad edge and the opening edge exposes real trace copper. The existing
  native `connected` fixture (0.1 mm expansion) stays a violation. No fleet
  board exercises this case, so it is not settled here.
- **Via-in-pad, grazing contact, merged regions, different-net copper**: unchanged.

## Effect, boards 00-07 (`--mfr jlcpcb`, pinned image)

| Board | errors before | errors after | `mask_to_copper` err before → after | m2c warnings (unchanged) | wall before / after |
|---|---:|---:|---:|---:|---:|
| 00 | 6 | 0 | 6 → 0 | 3 | 4.0 s / 3.9 s |
| 01 | 12 | 0 | 12 → 0 | 8 | 4.3 s / 4.3 s |
| 02 | 141 | 32 | 127 → 18 | 1 | 10.3 s / 10.5 s |
| 03 | 422 | 82 | 385 → 45 | 76 | 24.7 s / 25.5 s |
| 04 | 103 | 36 | 69 → 2 | 6 | 9.8 s / 10.1 s |
| 05 | 385 | 86 | 356 → 57 | 122 | 38.4 s / 39.7 s |
| 06 | 180 | 25 | 180 → 25 | 0 | 17.0 s / 17.2 s |
| 07 | 370 | 66 | 341 → 37 | 31 | 48.9 s / 47.2 s |

Each removed error reappears as exactly one `within_own_pad` info finding. The
assessment JSON keeps the measurement, its raw area, and the new
`exposed_beyond_owner_mm2`.

## Reproduce (one finding per family)

```bash
KCT_KICAD_DOCKER=1 uv run python -m kicad_tools.cli.kicad_toolchain --check   # 10.0.6
KCT_KICAD_DOCKER=1 uv run kct check boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb \
  --mfr jlcpcb --format json > b01.json
# plot the pinned KiCad's layers for the black-box comparison
#   kicad-cli pcb export gerbers --layers F.Cu,B.Cu,F.Mask,B.Mask --no-protel-ext -o out/ <pcb>
```

| Family | Board | Opening | Copper | Layer | Location (mm) | Measured |
|---|---|---|---|---|---|---|
| A | 01 | pad R1/1 | segment, VIN | F.Mask/F.Cu | (147.375, 83.000) | area 0.348 mm²; plot O-P = 0, beyond pad = 0 |
| B | 01 | pad J2/2 | GND zone | F.Mask/F.Cu | (158.500, 88.107) | area 0.567 mm²; beyond pad = 0 |
| C | 01 | pad R1/2 | via, VOUT, center (149.5, 83.9), drill 0.3 | F.Mask/F.Cu | (149.500, 83.575) | area 0.065 mm²; center 0.25 mm outside the opening, so the hole edge is 0.10 mm outside |
| D | 03 | pad U1/13 | segment, RESET (jog after the exit at 141.7, 82.3) | F.Mask/F.Cu | (141.575, 82.300) | 0.025 mm per object; 0.084 mm union |
| E | 02 | pad D2/1 | via, NODE_A | F.Mask/F.Cu | (148.050, 85.499) | 0.050 mm |
| F | 02 | pad R1/2 | segment, NODE_A | F.Mask/F.Cu | (134.835, 112.867) | overlap 0.0008 mm², below budget |

For A-C, take the `F_Mask.gbr` flash over the pad and the `F_Cu.gbr` flash
tagged `TO.P,R1,1` (etc.), and compute `mask ∩ copper − pad flash`: 0 mm².

## Sources

- JLCDFM "solder mask opening exposing trace" on the #8914 board: 5 danger and
  8 warning, all same-net, with geometry per item recorded in #8914 (the
  reporter's location comment). The consumer's family table is on #8858 (2026-11-17).
- Pour clearance is not fab-judged: the #8938 measurement (1,516 pour-to-pad
  gaps at 0.10-0.13 mm unflagged), recorded in `fab_dfm_parity.py`.
- The fab parity table row `solder_mask_opening_exposing_trace`, v11.
