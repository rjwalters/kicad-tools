# `mask_to_copper` findings on boards 00-07: what is real (issue #8858)

Measured 2026-11-17 on the pinned toolchain (`KCT_KICAD_DOCKER=1`;
`kicad_toolchain --check` reports kicad-cli **10.0.6**, isolated
`KICAD_CONFIG_HOME`). Every board assessment below ran to `coverage: complete`.
The policy is the interim JLCPCB default: 0.09 mm error / 0.15 mm warning (#8842).

> **Revision (review of PR #9108).** A first version excused any same-net copper
> that overlapped its pad and exposed nothing beyond it: 1,292 of 1,542 errors
> (84%). That was too broad. At 0 mm expansion "nothing exposed beyond the pad"
> holds for *any* copper, so it excused tracks running along or straddling the
> pad edge, which the fab flags as danger. The rule below excuses only a clean
> entry: **963 of 1,542 (62%)**. The numbers in this note are the revised ones.

## Question

`mask_to_copper` reported 12, 127 and 385 errors on boards 01, 02 and 03, and
1,542 across boards 00-07. Are these real exposures, or artifacts of the
threshold, the expansion handling or same-net treatment?

## Method

1. `kct check <routed pcb> --mfr jlcpcb --format json` per board, on the pinned image.
2. Classify each measurement from the per-object native geometry the assessment
   carries (pcbnew inside the pinned image), plus the authored track ends and
   widths from the source.
3. Black-box check against KiCad's own plot. The pinned kicad-cli plotted
   `F.Cu,B.Cu,F.Mask,B.Mask`, and an independent Gerbonara reader built:
   - M, the plotted mask;
   - C, the plotted copper;
   - P, each pad's own copper, from the flashes carrying KiCad's X2 attribute
     `TO.P,<ref>,<pad>`;
   - O, the mask component over P.

   Measured: `area(O − P)`, `area(O ∩ C − P)` (copper exposed beyond the pad's
   land), and copper under mask within 0.09 mm of O.

   The reader in `tests/_mask_gerbonara_oracle.py` drops round and oval flashes
   that KiCad plots as zero-length lines with a width (gerbonara calls them
   zero-size), so the measurements here used a scratch variant that keeps them.
4. Pinned `kicad-cli pcb drc`: 0 unconnected items on boards 01-03.

## Result 1: every opening is exactly its pad, which makes that measurement weak evidence

All fleet boards author `pad_to_mask_clearance 0`, and in the plot `area(O − P)`
is 0 for every finding. So "0 mm² exposed beyond the pad" is true of *any*
copper on these boards, by construction. It shows the opening exposes only pad
land. It says nothing about copper under mask right beside the opening, which is
what the fab's danger items are. **The verdict on the excused class therefore
rests on the fab evidence, not on that measurement.** JLCDFM did not flag plain
pad entries on the #8914 board, where the consumer counts 655 entries and 0 flags.
All five of its danger items were tracks beside, along, or bending beside a pad.

## The criterion

A same-net measurement is `within_own_pad` (info) only when **all** of these hold
(`_within_own_pad` in `src/kicad_tools/validate/mask_copper.py`):

1. One owner, a plain `rect`/`roundrect`/`circle`/`oval` pad, with mask margin ≥ 0.
2. Same identified net. This is the net pcbnew loads: a floating stub touching only
   one pad loads with that pad's net. A pad with no net never qualifies.
3. Direct overlap with the pad's copper beyond the construction budget
   `u = 2·max_error + 4 nm` (about 10 µm; the native board's `m_MaxError` per
   independently constructed side).
4. Nothing beyond the pad: `area(O ∩ copper − P.buffer(u)) ≤ 1e-9 mm²`. `u` is
   tolerated **once**, which absorbs the plot-versus-polygon rings below 5 µm on
   round pads and nothing wider. A 0.015 mm expansion ring fails.
5. Nothing beside the pad beyond one clean entry. Let
   `B = area((copper − O.buffer(u)) ∩ O.buffer(c + u))`, the conductor's copper
   under mask within the danger clearance `c` = 0.09 mm of the opening. Let `A`
   be the allowance:
   - `A = 1.45·w·c` for a straight segment with exactly one authored end strictly
     inside the pad and the other outside. A perpendicular crossing puts `w·c`
     there, a 45° crossing `√2·w·c`, and 1.45 is √2 plus 2.5% for polygon
     approximation.
   - `A = 0` for everything else: a segment lying wholly on the pad, arcs, vias,
     pours and stacked pads.
   - A segment with no end inside the pad (pass-through, straddle, along-edge) is
     never excused.

   The fab-danger measure is the excess `E = max(0, B − A)`, recorded as
   `beside_owner_excess_mm2`. The class requires `E ≤ 1e-9 mm²`.
6. A via's authored drill lies wholly outside the opening (moot in practice: a
   via ring touching a pad always has `B > 0`).

**Monotone by construction.** A finding is info only at `E` = 0. Any `E` > 0
keeps the measured verdict, which is an error for an overlap. Sweeps (in
`tests/test_mask_copper_own_pad_8858.py`), 0.2 mm track, 1 mm pad:

- **Translation.** A 0.4 mm track moved from 0.2 mm above the edge down onto the
  pad: warning at 0.20 mm, then error (disjoint) from 0.16 to 0.12 mm, then error
  with `E` = 0.052, 0.053, 0.051, 0.032 and 0.010 mm² while it straddles the edge,
  then info once it lies wholly on the pad (`E` = 0). A long track (always a
  pass-through) is an error at every offset.
- **Rotation.** An exit pivoted at the edge, angles from perpendicular:
  - 0° to 45°: info (`E` = 0);
  - 50°: error, `B/(w·c)` = 1.56;
  - 60°: 2.00;
  - 70°: 2.92;
  - 85°: 9.97.

  Severity never drops as `E` rises.

**Shapes the evidence does not decide, so they stay errors:**

- straight pass-through;
- entries shallower than about 45°, and entries at roundrect corners where the
  band exceeds 1.45·w·c;
- arcs;
- vias touching their pad (family C cannot be separated from E, a via just
  outside the pad);
- pours under and around the pad (no fab result covers pour beside an opening;
  #9085's declaration is the route for a reviewed own pour);
- custom, padstack and chamfered pads;
- solder-mask-defined pads;
- any expansion ring wider than `u`.

## Families (errors before the change; verdict on the revised rule)

| Family | 01 | 02 | 03 | Verdict |
|---|---:|---:|---:|---|
| A1. clean straight entry, one end in the pad, `E` = 0 | 5 | 69 | 127 | **info** (fab: entries not flagged) |
| A0. track lying wholly on the pad, `E` = 0 | 0 | 21 | 112 | **info** (no copper beside or beyond) |
| A2. entry with excess beside copper (shallow, or at a corner) | 0 | 3 | 1 | error |
| A3. pass-through / along-edge / straddle (no end in the pad) | 0 | 7 | 25 | error: fab danger items 1-2 shape |
| A4. both ends in the pad but copper beside it | 0 | 8 | 23 | error |
| B. own pour under and around the pad | 5 | 0 | 51 | error: fails closed (pour ring beside the opening) |
| C. via ring overlapping its pad | 2 | 2 | 1 | error: not separable from E |
| D. same-net track beside its pad, not touching | 0 | 6 | 26 | error: fab danger band |
| E. same-net via beside its pad | 0 | 11 | 19 | error |
| different-net | 0 | 0 | 0 | none were errors |
| **total** | **12** | **127** | **385** | info 5 / 90 / 239 |

The first version's board 02 table summed to 128 because the grazing contact
(R1/2) was counted in both A and F. It is now in A2/A3, and the table sums to 127.

The Judge's fleet cases are errors again:
- board 03 SW3/1, segment `e0343df4` on the pad's top edge: `E` = 0.057 mm²;
- board 03 SW1/1;
- board 02 D7/2, a track straddling x = 138.95.

Family B confirmed from the plot alone at board 01 R2/2 (roundrect in the GND
pour): O − P = 0 and nothing beyond the pad, but 0.108 mm² of pour copper under
mask within 0.09 mm. That is why it stays an error.

Plot check of every excused finding, using the zero-length-line-aware reader:
00: 2, 01: 5, 02: 90, 03: 239, 04: 42, 05: 200, 06: 132, 07: 253. All show
O − P = 0 and 0 mm² beyond the pad.

## Effect, boards 00-07 (`--mfr jlcpcb`, pinned image)

| Board | errors before → after | `mask_to_copper` err → after (info) | m2c warnings (unchanged) | wall before / after |
|---|---|---|---:|---|
| 00 | 6 → 4 | 6 → 4 (2) | 3 | 4.0 / 4.5 s |
| 01 | 12 → 7 | 12 → 7 (5) | 8 | 4.3 / 4.3 s |
| 02 | 141 → 51 | 127 → 37 (90) | 1 | 10.3 / 10.5 s |
| 03 | 422 → 183 | 385 → 146 (239) | 76 | 24.7 / 26.0 s |
| 04 | 103 → 61 | 69 → 27 (42) | 6 | 9.8 / 10.3 s |
| 05 | 385 → 185 | 356 → 156 (200) | 122 | 38.4 / 40.6 s |
| 06 | 180 → 48 | 180 → 48 (132) | 0 | 17.0 / 18.0 s |
| 07 | 370 → 117 | 341 → 88 (253) | 31 | 48.9 / 49.1 s |

## Consumer board (private, redacted)

The run is still incomplete on a footprint keepout object (a separate issue), so
everything stays capped at warning. That is unchanged and not regressed.

- 570 of 1,083 findings are `within_own_pad`.
- **Own-pad entries:** 561 of 655 are excused. The 94 left are shallow or corner
  entries, or on-pad tracks with copper beside the pad.
- **Pass-through:** 4 of 129 are excused, 125 stay. By their authored ends the 4
  are single entries: three 2.6 mm power tracks into 2.6 mm round pads, and one
  0.2 mm track. kct's capsule-based triage label calls them pass-through.

I **cannot show that all five fab dangers survive**, because the fab's item
coordinates are not in the record. All five are described as beside, along,
bending or branching shapes. Per object those are either non-entry segments
(errors) or entries with excess beside copper (errors). None is described as a
clean straight entry.

## #9107 (per-object distance)

Per-object measurement cannot understate. The distance from an opening to the
union of copper equals the minimum over the objects. Every object within reach
is measured, so the closest copper is always reported. It can overstate. A jog
segment's end cap shares copper with the entry segment, and is reported at that
cap's distance.

**Decision (2026-11-17, #9107): keep per-object; a jogged exit stays an
error.** The fab evidence decides against the union view. JLCDFM's five danger
items on the #8914 board include two jogged pad exits: item 1 (fab 0 mm,
"track leaves an 0603-size pad, jogs 45° to a via 1.1 mm away; the track body
lies along the pad's opening edge") and item 5 (fab 0.08 mm, "track leaves a
pad and jogs to a via 0.9 mm away"). The fab counts the jog, so kct must keep
a measured distance for the chained segment itself. A union view — copper
beyond the entry conductor — reports farther: in the pinned fixture (entry
0.125 mm past the pad edge, 0.2 mm track, board 03 U1/13 RESET's shape) the
45° jog moves from 0.025 mm to 0.084 mm (the issue's numbers), and a 30° jog
to 0.098 mm, out of the 0.09 mm danger band for a shape the fab flags as
danger. Chaining a conductor to a pad-entry conductor is therefore not a
disposition: `within_own_pad` keeps refusing any segment with no authored end
inside the pad, and the jog keeps its per-object verdict. The fully enclosed
collinear duplicates (board 02 R3/2, R4/2) keep their per-object distance too;
their copper beyond the entry conductor is empty, so a union view would report
nothing at all. Per shape, `tests/test_mask_copper_own_pad_8858.py` pins every
#8914 danger item (1-5) and the U1/13 jog at its per-object distance. No
threshold, disposition or count changes; the parity row note already records
that jogged exits stay errors.


## Reproduce

```bash
KCT_KICAD_DOCKER=1 uv run python -m kicad_tools.cli.kicad_toolchain --check   # 10.0.6
KCT_KICAD_DOCKER=1 uv run kct check <board>/output/<name>_routed.kicad_pcb --mfr jlcpcb --format json
# pinned plot: kicad-cli pcb export gerbers --layers F.Cu,B.Cu,F.Mask,B.Mask --no-protel-ext -o out/ <pcb>
```

| Family | Board | Opening | Copper | Location (mm) | Measured |
|---|---|---|---|---|---|
| A1 | 01 | R1/1 F.Mask | segment VIN | (147.375, 83.000) | info; plot: beyond pad 0, beside 0.045 mm² ≈ one 0.5 mm crossing |
| A3 | 03 | SW3/1 F.Mask | segment `e0343df4` (154.5,107.1)→(154.9,107.1) | on the pad's top edge | error, `E` = 0.057 mm² |
| B | 01 | R2/2 F.Mask | GND zone | (149.500, 92.000) | error; plot: beside 0.108 mm² |
| C | 01 | R1/2 F.Mask | via VOUT, (149.5, 83.9), drill 0.3 | (149.500, 83.575) | error (ring beside the opening) |
| D | 03 | U1/13 F.Mask | segment RESET (jog after the exit) | (141.575, 82.300) | error, 0.025 mm per object |
| E | 02 | D2/1 F.Mask | via NODE_A | (148.050, 85.499) | error, 0.050 mm |

## Sources

- JLCDFM "solder mask opening exposing trace" on the #8914 board: 5 danger and 8
  warning, all same-net, with item geometry recorded in #8914. The consumer's
  family table is on #8858 (2026-11-17).
- Pour clearance is not fab-judged (#8938), but that result is about pour gaps.
  It is not about pour beside a mask opening, which is why B fails closed.
- Fab parity table row `solder_mask_opening_exposing_trace`, v11.
