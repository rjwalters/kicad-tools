# Thermal relief on small passives: sourced threshold and grade for `thermal_asymmetry` case 1

Research note for issue #9796 (the both-solid small-passive branch of the
`thermal_asymmetry` rule, #7220).  Compiled 2026-11-20.  Each source is
tagged **[read]** (fetched and read in full while writing this note) or
**[snip]** (public table of contents / search excerpt only -- the full paid
text was not read).  How sources were obtained: plain HTTP fetches of the
public pages, except the two IPC tables of contents, which IPC's storefront
publishes freely as evaluation PDFs.

## Headline findings

1. **The mechanism is consensus: a pad tied solid to a large copper area is a
   heatsink, and that causes assembly defects.**  Eurocircuits (assembler DFM
   guidance): "Large copper areas act as Heatsinks and therefore connecting a
   component pad to one as in the graphics above would most likely result in
   Tombstoning" [E].  JLCPCB: "A pad connected directly to a large copper
   plane can act as a heat sink and reach the solder's liquidus temperature
   later than a pad connected to a narrow trace"; the fix they name is "Give
   the plane-side pad thermal relief spokes, not a solid pour" [J].
2. **Risk rises as the part shrinks, and the practical risk band ends below
   0603.**  JLCPCB's size ladder: 0201 (≈0.15 mg) "one of the highest-risk
   chip sizes", 0402 (≈0.6 mg) "a practical concern", 0603 (≈2.1 mg, ~3.5× an
   0402) "tombstoning becomes rare rather than routine", 0805+ "generally
   less common" [J].  This is the size susceptibility the issue's "0402 and
   smaller" band rests on.
3. **No located published source sets a numeric size threshold for relief
   spokes on SMD chip pads.**  IPC-2221B covers thermal relief for plated
   holes in planes (public TOC §9.1.3 "Thermal Relief in Conductor Planes",
   Fig 9-4); nothing in its TOC or in IPC-7351B's TOC (the SMD land-pattern
   standard) contains a thermal-relief requirement or size threshold for
   chip-pad-to-pour connections [I1][I2].  Eurocircuits and JLCPCB give the
   mechanism and the fix but no threshold either [E][J].  The threshold below
   is therefore a **conservative default, not a standard requirement**.
4. **KiCad's override chain is pad → footprint → zone.**  The KiCad
   S-expression file-format spec: the pad's `zone_connection` "If not
   defined, the footprint zone_connection setting is used", and the footprint
   token list includes `(zone_connect CONNECTION_TYPE)` [K].  The rule
   resolves the effective mode through that chain (this landed with #9796;
   before it, a footprint-level `(zone_connect)` was ignored).

## What each source actually says

- **[E, read] Eurocircuits, "Tombstoning", PCB assembly guidelines.**
  Causes: track-size imbalance, PTH near a pad, **pads connected to large
  copper areas**, via in pad.  "What it really comes down to is the difference
  in thermal mass between pads of the same component."  For pads into pours
  they prescribe an equalising track run ("at least 0.25mm of track before it
  connects to the copper area, in fact the longer the better") — i.e. a
  thermal break on both pads, which is what relief spokes formalise.  No size
  threshold given.
- **[J, read] JLCPCB blog, "PCB Tombstoning: Causes, Prevention & Fixes"
  (2026).**  Mechanism, size ladder (finding 2), and the two layout fixes:
  "Give the plane-side pad thermal relief spokes instead of a solid
  connection" and "Keep both pads on the same copper layer with identical
  trace widths".  A vendor blog, but the only located source that names sizes
  and masses per package.
- **[M, read] Murata MLCC mounting FAQ 0004.**  Process-side confirmation
  that preheat ΔT control "also prevents tombstoning"; nothing on pad layout.
- **[I1, snip] IPC-7351B public TOC.**  "Generic Requirements for Surface
  Mount Design and Land Pattern Standard".  No section title mentions thermal
  relief or tombstoning; the standard governs land-pattern geometry, not
  pad-to-pour connection policy.
- **[I2, snip] IPC-2221B public TOC.**  §9.1.3 "Thermal Relief in Conductor
  Planes" (p. 97, with Fig 9-4 "Typical Thermal Relief in Planes") sits in
  the *hole/land* requirements chapter — plated holes in planes, not SMD chip
  pads.  The PTH half of our rule (barrel fill) is the closer cousin of what
  IPC-2221 does cover.
- **[K, read] KiCad dev-docs, S-expression file formats (footprint and pad
  spec).**  Footprint token 18 `(zone_connect CONNECTION_TYPE)`; pad token
  `zone_connection` with "If not defined, the footprint zone_connection
  setting is used"; values 0 = none, 1 = thermal relief, 2 = solid fill.

## Threshold chosen

The both-solid (case 1) finding is gated on the **outer copper span of the
part's two pads** (footprint-local bounding box of the pads, larger of width
and height, so a 90°-rotated part measures the same):

| Land pattern (committed-fleet measurement) | Copper span |
|---|---|
| 0402 / 1005Metric (`R_0402_1005Metric`, board 03) | 1.56 mm |
| 0603 / 1608Metric (`C_0603_1608Metric`, board 07) | 2.45 mm |

`SMALL_PASSIVE_COPPER_SPAN_MM = 1.75` — the midpoint band between the 0402
and 0603 land-pattern spans.  It admits 0402, 0201 and 01005 (and
IPC-7351 density variants of 0402, whose pads grow a few hundredths of a mm)
and excludes 0603 and above, where the located guidance says tombstoning
"becomes rare rather than routine" [J].  Measuring the pad copper rather
than parsing the footprint name keeps the gate working for custom and
vendor footprints whose names carry no size code.

Why gate case 1 at all when case 2 (#7220) has no size gate?  Asymmetry is a
defect driver at any size (the solid pad is always later than the other);
both-solid pads only become a joint-formation problem when the part's own
thermal mass is small, so a large both-solid passive is not worth a warning.

## Severity chosen

**Warning**, identical to the existing case-2 and PTH findings of the rule:
the rule id stays in the advisory category, plain `kct check` exit codes and
readiness gating are unchanged, and findings are waivable per pad
(`items = [ref, ref-pad, ref-pad]`, same waiver keys as case 2).  No located
source grades this defect class; advisory-warning matches how the repo grades
the sibling thermal findings (#7220) and how the operator graded the related
`mask_to_copper` own-pour finding as info (ruling 2026-11-20, #9441) —
a solid-pour *assembly-yield* concern, not a clearance error.

## Sources

- [E] Eurocircuits, *Tombstoning*, technical-guidelines/pcb-assembly-guidelines.
  https://www.eurocircuits.com/technical-guidelines/pcb-assembly-guidelines/tombstoning/ **[read]**
- [J] JLCPCB, *PCB Tombstoning: Causes, Prevention & Fixes*. https://jlcpcb.com/blog/pcb-tombstone **[read]**
- [M] Murata, *Reflow soldering of chip MLCCs*, Capacitor FAQ mnt/0004.
  https://www.murata.com/en-global/support/faqs/capacitor/ceramiccapacitor/mnt/0004 **[read]**
- [I1] IPC-7351B, *Generic Requirements for Surface Mount Design and Land
  Pattern Standard* — public table of contents. https://www.electronics.org/TOC/IPC-7351B.pdf **[snip]**
- [I2] IPC-2221B, *Generic Standard on Printed Board Design* — public table
  of contents. https://www.electronics.org/TOC/IPC-2221B.pdf **[snip]**
- [K] KiCad, *S-expression file formats* (footprint and pad tokens).
  https://dev-docs.kicad.org/en/file-formats/sexpr-intro/ **[read]**
