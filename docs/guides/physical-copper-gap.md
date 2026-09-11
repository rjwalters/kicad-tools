# Physical copper gap preflight

`kct check board.kicad_pcb --drc-only --only physical_copper_gap --physical-copper-gap 0.25 --format json`

This opt-in check measures open slits between facing copper boundaries regardless
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
circle/oval/rect/roundrect pads, via copper and stored filled-zone polygons. It
retains source identities separately from the union. Zone outlines are not copper:
refill zones first. Unfilled zones, custom/unsupported pad shapes, unsupported
copper graphics and malformed arcs emit blocking `physical_copper_gap_incomplete`
findings rather than silently claiming full coverage. Keepout areas do not add
copper. Footprint-library metadata and unplated drill holes are not copper.

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
