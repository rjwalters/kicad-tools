# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-16*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9312**: ci: drop single-value matrices so regression check-runs keep their job names

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors)
- **#9685**: route --complete --voltage-map: exits 0 with committed copper that fails kct creepage on the same map (some links are rolled back, others are not)

## Important

`loom:important`; taken before unlabelled work.

- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#9659**: creepage export-rules: magnitude-only domains drop opposite-polarity and sign-flipping pairs (no rule emitted)
- **#9660**: optimize-traces pad-escape guard resolves the layer from old[0] only, so a multi-layer Kelvin branch is partly unguarded

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#9031**: tests: nested pytest.approx() at test_expand_route_experiment.py:166 blocks any pytest >= 9.2 bump
- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper
- **#9582**: voltage map: no way to give several scenarios to route/optimize-placement; _pair_bounds.max_dv rejects 0
- **#9600**: Audit kct route rows 3/7/8/10: necks, clearance tolerance, all-links, partial-progress summary
- **#9632**: Kept-copper audit: (severity ignore) rule leaves the unknown band unreachable (AC3 residue of #9578)
- **#9660**: optimize-traces pad-escape guard resolves the layer from old[0] only, so a multi-layer Kelvin branch is partly unguarded
- **#9685**: route --complete --voltage-map: exits 0 with committed copper that fails kct creepage on the same map (some links are rolled back, others are not)

## In Progress

Issues currently being built (`loom:building`).

