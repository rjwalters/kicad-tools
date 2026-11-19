# Physical copper gap preflight

`kct check board.kicad_pcb --drc-only --only physical_copper_gap --physical-copper-gap 0.25 --format json`

## Default under `--mfr jlcpcb` (fab view, Issue #8843)

Under `--mfr jlcpcb` (explicit or resolved) the net-agnostic "fab view" runs with
no extra flag, mirroring JLCDFM, which measures copper from gerbers without net
knowledge. Thresholds are JLCDFM *observations* from epic #8838, not fab-documented
limits, and live in one table at the top of `validate/rules/physical_gap.py`:

| Check | Band | Severity |
|---|---|---|
| Copper gap (tracks, pads, vias, fills; same-net included) | < 0.10 mm | `error` |
| Copper gap | 0.10 to < 0.15 mm | `warning` |
| Pad-to-pad mask bridge on **2 oz** copper (exception below) | < 0.20 mm | `error` |
| THT pad (any drill) or via drilled >= 1.0 mm, to SMD pad; via drilled >= 1.0 mm, to exposed via annulus except a same-net anchor↔anchor pair (`tht_smd_spacing`) | < 2.032 mm / < 3.05 mm | `error` / `warning` |

Same-net near-parallel copper (e.g. 0.08 mm) is always listed; it uses the same
tiers. The mask-bridge check needs a known copper weight (stackup or `--copper`);
with the weight unknown it emits a `physical_copper_gap_incomplete` *info* finding
(listed, not blocking) rather than assuming 1 oz, and weights other than 2 oz are not judged (versioned per-weight
table: epic #8838 item 2). The mask-bridge check runs on the outer copper
layers (`F.Cu` / `B.Cu`) only (Issue #8913): solder mask exists only as
`F.Mask` / `B.Mask`, so there is no inner-layer mask to bridge, and a
through-hole pad pair yields at most one finding per outer side rather than one
per copper layer. No inner-layer mask-bridge findings on a 4-layer board is the
intended scope, not a missed case. A same-footprint, same-net pad pair is
exempt (Issue #9420): two pads of **one footprint** on **one identical
non-empty net** are the land pattern of a stock module (its ground pad array),
which a consumer cannot edit, so that finding reads `info`, still listed with
distance and items intact; different footprints, different nets and no-net
(net 0) pairs keep the `error` -- [mask-to-copper](mask-to-copper.md) documents
the located fab evidence and the `mask_to_copper` twin exemption.
`tht_smd_spacing` measures **copper edge to copper
edge**; the epic does not say whether JLCDFM measures hole centre, hole edge or
copper edge, so this is a documented choice.

`tht_smd_spacing` judges a plated hole by its **drill diameter**, not by whether
KiCad stores it as a footprint pad or a via (Issue #8914): JLCDFM flagged
1.3-1.42 mm wire-anchor vias that the rule previously never looked at, because
the only way to include vias was an `include_vias=True` argument no `kct check`
code path ever passed. Component THT pads are judged at every drill size; a via
is judged once its drill reaches `ANCHOR_DRILL_MM` (1.0 mm, just under 18 AWG
wire). Judging *every* via is still wrong -- it produced 176 to 1,068 findings
per board on this repo's routed boards, against 0 to 6 at the 1.0 mm default,
where every via drills 0.15-0.3 mm. A THT or SMD pad whose `shape` the rule
cannot reproduce exactly (`trapezoid`; a `custom` pad whose primitives the
outline builder refuses) reports `tht_smd_spacing_incomplete` at *info* rather
than being dropped silently, so missing coverage never looks like a cleared
pad. A `custom` pad the builder *can* reproduce is measured by its
anchor-plus-primitives copper (Issue #9498).

`clearance_incomplete` -- the clearance rule's own finding for a refused
`custom` pad -- is *info* by design (Issue #9539), though the sibling
`physical_copper_gap_incomplete` is blocking for the same class of gap. The
passes answer differently shaped questions: the physical-gap pass asserts a
layer-wide minimum among *all* copper, so unmodelled copper would make a
clean verdict itself unsound, and it refuses to answer rather than answer
from a subset; the clearance rule measures pairs of known copper, so a
refused pad degrades to its measured anchor, every pair it does report is
still real, and the unmodelled remainder is named in the finding rather than
silently cleared. Raising it to blocking would gate most real custom-pad
boards on a coverage gap, not a defect: the stock-library census counted more
refused pads than exactly-evaluated ones, the blocking incomplete on custom
pads is itself the open report #9477 / #9528, and no tracked `boards/` file
contains a custom pad, so neither severity changes a committed verdict. The
residual missed-violation risk is covered by native DRC, which is the
authority for refused primitives: on the SOT-89 fixture family kicad-cli
reported `shorting_items` for a track overlapping a refused `gr_circle`
primitive's copper and a `clearance` error (actual 0.150 mm against the
0.200 mm rule) for a different track merely approaching it, while agreeing
with `kct check` on the supported gr_poly tab (measured 2026-11-19,
kicad-cli 10.0.7 with the pinned config tables; CI's pinned 10.0.6 image was
unavailable on the measuring host). KiCad loads only well-formed `gr_circle`
primitives (`center` plus `end`) -- the test fixture's centre-only token is a
kct-test spelling kicad-cli refuses to load -- so the probe used the
well-formed form, the only one a real board can carry.

An **exposed via annulus** also counts as the "SMD" side (Issue #8925): JLCDFM
flagged a 1.9 mm / 1.3 mm-drill anchor via 1.5 mm from an ordinary 0.8 mm /
0.4 mm via. Only an anchor *via* (drill >= `ANCHOR_DRILL_MM`) is paired against
via annuli -- a THT component pad never is (that reading added 24 findings to
board 05), and fan-out-vs-fan-out pairs are never judged. A same-net
anchor↔anchor pair is exempt (Issue #9325): consecutive `kct pcb reinforce`
wire legs sit ~2.3-2.5 mm apart on one net, the fab's DFM counted none of
those pairs, and reporting them produced 7 of the 11 errors on that board. The
fab's same-net treatment is evidenced only by that one board, not a general
fab-rule confirmation, and the exemption needs a real shared net: two
unconnected holes both read as net 0 (the no-net sentinel, issue #4127), so a
net-0 anchor pair stays reported. An annulus counts only on an outer face
where it is **not tented** (per-via `(tenting ...)` override,
then the board setup default, then KiCad's absent-token default: tented); the
fab's exact tenting test is unverified. A pair is reported once, naming every
layer it meets on. Every via on boards 03 / 05 / 06 / 07 resolves as tented, so
their counts are unchanged and the live calibration of this pairing is unverified.

**Design decision: flattened board geometry, not exported gerbers.** The pass runs
on the same per-layer copper union as the opt-in check: it exists and is tested,
needs no gerber export step, and avoids a new gerber-parsing dependency surface.

An explicit `--physical-copper-gap MM` still wins (single threshold, all `error`).
Other manufacturers are unchanged: coverage still reports
`skipped:opt_in:--physical-copper-gap` (and `skipped:opt_in:--mfr jlcpcb` for
`tht_smd_spacing`).

## Opt-in threshold

The explicit `--physical-copper-gap` form measures open slits between facing copper boundaries regardless
of electrical net identity. It unions copper on each layer before measuring, so
shared endpoints, overlapping tracks and pad escapes are not zero-clearance
errors. Branches connected elsewhere can still enclose a narrow slit. Electrical
short/connectivity checks remain separate and must still run.

The threshold is an explicit process choice, applied to all supported copper
boundaries. [JLCPCB's capability table](https://jlcpcb.com/capabilities/Capabilities)
(accessed 2026-09-10) lists **same-net track spacing: 0.25 mm**. That is a
track-specific requirement, not evidence that every pad/zone/different-net gap
requires 0.25 mm. Select the threshold appropriate to your reviewed construction;
this option does not change manufacturer profiles or native KiCad rules.

Python callers can set `DRCChecker(pcb, physical_copper_gap_mm=0.25)` or call
`check_physical_copper_gap(pcb, 0.25)` from `validate.rules.physical_gap`.
No copper is edited. Reports include measured gap, threshold, layer, net names,
source UUIDs (stable traversal labels when absent), and both measured boundary
locations in JSON. Reports aggregate the minimum observed gap per source set and
layer; a filled-zone UUID can represent multiple boundary regions.

## Coverage and limits

The checker uses stroked track widths, modern start/mid/end copper arcs, rotated
circle/oval/rect/roundrect pads, via copper, footprint copper polygons and stored
filled-zone polygons. It retains source identities separately from the union. Zone
outlines are not copper: refill zones first. Unfilled zones, custom pads whose
primitives the outline builder refuses (named, with the primitive kind --
Issue #9498; a reproducible custom pad contributes its anchor-plus-primitives
copper), other unsupported pad shapes, unsupported copper graphics and malformed
arcs emit blocking `physical_copper_gap_incomplete` findings rather than
silently claiming full coverage. Keepout areas do not add copper.
Footprint-library metadata and unplated drill holes are not copper.

A footprint copper polygon (`fp_poly` on a `.Cu` layer) is modeled on the layer the
polygon itself declares, with the footprint's placement applied: a filled polygon
(`(fill yes)`, legacy `(fill solid)`) contributes its whole interior, dilated by
half its stroke width when the outline is also stroked, and an unfilled polygon
contributes only the stroked ring so a slit inside it stays measurable. This is the
copper a standard `NetTie-*` footprint uses to join its pads; the join itself is
removed by the union like any other overlap, so it is not reported as a zero-width
slit, while a separate boundary closer than the threshold still is (issue #5817).
Mirroring needs no separate term because KiCad stores a back-side footprint's child
vertices already mirrored and its orientation already negated. A polygon this rule
cannot model exactly -- fewer than three vertices, non-finite coordinates, a copper
layer the board does not declare, a negative/non-numeric stroke, or a polygon that
is neither filled nor stroked -- produces `physical_copper_gap_incomplete`.

Curves are polygonal approximations, not an exact native-DRC or factory simulator.
Track arc chord error is at most 0.0001 mm; pad curves use the existing clearance
geometry tessellation. Comparisons have 0.001 mm tolerance. Very large curved pads
can exceed this approximation budget; use native/factory review for marginal
curved cases. Gaps at or below the tolerance are not distinguished from contact.
Only mutually facing boundary edges (within 25 degrees of the measured connector)
are candidates; acute/tapered notch completeness is not guaranteed. Local joined
source shoulders within one threshold of their actual overlap are excluded;
short slits wholly inside this neighbourhood may be missed. These limits avoid
turning ordinary valid joins into a flood of artificial manufacturing defects.
The check samples nearest and midpoint projections of boundary edges, not a full
medial-axis solution. A clean result is not an unconditional manufacturability
certificate, and it does not validate fill freshness or factory acceptance.

Malformed numeric source geometry is rejected before tolerant schema defaults can
become copper. Missing/repeated segment fields, invalid coordinates or widths,
invalid via spans, malformed pad sizes/ratios and unsupported chamfer modifiers
produce `physical_copper_gap_incomplete`. For these raw-source failures the
geometry collection is stopped; an empty result must never be read as a clean
gap verdict. Native chamfered pads require a future exact geometry implementation.

Explicit pad and via `padstack` definitions are unsupported, including stacks
whose current values happen to match the nominal size. They produce blocking
`physical_copper_gap_incomplete` findings before any nominal geometry is used;
the nominal pad/via shape must not substitute for layer-specific copper. Ordinary
pads and vias without a `padstack` retain the supported behavior above.
