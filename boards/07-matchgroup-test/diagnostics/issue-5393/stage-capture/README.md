# DQ3 stage-boundary supplement

This supplement completes the missing producer-boundary investigation from
#5393 and reconciles the newer predicate evidence with the original #5397
report. It does not qualify Board07, close parent #3438, or waive the held
#5286/#5333/#5164 integration contracts.

## Scope and control

The historical `3ff7fbd9…` PCB and image remain unavailable. The earlier
unmodified replacement-runtime full recipe, DDR-only reproduction and failed
controls remain in the parent directory's original archive. This supplement
adds one instrumented route prefix and a separate file-finishing replay;
it is not another complete recipe run.

The prefix uses source `a6c016e2c3fa32f4db5b5f3f2334b02dde2a1db0`, tree
`cb4c5971ddbe20a4aaddaa7303cffc524de240fc`. All 3,777 tracked source files
match before and after the route prefix. Runtime matches the retained replacement
control: Python 3.12.14, KiCad 10.0.5, image
`sha256:0d6887c861dd9926a02cdb57e3d649b72fc547ff2aef6605c5416131794d2115`,
and the reused retained native extensions:

| Extension | SHA256 |
|---|---|
| Router | `185bf17c591c09e31046689f87a5e0b4f8bbb34b64cfd023a89251f373376cb0` |
| Placement | `f21fbd60f964dfa6411b14cc905f608750afe7b2c2a415a6c8d304b8bcbd11f1` |
| DRC | `17fa69d4537c5e73949005f6916c915ab6d0ecbaae5882fd5a7fd51daea7209a` |

No extension was rebuilt for this capture. The routing-frame input is
`a9089c137e5932e5c02bce0beb607bc9cd25ed292d175378ed38395d34767234`;
project, DRU and authored net-class sidecars have individual hashes in
`preflight.json`. The native audit sidecars come from the retained routed
control; their presence is distinct from each recorded fill operation's
working project context.

Seed and PYTHONHASHSEED remain 42. Search remains 600 seconds, placement
allows two 600-second probes, and the hard invocation cap remains 2,400
seconds. No capture pauses or extends a deadline. Worker1's separate
container had 4 CPU/12 GiB limits. At launch the 8-CPU host had approximately
12 GiB available; the independent Board06 peer used about one CPU/1.6 GiB
and later terminated. This additional capture is not labeled a solo
performance measurement. Host samples and child PIDs are retained.

## Capture neutrality and postprocessing

The prefix stopped intentionally after initial CLI native fill, before CLI
DRC and external recipe finishing, with exit 0. That exit means capture
completed, not routing passed. Wrapper elapsed time was 701.045 seconds
(includes at most one 20-second terminal-poll interval).

Initial negotiated routing ended 30/31 with DQ3 open. Placement feedback
reverted the U2 translation, reporting `routed 29 -> 29`, as did the retained
unmodified control. The 18 in-memory snapshots took 1.652 seconds total;
each asserts serialization left live route geometry and metadata unchanged.
The wrappers forward original search calls and preserve their returned
objects. Capture activates at the existing post-search quality boundary.

The final prefix copper and all pad geometry match the retained unmodified
partial PCB `dc2416d48ae85bd9d7291586789ffacf7fcf2e45e48304b18a7c8963512d045d`.
The independent native comparison checks all 897 copper items in board-relative
coordinates, retaining nets, layers, widths, via sizes/drills and pad geometry,
while ignoring UUIDs and the documented sheet translation. Copper multiset
hash is `b11704f0582d6e248e57e6b87bdc9d85ebe1e710dea26e8bf5c4b7d32cca74f3`.
This supports neutral capture of the selected result; it does not assert
identical timing or every transient search state.

| Boundary | Before segments | After segments | Vias | Observed mutation |
|---|---:|---:|---:|---|
| Optimizer | 6,129 | 679 | 34 | Segment geometry changes |
| DRC nudge | 679 | 679 | 34 | Identical geometry hash |
| Consolidation | 679 | 303 | 34 | Collinear segments consolidated |
| Finalize gate | 303 | 303 | 34 | Identical geometry hash |
| Group tuning | 303 | 863 | 34 | Tuning adds segment geometry |
| Cleanup/serialization | 863 | 863 | 34 | Identical geometry hash |

