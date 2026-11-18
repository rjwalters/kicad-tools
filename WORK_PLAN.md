# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-16*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9303**: ci: qualify PR checks for GitHub merge queues
- **#9312**: ci: drop single-value matrices so regression check-runs keep their job names

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors) *(held)*

## Important

`loom:important`; taken before unlabelled work.

- **#8858**: Verify default-on mask_to_copper on native KiCad 10.0.5 and baseline 543/670 board findings (PR #8849)

## Ready

Human-approved issues (`loom:issue`) ready for implementation — excluding held (`loom:blocked`/`loom:operator`/`loom:frozen`), `loom:building`, and issues closed by an open `loom:pr` PR (listed under Approved).

- **#9031**: tests: nested pytest.approx() at test_expand_route_experiment.py:166 blocks any pytest >= 9.2 bump
- **#9305**: mask_to_copper: decide whether an area tolerance belongs on the 'beyond owner pad copper' test (112 of 144 consumer errors are sub-1e-6 mm² residue)
- **#9368**: load_bom_csv drops the LCSC column from a real bom_jlcpcb.csv (header is `LCSC Part #`)

## In Progress

Issues currently being built (`loom:building`).

- **#8858**: Verify default-on mask_to_copper on native KiCad 10.0.5 and baseline 543/670 board findings (PR #8849)
- **#8979**: CI red on main: Routed PCB DRC Check fails on boards 01/02 physical_copper_gap (2 + 14 blocking errors)
- **#9000**: Router: barrier-closure diagnostic (predict severed nets when a routing loop closes)
- **#9019**: Grid router (Python + C++): keep own-net pad-vs-trace legs and neck-down taper out of the slit band (#9011, board 06)
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI
- **#9097**: Board 04 fresh regen: raw route carries 13 advisory warnings (pour_slot x10, staircase, fragments, silk_hole_overlap) - reviewed gate leg 3 red
- **#9138**: Match-group skew producer measures summed PCB copper: dead-end loops and duplicates mask real skew
- **#9272**: Pure-Python router engines never apply the same-net trace slit-band guard (pad twin is implemented; the C++/Python backends disagree)
- **#9298**: Repository moved to the 2AMLogic organization: update hard-coded owner slug and the PyPI trusted publisher
- **#9299**: ci: qualify PR checks for GitHub merge queues
- **#9306**: Merge-queue rollout: two required contexts are matrix-expanded names the audit cannot match
- **#9310**: mask_copper tests: two own-pad cases pass on pre-#9261 code (one asserts a removed arm that was already dead)
- **#9315**: mask_to_copper fails boards on the wire-strip and anchor openings that pcb reinforce creates on purpose (281 of 438 errors on a consumer board)
- **#9323**: JLCPCB BOM: lines sharing one LCSC part come up unticked at the fab and are silently not fitted
- **#9325**: tht_smd_spacing pairs two same-net pcb reinforce anchors (7 of 11 errors on a consumer board; fab DFM counts none)
- **#9326**: No repair path for the fab-parity rules, and fix-drc/prune side effects on waivers (consumer board: 538 check errors, fix-drc finds nothing repairable)
- **#9331**: Waiver keys silently orphaned when prune/fix-drc move or merge copper
- **#9332**: Repair path for same-net fab-parity findings (physical_copper_gap, mask_to_copper own-pad)
- **#9340**: Local CI gate: convert the jobs that can be a script call, record the rest, retire the job-manifest drift test
- **#9346**: kct readiness --verify: the CPL rotation re-derivation fails open on any unexpected error
- **#9351**: Board recipe loader: bare `import generate_pcb` lets one board's siblings shadow another's in-process
- **#9355**: acute_foldback: the plan's base collision model still contains legs its own earlier repairs removed
- **#9356**: Baseline comment: board 04's red-since date in the #9140 AC4 paragraph is off by a day
- **#9362**: CPL rotation review: widen the unreviewed-placement list (SW/Y missed) and close three reporting/verify edges
- **#9363**: load_bom_csv treats a grouped BOM row as one component (kct estimate/suggest undercount)
- **#9364**: place-silk-refs: RefPlacement reasons quote sheet coordinates next to board-relative positions
- **#9365**: fix-drc mask-copper: clearance gate uses the manufacturer floor, not the effective net-class clearance

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#9179**: fix(validate): report off-path copper alongside derived match-group skew
- **#9279**: fix: enforce same-net trace slit band in Python escape and lattice via gates
- **#9328**: test: pin the removed mask-copper u-band arm with discriminating geometry
- **#9330**: fix: excuse a reinforce run's own copper in mask_to_copper via the sidecar
- **#9339**: fix: skip waived findings in fix-drc --drc-report repair targets
- **#9341**: fix: merge JLCPCB BOM lines sharing one LCSC part into a single row
- **#9344**: fix: exempt same-net anchor-anchor via pairs from tht_smd_spacing annulus pairing
- **#9353**: feat: report waiver keys invalidated by pcb prune and fix-drc edits
- **#9354**: fix: stop board recipe sibling imports shadowing across boards in one process
- **#9358**: Baseline board 06 mask_to_copper by net signature; drop the diffpair-gate skip
- **#9359**: fix(router): stop vetoing fold-back repairs against legs earlier repairs removed
- **#9360**: docs: split red-since date per board in diffpair coverage baseline comment
- **#9366**: fix: expand grouped BOM designator cells into one component each
- **#9367**: fix: name the coordinate frame of place-silk-refs placement output
- **#9369**: fix: revert mask-copper moves that create new DRC errors; restore snapshot on failed undo
- **#9370**: fix: widen CPL rotation review list and close readiness reporting edges
- **#9372**: fix: make readiness CPL rotation re-derivation fail visibly, not open

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9303**: ci: qualify PR checks for GitHub merge queues — closes #9299
- **#9312**: ci: drop single-value matrices so regression check-runs keep their job names — closes #9306