- **#8979**: CI red on main: Routed PCB DRC Check fails on boards 01/02 physical_copper_gap (2 + 14 blocking errors)
- **#9000**: Router: barrier-closure diagnostic (predict severed nets when a routing loop closes)
- **#9019**: Grid router (Python + C++): keep own-net pad-vs-trace legs and neck-down taper out of the slit band (#9011, board 06)
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI
- **#9138**: Match-group skew producer measures summed PCB copper: dead-end loops and duplicates mask real skew
- **#9306**: Merge-queue rollout: two required contexts are matrix-expanded names the audit cannot match
- **#9332**: Repair path for same-net fab-parity findings (physical_copper_gap, mask_to_copper own-pad)
- **#9436**: mask_to_copper: a pad solid-connected to its own-net pour is an error, but the fab DFM reports nothing (13 errors vs 0 danger / 0 warning)
- **#9515**: Silk text width model under-covers a justified label's far end (_TEXT_CHAR_WIDTH_FACTOR)
- **#9530**: In-memory mirror (recovery applicator) does not mirror custom-pad primitives; KiCad's flip does
- **#9536**: board04: regenerate output from the #9455 recipe, fix pinned-geometry tests, re-pin readiness
- **#9581**: route --complete: budget shorter than --timeout, every open link flagged deadline_hit, no output until exit
- **#9593**: Handle a board outline drawn inside a footprint (footprint-level Edge.Cuts) everywhere the outline is used
- **#9594**: Report layout-quality metrics: track vs ground vias, corner and turn-back counts, pair skew
- **#9596**: Add PCBJam to the ecosystem registry with its licence split, and a research note on its blind PCB study
- **#9597**: Router: cost and report for routes crossing another net's zone on opted-in plane layers
- **#9608**: route_understated_pairs misses _pair_bounds on slash-prefixed net names (false understatement reports)
- **#9618**: kct probe/apply: no test applies an arc item; items-file field allowlist is not per-type
- **#9619**: Align the clearance comparison slack with KiCad's (probe: ~500nm, kct uses 100nm)
- **#9620**: Router: automatic coarser-grid retry (kct route) and a non-fixed grid for route-auto
- **#9622**: Router: cost for splitting free area / keep non-local nets out from under fine-pitch ICs by default
- **#9628**: kct route: advisory run lock to stop a concurrent second writer from clobbering copper
- **#9636**: route --complete: pad-escape refusals on a net that already has copper (fine-pitch pin re-escape; own part's other pad counted as a wall)
- **#9640**: pcb prune and optimize-traces still straighten deliberate near-pad geometry into creepage failures (prune has no voltage map; optimize gate active but passes them)
- **#9642**: kct check silk_pad_clearance: board-level gr_line / gr_arc are compared in the wrong frame on origin-offset boards (false errors, and real overlaps missed)
- **#9644**: route-link uses uncapped spacing when a device bound names an implicit 0 V net
- **#9647**: path_ampacity / current-paths-audit: a thin branch that leaves the sink pad is counted as part of the resolved path (false 'would fuse')
- **#9649**: pcb reinforce: mask opening is refused for a whole leg when another net crosses it on the opposite layer (layer-agnostic obstacle test, no partial opening)
- **#9654**: path_ampacity: the unwired hole-to-pad stub inside a credited segment is credited too (#9648 follow-up)
- **#9659**: creepage export-rules: magnitude-only domains drop opposite-polarity and sign-flipping pairs (no rule emitted)
- **#9666**: path_ampacity wire credit: no real-geometry test for an uncovered stretch
- **#9673**: Copper arcs are invisible to the pairwise clearance/short rules, and `kct apply` can now write them
- **#9674**: kct probe/apply polish: docs understate the probed categories, and apply reports items that dedupe skipped

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#9141**: feat: hold grid A* legs and the neck-down taper out of the own-net pad slit band
- **#9179**: fix(validate): report off-path copper alongside derived match-group skew
- **#9396**: feat: repair same-net physical_copper_gap slits in fix-drc (#9332)
- **#9623**: feat: report necked copper in kct route results; audit rows 3/7/8/10 regression evidence
- **#9651**: fix(current-paths): exclude a branch buried in an endpoint pad from the resolved path
- **#9653**: fix: shift board-level silk into the pads' frame in silk_pad_clearance
- **#9656**: fix: apply _pair_bounds caps in route-link's omitted-net fallback
- **#9661**: fix: align the shared clearance slack with KiCad's probed 500 nm band
- **#9671**: fix: key creepage export domains by signed interval and difference pairs end-to-end
- **#9676**: fix: reject items-file fields outside their item type in kct probe/apply
- **#9679**: test: judge the uncovered stretch through reinforce_net-derived wire legs
- **#9680**: fix: judge wired-leg hole-to-pad stubs at copper width in path_ampacity
- **#9681**: chore: point package metadata at 2AMLogic/kicad-tools
- **#9682**: fix: normalise net keys in route_understated_pairs bounds lookup
- **#9683**: fix: report apply items skipped by dedupe and document all probe check categories

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9312**: ci: drop single-value matrices so regression check-runs keep their job names

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
- **#9031**: tests: nested pytest.approx() at test_expand_route_experiment.py:166 blocks any pytest >= 9.2 bump *(curated)*
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings *(curated)*
- **#9035**: Ecosystem registry drift: upstream license/rename/archive change *(curated)*
- **#9058**: Board 01: re-pin readiness evidence after PR #8848 on a host where mask_to_copper completes *(curated)*
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI *(curated)*
- **#9068**: Board 05: clear the strict-gate flood (29 physical_copper_gap errors, 13 silk_hole_overlap, 348 provisional mask_to_copper) and re-pin readiness *(curated)*
- **#9091**: Board 07: scripted repair of 29 same-net physical_copper_gap errors on the hand-reviewed release copper (ruling on #8979) *(curated)*
- **#9093**: submit-jlcpcb: attended, exact-total-approved JLC parts purchase leg *(curated)*
- **#9138**: Match-group skew producer measures summed PCB copper: dead-end loops and duplicates mask real skew *(curated)*
- **#9235**: Slow-suite test test_armc_route_gives_usb_d_equal_vias_with_every_gate fails on main (USB_D+ 2 vias, USB_D- 3) *(curated)*
- **#9273**: Same-net trace slit band sees only committed segments: measure whether any producer routes a via before the trace that skirts it *(curated)*
- **#9298**: Repository moved to the 2AMLogic organization: update hard-coded owner slug and the PyPI trusted publisher *(curated)*
- **#9306**: Merge-queue rollout: two required contexts are matrix-expanded names the audit cannot match *(curated)*
- **#9332**: Repair path for same-net fab-parity findings (physical_copper_gap, mask_to_copper own-pad) *(curated)*
- **#9381**: Committed demo-board BOMs still carry the pre-#9323 duplicate-LCSC lines *(curated)*
- **#9406**: Converge boards 06/07 DRC re-check into the single-sourced job scripts (latent local-gate --full failure) *(curated)*
- **#9424**: Board 05 unattended gate: repair route, then drop the last --skip mask_to_copper *(curated)*
- **#9436**: mask_to_copper: a pad solid-connected to its own-net pour is an error, but the fab DFM reports nothing (13 errors vs 0 danger / 0 warning) *(curated)*
- **#9454**: No migration path for evidence-bound waivers across the ev3 -> ev4 bump: every consumer waiver goes stale *(curated)*
- **#9464**: Boards 03, 07 and 09 have parts off the 0.1 mm grid that pad_grid now reports (surfaced by #9434) *(curated)*
- **#9467**: Board 02: clear mask_to_copper + silk_hole_overlap through the recipe, then re-issue the release (unblocks #9053) *(curated)*
- **#9515**: Silk text width model under-covers a justified label's far end (_TEXT_CHAR_WIDTH_FACTOR) *(curated)*
- **#9530**: In-memory mirror (recovery applicator) does not mirror custom-pad primitives; KiCad's flip does *(curated)*
- **#9536**: board04: regenerate output from the #9455 recipe, fix pinned-geometry tests, re-pin readiness *(curated)*
- **#9581**: route --complete: budget shorter than --timeout, every open link flagged deadline_hit, no output until exit *(curated)*
- **#9582**: voltage map: no way to give several scenarios to route/optimize-placement; _pair_bounds.max_dv rejects 0 *(curated)*
- **#9593**: Handle a board outline drawn inside a footprint (footprint-level Edge.Cuts) everywhere the outline is used *(curated)*
- **#9594**: Report layout-quality metrics: track vs ground vias, corner and turn-back counts, pair skew *(curated)*
- **#9596**: Add PCBJam to the ecosystem registry with its licence split, and a research note on its blind PCB study *(curated)*
- **#9597**: Router: cost and report for routes crossing another net's zone on opted-in plane layers *(curated)*
- **#9600**: Audit kct route rows 3/7/8/10: necks, clearance tolerance, all-links, partial-progress summary *(curated)*
- **#9608**: route_understated_pairs misses _pair_bounds on slash-prefixed net names (false understatement reports) *(curated)*
- **#9618**: kct probe/apply: no test applies an arc item; items-file field allowlist is not per-type *(curated)*
- **#9619**: Align the clearance comparison slack with KiCad's (probe: ~500nm, kct uses 100nm) *(curated)*
- **#9620**: Router: automatic coarser-grid retry (kct route) and a non-fixed grid for route-auto *(curated)*
- **#9622**: Router: cost for splitting free area / keep non-local nets out from under fine-pitch ICs by default *(curated)*
- **#9627**: kct route: lock existing copper by default on boards that already carry routing copper *(curated)*
- **#9628**: kct route: advisory run lock to stop a concurrent second writer from clobbering copper *(curated)*
- **#9632**: Kept-copper audit: (severity ignore) rule leaves the unknown band unreachable (AC3 residue of #9578) *(curated)*
- **#9636**: route --complete: pad-escape refusals on a net that already has copper (fine-pitch pin re-escape; own part's other pad counted as a wall) *(curated)*
- **#9640**: pcb prune and optimize-traces still straighten deliberate near-pad geometry into creepage failures (prune has no voltage map; optimize gate active but passes them) *(curated)*
- **#9642**: kct check silk_pad_clearance: board-level gr_line / gr_arc are compared in the wrong frame on origin-offset boards (false errors, and real overlaps missed) *(curated)*
- **#9644**: route-link uses uncapped spacing when a device bound names an implicit 0 V net *(curated)*
- **#9647**: path_ampacity / current-paths-audit: a thin branch that leaves the sink pad is counted as part of the resolved path (false 'would fuse') *(curated)*
- **#9649**: pcb reinforce: mask opening is refused for a whole leg when another net crosses it on the opposite layer (layer-agnostic obstacle test, no partial opening) *(curated)*
- **#9654**: path_ampacity: the unwired hole-to-pad stub inside a credited segment is credited too (#9648 follow-up) *(curated)*
- **#9659**: creepage export-rules: magnitude-only domains drop opposite-polarity and sign-flipping pairs (no rule emitted) *(curated)*
- **#9660**: optimize-traces pad-escape guard resolves the layer from old[0] only, so a multi-layer Kelvin branch is partly unguarded *(curated)*
- **#9666**: path_ampacity wire credit: no real-geometry test for an uncovered stretch *(curated)*
- **#9673**: Copper arcs are invisible to the pairwise clearance/short rules, and `kct apply` can now write them *(curated)*
- **#9674**: kct probe/apply polish: docs understate the probed categories, and apply reports items that dedupe skipped *(curated)*
- **#9685**: route --complete --voltage-map: exits 0 with committed copper that fails kct creepage on the same map (some links are rolled back, others are not) *(curated)*
- **#9690**: Filled Edge.Cuts shapes (no fill token or fill yes) are cut-outs natively but kct checks only their stroke *(curated)*

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
| Operator merge-risk holds | 2 |
| Very important | 2 |
| Important | 3 |
| Ready (`loom:issue`) | 7 |
| In Progress (`loom:building`) | 34 |
| PRs awaiting review | 16 |
| Approved PRs awaiting merge | 2 |
| Curated | 70 |
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