Exact PCBs, geometry multisets, per-route metadata and hashes are retained
for each before/after boundary, including post-invariant quality captures.
The prior initial-search capture still supplies the search-entry/exit
witness with all 120 fixed escape routes; no transient search-history
reconstruction is claimed.
## Separate file finishing

`capture_finishing.py` imports the unchanged a6 recipe. It suppresses exactly
one `kct route` subprocess and loads `native-fill-after.kicad_pcb`; an assertion
checks that the recipe's routing-frame input exactly matches the prefix input.
The substituted status 2 is explicitly informational, not a newly measured
route exit. All subsequent recipe functions and budgets remain original.
This stage has its own 600-second cap and cannot start another full route.

The 64 synchronous snapshots bracket authoritative zone replacement, each
fill subprocess, stitching, relocation, repair, local detour and quantization.
Relocation returns zero moves with identical before/after PCB bytes; therefore
its native staged candidate path is not entered. Hooks for that path are
included, but no fabricated relocation candidate is claimed. Local detour
validation subprocesses and results are retained; the snapshots represent
published producer boundaries, not every transient in-memory trial.

The final replay PCB is
`19b348b8d8aba0ed1749200926eb664c3ab019c7519c8940579e6e04e6dc221a`.
Its 1,283 copper items and pad geometry match the retained full-control PCB
`9f8c6d12847e29997445590153c68e5b68eca639226c1c503ccb8bf04ca65588`.
The native copper multiset hash is
`370a06e1e43b310c97abbdf6e012fac933ec8a190778f401eebb49a5f272c98f`.
This comparison excludes UUIDs, filled-zone polygons and whole-board native
connectivity; those are checked separately below.

The unchanged recipe `kct check` reports **16 errors across 63 checked rules**,
with zero warnings counted by that check: 1 DQ3 connectivity error,
6 pair-skew errors, 7 continuity errors and 2 group-skew errors. Its complete
output is retained. All seven pair continuities are 0 against the unchanged
0.85 requirement. Pair skews are DQS 5.301 mm, MIPI clock 3.736 mm,
MIPI data0 5.812 mm and data1 6.889 mm against 0.050 mm; TMDS data0
1.457 mm and data2 1.091 mm against 0.075 mm. Group skews are HDMI
3.829 mm against 0.075 mm and MIPI 3.817 mm against 0.050 mm.
These are the check's displayed measurements, not loosened allowances.
ERC and schematic LVS are explicitly skipped in this file-only replay because
no schematic was supplied; the prior full-control LVS evidence remains in the
original archive. Capture process exit 0 and successful file finishing do not
mean DRC, LVS, production routing, or the parent acceptance passed.

## Independent native stage results

The audit completed with exit 0: 31 distinct PCB/project/DRU byte sets,
each checked saved and independently refilled, cover all 84 snapshots.
Byte-identical repeated states explicitly reference their measured state.
Every check binds all 244 pads. **DQ3 remains open in every saved and
refilled snapshot.** Native process exit 0 means the tool completed; findings
are read from its retained JSON and physical pad components.

| Stage state | Saved open nets | Independently refilled open nets |
|---|---|---|
| Pre-optimizer through cleanup | DQ3, GND, +1V2, +1V8 | DQ3, GND, +1V2 |
| After stitching | DQ3, GND, +1V2, +1V8 | DQ3, GND |
| Repair round 1, before refill | DQ3, GND, +1V8 | DQ3, GND |
| Repair round 2, before refill | DQ3, +1V8 | DQ3 |
| After accepted local detour through final | DQ3 | DQ3 |

Native checks of the saved, pre-refill repair states report 99 clearance /
69 hole-clearance findings in round 1, and 66 / 47 in round 2. Their
independently refilled copies report zero of those findings. These transient
states and complete raw reports are retained rather than hidden behind final
counters. No native short finding appears in these stage reports.

Final saved and refilled physical pad partitions are equal. Both retain
11 `via_dangling`, 6 `lib_footprint_issues`, 4 `text_thickness`,
4 `text_height` and 5 `silk_over_copper` violations, in addition to DQ3's
physical open. The final independently refilled PCB is
`5d46784dea466ce1f9db872b06b90105b95ce0a00ca0bad424e14b9a501460d6`.
Earlier saved/refilled power partitions differ, as the table shows; final
partition equality is not generalized to every stage or the older controls.