## Proposed

Issues carrying `loom:curated`.

- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)* *(held)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)* *(held)*
- **#7856**: [Epic #6441] Phase 5 measurement: coupled-trunk retention >= 95% on boards 03, 06b, 06rf, 07 *(curated)* *(held)*
- **#8214**: optimize-traces: collision-check each rewrite on finished boards instead of whole-net rollback *(curated)* *(held)*
- **#8425**: Live-verify covered-track split (#8406) on pinned kicad-cli and #8368 board B *(curated)* *(held)*
- **#8486**: route perf: nets proven sealed are re-searched at full budget and re-rescued every iteration (43% of search time, rescue 0/15 for ~1,475 s each pass) *(curated)* *(held)*
- **#8530**: Fiducials + board-identification silkscreen should be automatic: add-fiducials / add-board-info commands, a check rule, and a default tapeout step *(curated)* *(held)*
- **#8603**: Board 07: classify and eliminate remaining redundant_copper warnings at source (part of #8399) *(curated)* *(held)*
- **#8610**: route --complete chain pass commits ~282 shorts against Net 0 copper on dense mains board (rc 4) *(curated)* *(held)*
- **#8620**: Find the real cause of the 10-12x pure-Python routing slowdown with all extras on macOS *(curated)* *(held)*
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs *(curated)* *(held)*
- **#8858**: Verify default-on mask_to_copper on native KiCad 10.0.5 and baseline 543/670 board findings (PR #8849) *(curated)*
- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors) *(curated)* *(held)*
- **#8943**: Record JLCDFM silkscreen line-width selection criterion and per-item rows (residue of #8844) *(curated)* *(held)*
- **#8948**: kct's own managed .kicad_dru makes mask_to_copper incomplete: admit physical_hole_clearance, text_thickness and text_height to the native context allowlist *(curated)* *(held)*
- **#8962**: Same-net slit population on fresh regens: repair-pass + route-side producers (stale fixture attribution from #8957) *(curated)* *(held)*
- **#8979**: CI red on main: Routed PCB DRC Check fails on boards 01/02 physical_copper_gap (2 + 14 blocking errors) *(curated)*
- **#9000**: Router: barrier-closure diagnostic (predict severed nets when a routing loop closes) *(curated)*
- **#9019**: Grid router (Python + C++): keep own-net pad-vs-trace legs and neck-down taper out of the slit band (#9011, board 06) *(curated)*
- **#9031**: tests: nested pytest.approx() at test_expand_route_experiment.py:166 blocks any pytest >= 9.2 bump *(curated)*
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings *(curated)*
- **#9035**: Ecosystem registry drift: upstream license/rename/archive change *(curated)*
- **#9058**: Board 01: re-pin readiness evidence after PR #8848 on a host where mask_to_copper completes *(curated)* *(held)*
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI *(curated)*
- **#9068**: Board 05: clear the strict-gate flood (29 physical_copper_gap errors, 13 silk_hole_overlap, 348 provisional mask_to_copper) and re-pin readiness *(curated)* *(held)*
- **#9093**: submit-jlcpcb: attended, exact-total-approved JLC parts purchase leg *(curated)* *(held)*
- **#9097**: Board 04 fresh regen: raw route carries 13 advisory warnings (pour_slot x10, staircase, fragments, silk_hole_overlap) - reviewed gate leg 3 red *(curated)*
- **#9138**: Match-group skew producer measures summed PCB copper: dead-end loops and duplicates mask real skew *(curated)*
- **#9235**: Slow-suite test test_armc_route_gives_usb_d_equal_vias_with_every_gate fails on main (USB_D+ 2 vias, USB_D- 3) *(curated)* *(held)*
- **#9272**: Pure-Python router engines never apply the same-net trace slit-band guard (pad twin is implemented; the C++/Python backends disagree) *(curated)*
- **#9273**: Same-net trace slit band sees only committed segments: measure whether any producer routes a via before the trace that skirts it *(curated)* *(held)*
- **#9298**: Repository moved to the 2AMLogic organization: update hard-coded owner slug and the PyPI trusted publisher *(curated)*
- **#9305**: mask_to_copper: decide whether an area tolerance belongs on the 'beyond owner pad copper' test (112 of 144 consumer errors are sub-1e-6 mm² residue) *(curated)*
- **#9306**: Merge-queue rollout: two required contexts are matrix-expanded names the audit cannot match *(curated)*
- **#9307**: Declare merge_group in select_routed_pcbs.py and pin the merge-group CI invariants with a test *(curated)* *(held)*
- **#9310**: mask_copper tests: two own-pad cases pass on pre-#9261 code (one asserts a removed arm that was already dead) *(curated)*
- **#9315**: mask_to_copper fails boards on the wire-strip and anchor openings that pcb reinforce creates on purpose (281 of 438 errors on a consumer board) *(curated)*
- **#9323**: JLCPCB BOM: lines sharing one LCSC part come up unticked at the fab and are silently not fitted *(curated)*
- **#9325**: tht_smd_spacing pairs two same-net pcb reinforce anchors (7 of 11 errors on a consumer board; fab DFM counts none) *(curated)*
- **#9326**: No repair path for the fab-parity rules, and fix-drc/prune side effects on waivers (consumer board: 538 check errors, fix-drc finds nothing repairable) *(curated)*
- **#9331**: Waiver keys silently orphaned when prune/fix-drc move or merge copper *(curated)*
- **#9332**: Repair path for same-net fab-parity findings (physical_copper_gap, mask_to_copper own-pad) *(curated)*
- **#9340**: Local CI gate: convert the jobs that can be a script call, record the rest, retire the job-manifest drift test *(curated)*
- **#9346**: kct readiness --verify: the CPL rotation re-derivation fails open on any unexpected error *(curated)*
- **#9351**: Board recipe loader: bare `import generate_pcb` lets one board's siblings shadow another's in-process *(curated)*
- **#9355**: acute_foldback: the plan's base collision model still contains legs its own earlier repairs removed *(curated)*
- **#9356**: Baseline comment: board 04's red-since date in the #9140 AC4 paragraph is off by a day *(curated)*
- **#9362**: CPL rotation review: widen the unreviewed-placement list (SW/Y missed) and close three reporting/verify edges *(curated)*
- **#9363**: load_bom_csv treats a grouped BOM row as one component (kct estimate/suggest undercount) *(curated)*
- **#9364**: place-silk-refs: RefPlacement reasons quote sheet coordinates next to board-relative positions *(curated)*
- **#9365**: fix-drc mask-copper: clearance gate uses the manufacturer floor, not the effective net-class clearance *(curated)*
- **#9368**: load_bom_csv drops the LCSC column from a real bom_jlcpcb.csv (header is `LCSC Part #`) *(curated)*
- **#9371**: Changelog check: two rapid changelog:skip flips can leave a stale verdict under the required name *(curated)*

## Proposed (Architect / Hermit)

_None._

## Epics

- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(held)*
- **#5952**: Epic: Harness-neutral kicad-tools (Claude Code, Codex CLI, opencode)
- **#8838**: Epic: fab DFM parity - a board that passes kct check --mfr jlcpcb must pass JLCPCB's DFM tool

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 3 |
| Very important | 1 |
| Important | 1 |
| Ready (`loom:issue`, net of holds/building/merge-pending) | 3 |
| In Progress (`loom:building`) | 28 |
| PRs awaiting review | 18 |
| Approved PRs awaiting merge | 3 |
| Curated | 53 |
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
