# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-20*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9312**: ci: drop single-value matrices so regression check-runs keep their job names
- **#9646**: fix: gate pcb prune and optimize-traces on per-pair creepage shortfall

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors)

## Important

`loom:important`; taken before unlabelled work.

- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#9467**: Board 02: clear mask_to_copper + silk_hole_overlap through the recipe, then re-issue the release (unblocks #9053)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#9467**: Board 02: clear mask_to_copper + silk_hole_overlap through the recipe, then re-issue the release (unblocks #9053)
- **#9658**: layout_quality corner counting is quadratic in per-net segment count
- **#9724**: Probe copper-to-edge clearance against pinned KiCad and move edge.py onto the measured band
- **#9746**: Creepage cross-face paths ignore (general (thickness)): thin boards get an optimistic bore depth
- **#9747**: readiness --generate: the cleanup's prune step ignores the board's current-paths sidecar, removes one layer of deliberately doubled power copper, and the run then fails its own path_ampacity check
- **#9752**: Padstack-nested pad primitives: flip_footprint_children_to_back and custom_pad_copper still read only the top-level block

## In Progress

Issues currently being built (`loom:building`).

- **#8979**: CI red on main: Routed PCB DRC Check fails on boards 01/02 physical_copper_gap (2 + 14 blocking errors)
- **#9000**: Router: barrier-closure diagnostic (predict severed nets when a routing loop closes)
- **#9019**: Grid router (Python + C++): keep own-net pad-vs-trace legs and neck-down taper out of the slit band (#9011, board 06)
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI
- **#9304**: Match-group skew: exclude truly-defect off-path copper without switching rulers (from #9138)
- **#9306**: Merge-queue rollout: two required contexts are matrix-expanded names the audit cannot match
- **#9436**: mask_to_copper: a pad solid-connected to its own-net pour is an error, but the fab DFM reports nothing (13 errors vs 0 danger / 0 warning)
- **#9597**: Router: cost and report for routes crossing another net's zone on opted-in plane layers
- **#9636**: route --complete: pad-escape refusals on a net that already has copper (fine-pitch pin re-escape; own part's other pad counted as a wall)
- **#9640**: pcb prune and optimize-traces still straighten deliberate near-pad geometry into creepage failures (prune has no voltage map; optimize gate active but passes them)
- **#9692**: Footprint-local filled Edge.Cuts shapes (fp_circle/fp_poly/fp_rect) are cut-outs natively but kct checks only their stroke
- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410)
- **#9708**: CI red on main: test_route_pairwise_gate_4588 strict-noop tests route 0% since the lattice own-net pad band (#9060)
- **#9716**: route-auto emits board-default track width for a wide-class net (row 3's 'limit the neck' half)
- **#9725**: current paths / pcb reinforce: a tripled layer change (three vias in a row with a short bar on both layers) makes the whole path 'ambiguous'
- **#9726**: Harden _warn_off_path_copper integration: unguarded cross-boundary call and warn-before-skip ordering
- **#9734**: router: board 05 VCP unrouted again at main - finalize safety net demotes the net after negotiation connects it
- **#9748**: optimize-traces --drc-aware --mfr jlcpcb introduces mask_to_copper errors at pad entries that the un-optimised board does not have
- **#9750**: mask_to_copper: a same-net track wider than its own pad, and any track entering a stock custom pad (SOT-89 tab), are errors with no clean layout available
- **#9755**: Consolidate the three near-identical voltage-map sidecar loaders
- **#9762**: Refresh board 04 readiness after #9614 changed project.kct

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#9645**: fix(route): start completion links from kept copper and stop sibling pads walling a pad in
- **#9669**: feat: charge, report and optionally refuse plane-zone crossings in kct route
- **#9741**: fix(validate): shed defect copper from the match-group skew ruler (Issue #9304)
- **#9753**: fix(mask): excuse wide same-net bars on their own pad and custom-pad entries
- **#9760**: refactor: share the voltage-map sidecar read and non-object check in creepage.engine

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9141**: feat: hold grid A* legs and the neck-down taper out of the own-net pad slit band
- **#9312**: ci: drop single-value matrices so regression check-runs keep their job names
- **#9646**: fix: gate pcb prune and optimize-traces on per-pair creepage shortfall
- **#9721**: fix: treat filled footprint-local Edge.Cuts shapes as cut-out regions
- **#9739**: fix(validate): report match-group off-path copper only for eligible groups

## Proposed

Issues carrying `loom:curated`.

- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#7856**: [Epic #6441] Phase 5 measurement: coupled-trunk retention >= 95% on boards 03, 06b, 06rf, 07 *(curated)*
- **#8214**: optimize-traces: collision-check each rewrite on finished boards instead of whole-net rollback *(curated)*
- **#8425**: Live-verify covered-track split (#8406) on pinned kicad-cli and #8368 board B *(curated)*
- **#8486**: route perf: nets proven sealed are re-searched at full budget and re-rescued every iteration (43% of search time, rescue 0/15 for ~1,475 s each pass) *(curated)*
- **#8530**: Fiducials + board-identification silkscreen should be automatic: add-fiducials / add-board-info commands, a check rule, and a default tapeout step *(curated)*
- **#8603**: Board 07: classify and eliminate remaining redundant_copper warnings at source (part of #8399) *(curated)*
- **#8610**: route --complete chain pass commits ~282 shorts against Net 0 copper on dense mains board (rc 4) *(curated)*
- **#8620**: Find the real cause of the 10-12x pure-Python routing slowdown with all extras on macOS *(curated)*
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs *(curated)*
- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors) *(curated)*
- **#8943**: Record JLCDFM silkscreen line-width selection criterion and per-item rows (residue of #8844) *(curated)*
- **#8962**: Same-net slit population on fresh regens: repair-pass + route-side producers (stale fixture attribution from #8957) *(curated)*
- **#8979**: CI red on main: Routed PCB DRC Check fails on boards 01/02 physical_copper_gap (2 + 14 blocking errors) *(curated)*
- **#9000**: Router: barrier-closure diagnostic (predict severed nets when a routing loop closes) *(curated)*
- **#9019**: Grid router (Python + C++): keep own-net pad-vs-trace legs and neck-down taper out of the slit band (#9011, board 06) *(curated)*
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings *(curated)*
- **#9035**: Ecosystem registry drift: upstream license/rename/archive change *(curated)*
- **#9058**: Board 01: re-pin readiness evidence after PR #8848 on a host where mask_to_copper completes *(curated)*
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI *(curated)*
- **#9068**: Board 05: clear the strict-gate flood (29 physical_copper_gap errors, 13 silk_hole_overlap, 348 provisional mask_to_copper) and re-pin readiness *(curated)*
- **#9091**: Board 07: scripted repair of 29 same-net physical_copper_gap errors on the hand-reviewed release copper (ruling on #8979) *(curated)*
- **#9093**: submit-jlcpcb: attended, exact-total-approved JLC parts purchase leg *(curated)*
- **#9235**: Slow-suite test test_armc_route_gives_usb_d_equal_vias_with_every_gate fails on main (USB_D+ 2 vias, USB_D- 3) *(curated)*
- **#9273**: Same-net trace slit band sees only committed segments: measure whether any producer routes a via before the trace that skirts it *(curated)*
- **#9298**: Repository moved to the 2AMLogic organization: update hard-coded owner slug and the PyPI trusted publisher *(curated)*
- **#9304**: Match-group skew: exclude truly-defect off-path copper without switching rulers (from #9138) *(curated)*
- **#9306**: Merge-queue rollout: two required contexts are matrix-expanded names the audit cannot match *(curated)*
- **#9381**: Committed demo-board BOMs still carry the pre-#9323 duplicate-LCSC lines *(curated)*
- **#9406**: Converge boards 06/07 DRC re-check into the single-sourced job scripts (latent local-gate --full failure) *(curated)*
- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper *(curated)*
- **#9436**: mask_to_copper: a pad solid-connected to its own-net pour is an error, but the fab DFM reports nothing (13 errors vs 0 danger / 0 warning) *(curated)*
- **#9454**: No migration path for evidence-bound waivers across the ev3 -> ev4 bump: every consumer waiver goes stale *(curated)*
- **#9464**: Boards 03, 07 and 09 have parts off the 0.1 mm grid that pad_grid now reports (surfaced by #9434) *(curated)*
- **#9467**: Board 02: clear mask_to_copper + silk_hole_overlap through the recipe, then re-issue the release (unblocks #9053) *(curated)*
- **#9597**: Router: cost and report for routes crossing another net's zone on opted-in plane layers *(curated)*
- **#9627**: kct route: lock existing copper by default on boards that already carry routing copper *(curated)*
- **#9636**: route --complete: pad-escape refusals on a net that already has copper (fine-pitch pin re-escape; own part's other pad counted as a wall) *(curated)*
- **#9640**: pcb prune and optimize-traces still straighten deliberate near-pad geometry into creepage failures (prune has no voltage map; optimize gate active but passes them) *(curated)*
- **#9658**: layout_quality corner counting is quadratic in per-net segment count *(curated)*
- **#9692**: Footprint-local filled Edge.Cuts shapes (fp_circle/fp_poly/fp_rect) are cut-outs natively but kct checks only their stroke *(curated)*
- **#9706**: CI red on main: Routed PCB DRC Check reads an incomplete mask_to_copper pass as stale identity allowances (no native toolchain in the job, #9410) *(curated)*
- **#9708**: CI red on main: test_route_pairwise_gate_4588 strict-noop tests route 0% since the lattice own-net pad band (#9060) *(curated)*
- **#9716**: route-auto emits board-default track width for a wide-class net (row 3's 'limit the neck' half) *(curated)*
- **#9724**: Probe copper-to-edge clearance against pinned KiCad and move edge.py onto the measured band *(curated)*
- **#9725**: current paths / pcb reinforce: a tripled layer change (three vias in a row with a short bar on both layers) makes the whole path 'ambiguous' *(curated)*
- **#9726**: Harden _warn_off_path_copper integration: unguarded cross-boundary call and warn-before-skip ordering *(curated)*
- **#9734**: router: board 05 VCP unrouted again at main - finalize safety net demotes the net after negotiation connects it *(curated)*
- **#9747**: readiness --generate: the cleanup's prune step ignores the board's current-paths sidecar, removes one layer of deliberately doubled power copper, and the run then fails its own path_ampacity check *(curated)*
- **#9748**: optimize-traces --drc-aware --mfr jlcpcb introduces mask_to_copper errors at pad entries that the un-optimised board does not have *(curated)*
- **#9750**: mask_to_copper: a same-net track wider than its own pad, and any track entering a stock custom pad (SOT-89 tab), are errors with no clean layout available *(curated)*
- **#9752**: Padstack-nested pad primitives: flip_footprint_children_to_back and custom_pad_copper still read only the top-level block *(curated)*
- **#9755**: Consolidate the three near-identical voltage-map sidecar loaders *(curated)*
- **#9762**: Refresh board 04 readiness after #9614 changed project.kct *(curated)*

