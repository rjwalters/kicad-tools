# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-16*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9123**: ci: anchor native-diagnostics opt-in to its own line and state the audit tolerance

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors) *(held)*

## Important

`loom:important`; taken before unlabelled work.

- **#8858**: Verify default-on mask_to_copper on native KiCad 10.0.5 and baseline 543/670 board findings (PR #8849) *(held)*

## Ready

Human-approved issues (`loom:issue`) ready for implementation — excluding held (`loom:blocked`/`loom:operator`/`loom:frozen`), `loom:building`, and issues closed by an open `loom:pr` PR (listed under Approved).

- **#9031**: tests: nested pytest.approx() at test_expand_route_experiment.py:166 blocks any pytest >= 9.2 bump
- **#9140**: Coupled-pair pose/stub-tail legs: keep own-net pad legs out of the slit band (#9011, board 06 LVDS)
- **#9210**: acute_foldback: cross-net clearance veto keeps copper a later same-group repair removed
- **#9223**: Stale USB2_D- allowance rationale in test_board06_via_overlap_legalization docstring
- **#9236**: Escape router's same-net pad slit check builds pad boxes from raw width/height, ignoring rotation
- **#9242**: fab parity TABLE_VERSION is a merge-conflict hotspot: concurrent PRs keep claiming the same number
- **#9287**: test flake: test_expensive_optimizer_unwinds_and_saves_only_raw_partial[True] fails the 4 s post-arm budget on the Test job

## In Progress

Issues currently being built (`loom:building`).

- **#8979**: CI red on main: Routed PCB DRC Check fails on boards 01/02 physical_copper_gap (2 + 14 blocking errors)
- **#9000**: Router: barrier-closure diagnostic (predict severed nets when a routing loop closes)
- **#9019**: Grid router (Python + C++): keep own-net pad-vs-trace legs and neck-down taper out of the slit band (#9011, board 06)
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI
- **#9073**: Board 05: clear the 29 physical_copper_gap errors in the reviewed routing recipe
- **#9097**: Board 04 fresh regen: raw route carries 13 advisory warnings (pour_slot x10, staircase, fragments, silk_hole_overlap) - reviewed gate leg 3 red
- **#9107**: mask_to_copper: per-object distance on a jogged pad exit counts copper already inside the entry track
- **#9120**: CI: native-diagnostics opt-in is a substring match on the PR body, so a PR that merely mentions the marker inherits the native-workload audit gate
- **#9124**: Board 06 drain remainder: same-net via-vs-trace slits and the face-aligned corner-wedge seat the slit-band predicate accepts
- **#9138**: Match-group skew producer measures summed PCB copper: dead-end loops and duplicates mask real skew
- **#9182**: off_path_length_mm charges length-matching staircase copper, so OFF_PATH_TOLERANCE_MM tracks staircase density rather than defect presence
- **#9215**: Same-net trace slit-band guard reaches only board 06: kct route, the Python pathfinders and the via-then-trace order are unguarded
- **#9232**: test oracle drops round/oval Gerber flashes plotted as zero-length lines (native mask comparisons can pass vacuously)
- **#9234**: mask_to_copper stays incomplete on a footprint-embedded keepout rule area (missing/ambiguous native object)
- **#9235**: Slow-suite test test_armc_route_gives_usb_d_equal_vias_with_every_gate fails on main (USB_D+ 2 vias, USB_D- 3)
- **#9239**: Board 04 repair_sharp_corners: clearance check ignores board edge and keepouts, and can fall back to a narrower trace
- **#9241**: mypy reports 8 errors in optional-extra modules that the CI Type Check never sees
- **#9243**: No test runs a board's package producer, so a textually clean merge can leave it refusing
- **#9244**: kct route --report-detour-ratio: the alone-on-empty probe spends the real run's --timeout and wall-deadline budget
- **#9245**: ci flake: 'Detect Changes' paths-filter fails on GitHub 'diff temporarily unavailable' listFiles error
- **#9246**: ci flake: 'Changelog Fragment Check' cancelled by its 5-minute job timeout inside actions/checkout, reported as a fragment failure
- **#9261**: mask_to_copper: an on-pad track whose end cap pokes past the pad edge is an error while a full exit is info
- **#9267**: design-source/ archive can omit a live board recipe entirely (measured: board 03 silk_repair.py)
- **#9269**: mask_to_copper: print beside_owner_excess_mm2 in the finding message
- **#9272**: Pure-Python router engines never apply the same-net trace slit-band guard (pad twin is implemented; the C++/Python backends disagree)
- **#9273**: Same-net trace slit band sees only committed segments: measure whether any producer routes a via before the trace that skirts it
- **#9274**: deps: declare pillow as a direct dependency (kct imports PIL but pillow is only transitive)
- **#9276**: Host-path scan does not cover docs/ outside docs/diagnostics/
- **#9284**: mask_to_copper: stop reporting a spoke-connected own pour beside its own pad as an error (fab DFM does not list it)

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#9209**: fix(router): reject tangent via seats in the same-net pad slit-band guard
- **#9250**: fix: keep keepout rule areas from blocking mask-to-copper attribution
- **#9256**: ci: tolerate transient paths-filter failures in Detect Changes
- **#9259**: ci: raise changelog fragment-check timeout from 5 to 15 minutes
- **#9262**: test: replay the package-release drop gate over committed packages
- **#9263**: fix: make mypy baseline gate pass identically with and without optional extras
- **#9264**: fix(mask_copper): grant entry allowance to any segment with an end inside its pad
- **#9266**: test: pin per-object mask_to_copper distance for jogged pad exits
- **#9270**: fix: exclude the detour probe's wall-clock cost from routing budgets
- **#9275**: fix: resolve the same-net trace slit-band edge on the kct route path
- **#9277**: fix(board01): slide same-net vias out of pad mask apertures in the recipe
- **#9278**: chore(deps): declare pillow>=12 in screenshot extra and dev group
- **#9279**: fix: enforce same-net trace slit band in Python escape and lattice via gates
- **#9280**: fix(boards): archive board 03 silk_repair.py and assert design-source completeness
- **#9282**: test: scan docs/** for absolute host paths, allowlisting docs/mcp placeholders
- **#9283**: feat(mask_to_copper): print beside owner excess in the finding message

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9123**: ci: anchor native-diagnostics opt-in to its own line and state the audit tolerance — closes #9120
- **#9194**: docs: record measured off-path decomposition at board-07's OFF_PATH_TOLERANCE_MM — closes #9182
- **#9252**: test: keep zero-length round-pen flashes as disks in the native Gerber oracle — closes #9232
- **#9253**: fix(board04): check board-edge and keepout clearance in repair_sharp_corners; drop the narrower-width fallback — closes #9239