These captures establish that optimizer, nudge, consolidation, group tuning,
stitching, relocation and fills neither create nor repair the original DQ3
open in this measured sequence. Its first occurrence remains the initial
search failure demonstrated by the retained prefix controls.

## Current-source mechanism and one recommendation

The original report's grid-versus-search-cost uncertainty is now narrowed by
retained real-geometry predicates. These are a separate source variant:
`58ee27d6f7b6c654221d7e49dee49bc301958f54`, the #5406 budget-precedence
correction, and the two #5409 authored-gap modules. They are not a6 and are
not unmodified current main. `mechanism/budget-source` and
`mechanism/gap-source` retain exact source deltas and hash comparisons.
The changed native router is
`b0d4776037c4441b81a1fa81b8fe5342fe6e4d2f26168ebc4567d72478cced7b`,
distinct from the a6 router above. All four original archive hashes are in
`mechanism/archive-origins.json`.

With corrected pair sizing, the initial DQ3 search still returns no route at
1,000,000 expansions. Removing DQS_N permits a DQ3 path. An append-only
witness restores the original foreign copper and adds that path; native saved
and refilled checks connect both DQ3 and DQS_N with 244 pads and no native
short/clearance/hole finding. Other incomplete nets and findings remain.

The live C++ geometric validator accepts that actual 0.15 mm-wide witness,
with minimum clearance 0.2131944895 mm. Its via at (143.142,123.292) is
0.813195 mm from the DQS_N via at (143.7769928,123.8000031): copper gap
0.213195 mm exceeds 0.2 mm and drill gap 0.513195 mm exceeds 0.5 mm.
Yet trace, diagonal, via and direct center-cell predicates reject dynamic
routed-via halo cells. Recorded cells are not static obstacles, pads,
reservations or fill. The conservative marker covers a six-cell square at
0.127 mm resolution and includes this physically legal witness location.

**One Phase2 recommendation: #5410's exact-geometry refinement of dynamic
routed-copper halo rejection**, consistently across applicable search gates.
Preserve static obstacles, pad metal, reservations, object-pair clearance,
electrical rules and budgets. Do not erase foreign copper or soften global
requirements. This establishes a rejecting mechanism and legal coexistence
control; it does not claim a complete A* state replay or that this is the only
remaining defect. #5410's production validation remains independent work.

## Remaining parent work and historical reconciliation

The earlier exact-source full recipe measured 30/31; independent DDR-only
measured 10/11 with DQ3 open in both native saved and refilled components.
Those raw results and failed ordering/removal controls remain in #5397's
original archive. They fulfill diagnostic measurements, not the parent's
31/31 plus 11/11 qualification requirement.

Historical #3434 used revisions 82d1bc7d and 3dc88588, a 600-second total
budget, skipped DRC and a contended host. It is not an equivalent control
for these source/runtime/budget conditions, so its 26/31 result cannot support
a causal comparison. No additional historical or DDR rerun was needed.

#3438 remains open for complete production and DDR connectivity and all
physical/quality gates. This diagnostic report does not promote #5328,
restore Board07 CI, or weaken any accepted threshold.

## Reproduction and verification

Combined archive SHA256: `4a263f575d78faeb37063c5b1d0e5ad8990e06b85ee378e4f9523476465dd49a`
(896 files, 21,671,616 compressed bytes).

Run `python verify_evidence.py` here and in the parent directory to validate
both independent archives without executing their contents. The archive
includes exact commands, source manifest, native identities, input context,
raw logs, stage snapshots and audit reports. The named worker1 runtime remains
available by the recorded image hash; reconstructing another runtime must be
reported as a new variant rather than assumed identical.

`run_prefix.py` records the 2,400-second prefix command and PIDs;
`run_finishing.sh` runs only the retained-file finishing replay;
`run_audits.sh` bounds independent saved/refilled native checks. Paths are
container-mounted `/capture` and read-only `/baseline`, as recorded in the
container receipt. Run against fresh output directories. Original archives
are preserved independently; none is relabeled as recovered historical bytes.
