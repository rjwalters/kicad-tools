# Routing development record — 2026-09-10

These `.py.txt` files capture development procedures for engineering review.
They retain their scratch paths and are not the supported release command.
Candidate selection, selective repairs and native refill occurred between runs;
running one snapshot is not a claim of reproducing the final board. The supported
reproduction is `real_design/build.py`, using independently regenerated circuit
identity, the reviewed physical artifact and fresh validation.

The measured design progression was:

1. Keep the reviewed package escapes and completed power copper. Reserve In1
   for ground and In4 for 3.3 V. Route data/DQM only on F/In2, and address/control
   only on In3/B beyond the immutable front package escapes.
2. A planar pass connected 35 of 38 bus nets in 18.6 seconds of geometric search,
   with no new holes. Native DRC found zero physical errors, but the resulting
   22–96 mm lengths did not meet timing. This candidate was not released.
3. Use a bounded two-layer search per group with full-board obstacles for every
   standard through via. Match each routed member before later nets consume its
   available tuning area. Accepted meanders were checked against continuous
   foreign copper, existing own-net copper, board edges and minimum leg spacing.
4. Repair A1's search-limit failure and A5's clock-via spacing explicitly. Finish
   the four auxiliary nets on the matched board. Two residual short nets used
   reviewed real-via waypoints: DQ14 at board-relative (85, 48) and A10 at (25, 68).
   Both obey their group layer pairs and the actual group/clock budgets.
5. Remove 18 fanout barrels connected to only one signal layer, then repeat native
   and strict connectivity checks. Normalize stock silkscreen strokes to 0.15 mm
   and add operator labels. These finishing steps changed no component pin map.

The clock uses a broad back-layer corridor through (65, 62.5), rather than a
repeated serpentine, with a measured 72.953 mm electrical length and 13.521 pF
external-load estimate. The final byte/address/command spreads and model limits
are in the design README and the archived validation evidence.

Related tool investigations: full-span routing vias [#5013](https://github.com/rjwalters/kicad-tools/issues/5013),
explicit reference-plane reservations [#5014](https://github.com/rjwalters/kicad-tools/issues/5014),
asymmetric stripline [#5016](https://github.com/rjwalters/kicad-tools/issues/5016),
native dielectric sublayers/reference selection [#5017](https://github.com/rjwalters/kicad-tools/issues/5017)
and [#5018](https://github.com/rjwalters/kicad-tools/issues/5018),
per-driver load budgets [#5020](https://github.com/rjwalters/kicad-tools/issues/5020),
and symmetric clock spacing [#5021](https://github.com/rjwalters/kicad-tools/issues/5021).
The need to reserve tuning room was already tracked by
[#4085](https://github.com/rjwalters/kicad-tools/issues/4085); this record does not
present that existing architectural idea as a new feature.