## Proposed

Issues carrying `loom:curated`.

- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)* *(held)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)* *(held)*
- **#7856**: [Epic #6441] Phase 5 measurement: coupled-trunk retention >= 95% on boards 03, 06b, 06rf, 07 *(curated)*
- **#8214**: optimize-traces: collision-check each rewrite on finished boards instead of whole-net rollback *(curated)* *(held)*
- **#8425**: Live-verify covered-track split (#8406) on pinned kicad-cli and #8368 board B *(curated)* *(held)*
- **#8434**: Changelog Fragment Check re-runs on every label change; Loom role label churn keeps it queued *(curated)* *(held)*
- **#8486**: route perf: nets proven sealed are re-searched at full budget and re-rescued every iteration (43% of search time, rescue 0/15 for ~1,475 s each pass) *(curated)* *(held)*
- **#8530**: Fiducials + board-identification silkscreen should be automatic: add-fiducials / add-board-info commands, a check rule, and a default tapeout step *(curated)* *(held)*
- **#8603**: Board 07: classify and eliminate remaining redundant_copper warnings at source (part of #8399) *(curated)* *(held)*
- **#8610**: route --complete chain pass commits ~282 shorts against Net 0 copper on dense mains board (rc 4) *(curated)* *(held)*
- **#8620**: Find the real cause of the 10-12x pure-Python routing slowdown with all extras on macOS *(curated)* *(held)*
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs *(curated)* *(held)*
- **#8858**: Verify default-on mask_to_copper on native KiCad 10.0.5 and baseline 543/670 board findings (PR #8849) *(curated)* *(held)*
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
- **#9073**: Board 05: clear the 29 physical_copper_gap errors in the reviewed routing recipe *(curated)*
- **#9093**: submit-jlcpcb: attended, exact-total-approved JLC parts purchase leg *(curated)* *(held)*
- **#9097**: Board 04 fresh regen: raw route carries 13 advisory warnings (pour_slot x10, staircase, fragments, silk_hole_overlap) - reviewed gate leg 3 red *(curated)*
- **#9107**: mask_to_copper: per-object distance on a jogged pad exit counts copper already inside the entry track *(curated)*
- **#9120**: CI: native-diagnostics opt-in is a substring match on the PR body, so a PR that merely mentions the marker inherits the native-workload audit gate *(curated)*
- **#9124**: Board 06 drain remainder: same-net via-vs-trace slits and the face-aligned corner-wedge seat the slit-band predicate accepts *(curated)*
- **#9138**: Match-group skew producer measures summed PCB copper: dead-end loops and duplicates mask real skew *(curated)*
- **#9140**: Coupled-pair pose/stub-tail legs: keep own-net pad legs out of the slit band (#9011, board 06 LVDS) *(curated)*
- **#9182**: off_path_length_mm charges length-matching staircase copper, so OFF_PATH_TOLERANCE_MM tracks staircase density rather than defect presence *(curated)*
- **#9210**: acute_foldback: cross-net clearance veto keeps copper a later same-group repair removed *(curated)*
- **#9215**: Same-net trace slit-band guard reaches only board 06: kct route, the Python pathfinders and the via-then-trace order are unguarded *(curated)*
- **#9223**: Stale USB2_D- allowance rationale in test_board06_via_overlap_legalization docstring *(curated)*
- **#9232**: test oracle drops round/oval Gerber flashes plotted as zero-length lines (native mask comparisons can pass vacuously) *(curated)*
- **#9234**: mask_to_copper stays incomplete on a footprint-embedded keepout rule area (missing/ambiguous native object) *(curated)*
- **#9235**: Slow-suite test test_armc_route_gives_usb_d_equal_vias_with_every_gate fails on main (USB_D+ 2 vias, USB_D- 3) *(curated)*
- **#9236**: Escape router's same-net pad slit check builds pad boxes from raw width/height, ignoring rotation *(curated)*
- **#9239**: Board 04 repair_sharp_corners: clearance check ignores board edge and keepouts, and can fall back to a narrower trace *(curated)*
- **#9241**: mypy reports 8 errors in optional-extra modules that the CI Type Check never sees *(curated)*
- **#9242**: fab parity TABLE_VERSION is a merge-conflict hotspot: concurrent PRs keep claiming the same number *(curated)*
- **#9243**: No test runs a board's package producer, so a textually clean merge can leave it refusing *(curated)*
- **#9244**: kct route --report-detour-ratio: the alone-on-empty probe spends the real run's --timeout and wall-deadline budget *(curated)*
- **#9245**: ci flake: 'Detect Changes' paths-filter fails on GitHub 'diff temporarily unavailable' listFiles error *(curated)*
- **#9246**: ci flake: 'Changelog Fragment Check' cancelled by its 5-minute job timeout inside actions/checkout, reported as a fragment failure *(curated)*
- **#9261**: mask_to_copper: an on-pad track whose end cap pokes past the pad edge is an error while a full exit is info *(curated)*
- **#9267**: design-source/ archive can omit a live board recipe entirely (measured: board 03 silk_repair.py) *(curated)*
- **#9269**: mask_to_copper: print beside_owner_excess_mm2 in the finding message *(curated)*
- **#9272**: Pure-Python router engines never apply the same-net trace slit-band guard (pad twin is implemented; the C++/Python backends disagree) *(curated)*
- **#9273**: Same-net trace slit band sees only committed segments: measure whether any producer routes a via before the trace that skirts it *(curated)*
- **#9274**: deps: declare pillow as a direct dependency (kct imports PIL but pillow is only transitive) *(curated)*
- **#9276**: Host-path scan does not cover docs/ outside docs/diagnostics/ *(curated)*
- **#9287**: test flake: test_expensive_optimizer_unwinds_and_saves_only_raw_partial[True] fails the 4 s post-arm budget on the Test job *(curated)*

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
| Operator merge-risk holds | 2 |
| Very important | 1 |
| Important | 1 |
| Ready (`loom:issue`, net of holds/building/merge-pending) | 7 |
| In Progress (`loom:building`) | 30 |
| PRs awaiting review | 17 |
| Approved PRs awaiting merge | 5 |
| Curated | 56 |
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
