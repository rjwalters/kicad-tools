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
- pours under and around the pad (no fab result covered pour beside an opening
  at the time; #9085's declaration is the route for a reviewed own pour). Since
  #9284 a pad joined to its own pour by thermal spokes is `own_pour_thermal`
  info; see "Family B revisited" below;
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
| B. own pour under and around the pad | 5 | 0 | 51 | error: fails closed (pour ring beside the opening); since #9284 the spoke-connected ones are info (01: 5, 03: 45) |
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
| 03 | 422 → 183 | 385 → 146 (239) | 79 | 24.7 / 26.0 s |
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

The fab's item coordinates are not in the record, so I cannot match the five
fab dangers to objects. The consumer's re-test of `c96d21e9a` on #8858
([comment](https://github.com/rjwalters/kicad-tools/issues/8858#issuecomment-6960603314))
located them **by pad (opening owner)** and did show that all five survive: each
of the five fab-danger pads still has at least one track or via finding at
warning level (two through a pass-through segment plus the via, one through
the via and two of its four entry segments, two through a pass-through segment
alone). That is evidence per pad, not per object. All five are described as
beside, along, bending or branching shapes, and per object those are either
non-entry segments (errors) or entries with excess beside copper (errors). None
is described as a clean straight entry. The severity there is `warning` rather
than `error` only because the assessment is provisional.

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
that jogged exits stay errors. Whether the entry-allowance tolerance itself
belongs in this rule — the open question the owner raises against the
post-#9108 consumer measurement in the #9107 thread — is tracked separately
in #9305, so the pointer survives this issue's close.

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

## Family B revisited: own pour joined by thermal spokes (issue #9284)

### Fab evidence

Board 01's committed Gerber package (sha256 `36c0905e…`, from main `95b8327ce`)
went through JLCDFM's DFM analysis on 2026-11-17, recorded on #8858
([comment](https://github.com/rjwalters/kicad-tools/issues/8858#issuecomment-6963569039)).
"Solder mask opening exposing trace" (danger 0.08 mm, warning 0.15 mm) lists three
items, all a pour against a **different-net** pad opening on the top mask: R2.1
warning at 0.13 mm, R1.2 good at 0.2 mm, R1.1 good at 0.2 mm. The own-pour pads
R2.2, J1.2 and J2.2 have **no row at all**, and the bottom mask has no entry. So
the shape is *not listed*, which is weaker than a measured pass; the reporter
infers that the tool treats copper connected to the pad as one conductor.

### What pcbnew declares on board 01 (pinned image, black-box API reads)

| Pad | Shape | Connection (source) | Relief gap | Spoke width | Pour body clearance measured |
|---|---|---|---:|---:|---:|
| R2.2 (F) | roundrect, SMD | thermal (zone; pad overrides gap/spoke) | 0.2 mm | 0.3 mm | ≥ 0.19 mm |
| J1.2, J2.2 (F, B) | oval, PTH | thermal (zone) | 0.3 mm | 0.4 mm | ≥ 0.29 mm |

Every pad has 4 spokes; mask expansion is 0 mm (`pad_to_mask_clearance 0`).
"Pour body clearance measured" is the widest band around the opening in which the
fill is still spokes only (bisection with the class's own test), so it bounds
the body's distance from below. **The tested relief gaps are 0.2 and 0.3 mm.**

### The class: `own_pour_thermal` (info)

`_own_pour_thermal` in `src/kicad_tools/validate/mask_copper.py`. A same-net
`zone` measurement over a single plain-pad opening qualifies only when all of:

1. plain `rect`/`roundrect`/`circle`/`oval` owner, mask margin ≥ 0;
2. **pcbnew declares thermal relief**: pad override, else footprint override,
   else the zone's `connect_pads`; "thermal for through-hole only" counts only on
   a plated through-hole pad. The worker reads `GetLocalZoneConnection` (pad and
   footprint), `GetPadConnection`, the gap and spoke-width overrides and the zone
   defaults. Custom `zone_connection` rules already make the run incomplete;
3. the fill overlaps the pad beyond `u` (the spokes reach in);
4. nothing exposed beyond the pad (same test as `within_own_pad`);
5. within the **examined band** `b` (the policy's warning clearance, 0.15 mm for
   JLCPCB; the danger clearance without one) the fill under mask is spokes and
   nothing else. Each connected piece of `(fill − O.buffer(u)) ∩ O.buffer(b + u)`
   must touch the pad, reach the band's outer edge, meet each band edge along at
   most `1.45·s` (`s` = declared spoke width), not wrap around the pad, and hold at
   most `1.45·s·b`; 1 to 4 pieces. The contact-length condition matters: a body
   just inside the band adds little *area* to each spoke piece, but it lies along
   the band's outer edge.

**Which copper is excused:** the spokes, each crossing the pad edge, out to
0.15 mm from the opening. The pour body is never excused: it must lie at least
0.15 mm from the opening (board 01's bodies sit at 0.2 and 0.3 mm). A pour body
between 0.09 and 0.15 mm is untested, so it stays an **error** rather than
dropping to a warning; that is stricter than a different-net pour at the same
distance (a warning), and deliberately so until a fab result covers it.

**Stays an error:** a solid connection (declared, or a thermal declaration
whose fill is solid); a same-net pour not connected to the pad, or another
same-net zone or island in the band; a pour body inside 0.15 mm; spokes wider
than declared or more than four pieces; any positive expansion wider than `u`;
different-net pour keeps its measured verdict.

**Monotone.** Sweeps in `tests/test_mask_copper_own_pour_9284.py` (1 × 1 mm pad,
0.3 mm spokes): relief gap 0.30 → 0.00 mm in 0.01 mm steps is info down to
0.16 mm and error from 0.15 mm inward; actual spoke width 0.20 → 0.80 mm against
a declared 0.3 mm is info, then error for good; expansion 0 → 0.2 mm is info at
0 and error for every positive value. Severity never drops as copper moves closer
or widens.

**Native.** `test_native_own_pour_thermal_only_for_spokes` fills the pour with
the pinned pcbnew and checks the Gerbonara-read plot: spoke-connected (gaps 0.2
and 0.3 mm) is info with nothing beyond the pad and exactly four spoke pieces in
the 0.15 mm band; solid, unconnected same-net (0.05 mm), 0.05 mm expansion, and
a 0.1 mm relief gap are errors; a different-net pour at 0.12 mm is a warning.

### Effect, boards 00-07 (`--mfr jlcpcb`, pinned image, 2026-11-17)

| Board | errors before → after | `mask_to_copper` err → after | m2c info before → after | `own_pour_thermal` | wall before / after |
|---|---|---|---|---:|---|
| 00 | 4 → 0 | 4 → 0 | 2 → 6 | 4 | 4.9 / 4.0 s |
| 01 | 7 → 2 | 7 → 2 | 5 → 10 | 5 | 4.2 / 4.4 s |
| 02 | 51 → 51 | 37 → 37 | 90 → 90 | 0 | 10.0 / 10.6 s |
| 03 | 183 → 138 | 146 → 101 | 239 → 284 | 45 | 25.1 / 27.3 s |
| 04 | 61 → 59 | 27 → 25 | 42 → 44 | 2 | 9.8 / 10.3 s |
| 05 | 185 → 147 | 156 → 118 | 200 → 238 | 38 | 37.8 / 40.0 s |
| 06 | 48 → 48 | 48 → 48 | 132 → 132 | 0 | 16.8 / 17.4 s |
| 07 | 117 → 117 | 88 → 88 | 253 → 253 | 0 | 46.7 / 48.6 s |

Warnings are unchanged on every board. Board 01's two remaining errors are the
two via rings (family C), unchanged.

**Board 03's 51 own-pour findings:** 45 are spoke-connected (thermal declared,
spokes confirmed in the fill, relief gap 0.2 mm, spoke 0.25 or 0.3 mm; fill spokes-only out to
≥ 0.19 mm from the opening) and are now info. 6 are **solid** (pad-level solid override: U1 pads
5, 15, 23, 35, 43 and U2 pad 2) and stay errors. Board 04 has 11 spoke-connected
pads with a **0.1 mm** relief gap; their pour body sits inside the 0.15 mm band,
so they stay errors (untested geometry), plus 5 solid pads. Board 05 has one
solid pad that stays an error.

### Parity in the other direction (different-net pour, board 01)

| Pad | Fab | kct (`--mfr jlcpcb`) | Agrees |
|---|---|---|---|
| R2.1 | warning, 0.13 mm | warning, 0.1275 mm (GND zone vs VOUT pad) | yes |
| R1.2 | good, 0.2 mm | no finding (beyond the 0.15 mm reach) | yes |
| R1.1 | good, 0.2 mm | no finding (beyond the 0.15 mm reach) | yes |

kct is not silent where the fab warns, so no threshold issue is filed. kct also
warns at 0.1275 mm for J1.1 and J2.1 (PTH, different net, both sides), where the
fab's list has no row; kct is the stricter of the two there.

### Via ring over its own pad (family C): not excused

The fab's "Via placed within a pad" and "Via to pad" are both 0/0/0 on board 01.
The only fab items at those vias are indirect (the R2.1 mask warning at the
corner where the via's clearance meets the pad's, and a 0.04 mm silkscreen-to-hole
danger on the R2.1 via). Silence in those categories is not a measured pass for
mask exposure of the ring beside the opening, so family C stays an error.

## Sources

- JLCDFM "solder mask opening exposing trace" on the #8914 board: 5 danger and 8
  warning, all same-net, with item geometry recorded in #8914. The consumer's
  family table is on #8858 (2026-11-17).
- Pour clearance is not fab-judged (#8938), but that result is about pour gaps.
  It is not about pour beside a mask opening, which is why B fails closed.
- Fab parity table row `solder_mask_opening_exposing_trace`; #9284 added the
  `own_pour_thermal` class and the different-net parity check. The table has no
  integer version: `fab_dfm_parity.TABLE_VERSION` is a content hash of the rows
  (`3559f167` as of 2026-11-18 on main `f2347f38d`; re-read
  `fab_dfm_parity.TABLE_VERSION` for the current value).
- JLCDFM result for board 01's Gerbers, own pour and via ring: #8858, 2026-11-17.

## Residual after the router repairs, and the disjoint class verified against the plot (2026-11-18, main `f77b80fb5`)

Re-measured on the pinned toolchain (`kicad-cli` 10.0.6) after the board-side
and rule-side fixes that landed since the table above (#9264 entry allowance,
#9275 route-path slit band, #9295 escape via placement). Two surfaces, because
the CI gates judge two different artifacts:

- **Fresh CI-recipe regenerations** (`--seed 42`, the `regression-output/`
  trees the regression gates route from scratch): board 06 **200 / 45 / 444**
  errors / warnings / info, board 07 **54 / 53 / 412**. Class split, board 06:
  122 pass-through, 78 own-pad entry; 80 disjoint, 120 overlap. Board 07: 29
  pass-through, 7 own-pad entry, 18 pour-beside-own-pad overlap (the shape
  #9284's thermal-spoke class addresses; the zones connect `connect_pads`
  thermal with a 0.3 mm gap), 7 disjoint, 47 overlap. Reruns of the same
  output are identical (deterministic).
- **Committed `output/` PCBs** under the current rule: board 05 121 / 122 /
  236, board 06 48 / 0 / 133, board 07 67 / 31 / 275. (The two numbers are
  different artifacts, not before/after of one route: boards 06/07 are
  re-routed from scratch on each regeneration, and only the fresh tree is
  what the gates judge. Why fresh 06 is the larger number is not
  investigated here.)

Wall time, board 05 committed PCB, one run each on a loaded shared worker:
**122.4 s** with the pass, **42.1 s** with `--skip mask_to_copper`
(+80 s wall, +46 s user).

The committed evidence above plot-checked the excused (`within_own_pad`)
findings; the disjoint/error class had only been classified from the rule's
own messages. All 7 fresh-07 disjoint findings are now checked the same
black-box way — mask and copper read with the independent Gerbonara oracle
(the `tests/_mask_gerbonara_oracle.py` object loop, plus the X2 `TO.P` /
`TO.N` attributes the pinned plot carries), conductor geometry from the
authored board:

| Opening | Side | Conductor | plot distance | rule verdict |
|---|---|---|---|---|
| J3/4 | F | A2 track cap leaving at 45° | 0.09 mm | uncertain (on the boundary) |
| U4/B5 | F | GND track | 0.0485 mm | violation |
| U4/B5 | F | GND track | 0.0872 mm | uncertain |
| J3/7 | B | A5 track cap leaving at 45° | 0.07 mm | violation |
| U5/13 | F | GND track tangent to the pad edge | ~0 (authored 0.0) | violation |
| U3/7 | F | same tangent shape | ~0 (authored 0.0) | violation |
| U2/13 | F | same tangent shape | ~0 (authored 0.0) | violation |

Every opening is exactly its pad (`area(O − P)` = 0; the J3 pads plot as the
authored 1.5 mm circles). Verdict: **real**. The disjoint measurements are
same-net track caps and tangents 0–0.09 mm from a real mask opening — all
inside or on the rule's own danger clearance `c` = 0.09 mm (§ "The
criterion"). Against the consumer board's observed fab limit the split is
narrower: five findings inside the fab's 0.08 mm danger band, and two — the
0.0872 mm and 0.09 mm rows, marked `uncertain` — in its warning band
(0.08–0.15 mm). The three tangent cases
are authored-touching (distance 0.0, zero-area contact); the rule's plot
reconstruction reads a µm-positive distance, so the relation label flips
touching/disjoint on plot polygonalization. The finding is real in both
readings: the fab sees copper at ~0 mm from the opening.

Conclusion, in the operator ruling's terms: the residuals are real, so the
five `--skip mask_to_copper` CI gate skips stay until the boards are repaired
(the #9326 path). The pour-beside-own-pad overlaps await #9284; the
overlap-with-excess severity question is the one raised for the rule owner on
#8858.

Reproduce:

```bash
KCT_KICAD_DOCKER=1 uv run python scripts/ci/check_matchgroup_coverage.py \
  boards/07-matchgroup-test --seed 42
KCT_KICAD_DOCKER=1 uv run kct check \
  boards/07-matchgroup-test/regression-output/matchgroup_test_routed.kicad_pcb \
  --mfr jlcpcb --only mask_to_copper --format json
# pinned plot for the oracle: kicad-cli pcb export gerbers \
#   --layers F.Cu,B.Cu,F.Mask,B.Mask --no-protel-ext -o out/ <pcb>
```

## Board 06's residual baselined by net signature; its diffpair-gate skip removed (2026-11-18, main `304c70e2c`)

The authorisation for baselining this fixture gate is #7270 Q3 -- a known
finding on a synthetic regression-fixture gate is excepted by identity plus
a tracking issue number, never by count -- together with #8858's curated
acceptance criteria ("If real: baseline the findings ... Either way remove
the `--skip mask_to_copper` gates") and the Curator's 2026-11-18
re-curation ("Remove them only per board, once that board's residual is
repaired or baselined"). This does not conflict with the operator ruling's
"no allowance entries" condition (#8858 comment 6938665789, restated on
#8900): that condition governs the release surface -- board check/readiness
output, where it sits beside "no acknowledgement of fiducial or board-ID
warnings" -- not this synthetic regression-fixture gate, and the same
ruling also prohibits the interim `--skip mask_to_copper` this change
removes. `check_routed_drc.py`, the release surface, keeps its skip
untouched. On that basis the diff-pair gate's `--skip mask_to_copper`
(#8842) is replaced by identity entries rather than waiting for the
#9326/#9332 repair path. Measured on the
pinned toolchain (`KCT_KICAD_DOCKER=1`; `kicad-cli` **10.0.6** in
`kicad/kicad@sha256:18693567` — the digest CI's `KICAD_IMAGE` pins), fresh
seed-42 regeneration of board 06:

- **200 `mask_to_copper` errors** (122 same-net pass-throughs, 78 same-net
  own-pad entries; relation split 182 violation / 18 uncertain), alongside
  the 13 identity-allowed `physical_copper_gap` findings (MIPI_CLK+ 11,
  GND 2). Class split matches the table above.
- **Net-signature stability**: two independent regenerations reproduced all
  **22** signatures and their per-net counts exactly (USB2_D- 18,
  USB2_D+ 17, MIPI_RST 16, MIPI_CLK- 15, MIPI_CLK+ 14, PCIE_TX+ 14,
  PCIE_RX+ 13, PCIE_TX- 13, PCIE_RX- 12, USB3_TX1- 11, USB3_RX2+ 10,
  USB_CC1 9, USB_CC2 9, USB3_RX2- 8, GND 5, MIPI_D0- 4, +1V2 3, +3V3 3,
  MIPI_D0+ 2, USB3_RX1- 2, USB3_RX1+ 1, USB3_TX2+ 1). The #8957 model
  applies: the rule's `items` are opening/conductor UUIDs re-minted by
  every regeneration, so `mask_to_copper` joins `NET_IDENTITY_RULES` and
  the rule now names `nets` (owner pad + exposed conductor) on each
  finding — additive metadata, severities unchanged.
- **Gate**: `check_diffpair_coverage.py` drops the skip; the 22 net
  signatures are listed in `DIFFPAIR_FIXTURE_ALLOWED_FINDINGS`, each
  citing #8858. Full gate rerun end-to-end (route + check) exits 0: 213
  errors, 213 identity-allowed, 0 unallowed, 0 stale. A net that starts
  carrying mask errors fails the gate; a net the router stops exposing
  goes stale and forces its entry out (the same ratchet the #8957 slits
  are on; entries drain via the #9326/#9332 repair path).
- **Wall, `kct check` on the fresh board 06 PCB** (loaded shared worker,
  one run each): **49.6 s / 47.8 s** with the pass, **16.8 s** with
  `--skip mask_to_copper`; the gate's own re-route step 250-276 s. The
  board 05 numbers below are unchanged.

The other four skips stay: board 07's matchgroup gate, `check_routed_drc.py`
(the release surface) and `board_05_unattended.py` await their own
per-board increments.