## Proposed (Architect / Hermit)

_None._

## Epics

- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC
- **#5952**: Epic: Harness-neutral kicad-tools (Claude Code, Codex CLI, opencode)
- **#8838**: Epic: fab DFM parity - a board that passes kct check --mfr jlcpcb must pass JLCPCB's DFM tool

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 3 |
| Very important | 1 |
| Important | 2 |
| Ready (`loom:issue`) | 6 |
| In Progress (`loom:building`) | 22 |
| PRs awaiting review | 6 |
| Approved PRs awaiting merge | 6 |
| Curated | 55 |
| Architect / Hermit proposals | 0 |
| Active epics | 4 |
<!-- guide:plan-body:end -->

## Release History (static archive)

_Historical release notes below this point are a static archive, not regenerated by Guide — see WORK_LOG.md for the current chronological record of merged PRs and closed issues._

| Release | Highlights |
|---------|-----------|
| **v0.20.0** (2026-08-06) | `kct route --complete` completion pass, route-time HV-isolation enforcement in every engine, `.kct_waivers.json` waiver mechanism, `net-status` strict-by-default, KiCad-10 net-dialect / export-manifest / LVS-identity correctness sweep; shipped as a 13-PR train through a GitHub Actions outage |
| **v0.19.0** (2026-07-20) | HV-isolation design loop (`kct creepage --voltage-map`, `zones hv-keepout`, HV-aware placement, `/kct:hv-isolation-loop` skill) + via-in-pad manufacturability (`kct fix-vias` off-pad relocation), `analyze electrical-rating`, `--emit-dru`/`--emit-drc-constraints` rule-identity sidecars |
| **v0.18.0** (2026-07-20) | HV / analog manufacturing gates — `kct creepage` (surface-path creepage audit vs IEC 60664-1/62368-1) and `kct analyze current-sense` (analog layout lint). Plus real `--nets` route filter, `pcb reinforce` multi-branch anchoring, `route --layers auto` inner-layer advisory |
| **v0.17.0** (2026-07-17) | Experimental alternative routing substrate — adaptive octilinear **lattice** engine + constrained-Delaunay **navmesh/mesh** engine, both default-OFF; routes large mixed-pitch boards the grid can't fit in memory |
| **v0.16.0** (2026-07-15) | Region-bounded routing, ampacity-aware net-class min-width + DRC (IPC-2221), copper dedupe, `pcb reinforce` anchor-PTH rows, `net-status --strict` real-geometry connectivity, off-board preflight |
| **v0.15.0** (2026-07-13) | Router feasibility certificates + constructive escape ordering, coupled diff-pair corridor attractors + C++ joint-state A\* port, copper-LVS gates across boards 01–07, LCSC/EasyEDA + cross-library 3D model resolver tiers |
| **v0.14.0** (2026-06-16) | Demo gallery website (kicad-tools.org), zone-fill foreign-pad clearance fix, PCB `page_fit`, oblique 3D + 2D-SVG renders, gallery LVS status, ERC/LVS/Manifest meta sub-checks for `kct check` |

**LVS Soundness Epic — COMPLETE (2026-06-17).** Independent copper-LVS soundness epic (motivated by #3742), shipped via a `/loom:sweep` run processing 16 PRs: gate + extractor hardening (independent copper-extracted LVS gate, label-free pour extraction, per-zone pour-pad bonding, foreign-net track-segment carve, layer-aware segment chaining), fleet rollout across boards 00–07, and real defects the gate surfaced and fixed on boards 02/03/04 that DRC had missed.
