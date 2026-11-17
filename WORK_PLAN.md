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

- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors)

## Important

`loom:important`; taken before unlabelled work.

- **#8858**: Verify default-on mask_to_copper on native KiCad 10.0.5 and baseline 543/670 board findings (PR #8849)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#9031**: tests: nested pytest.approx() at test_expand_route_experiment.py:166 blocks any pytest >= 9.2 bump
- **#9140**: Coupled-pair pose/stub-tail legs: keep own-net pad legs out of the slit band (#9011, board 06 LVDS)
- **#9210**: acute_foldback: cross-net clearance veto keeps copper a later same-group repair removed

## In Progress

Issues currently being built (`loom:building`).

- **#8858**: Verify default-on mask_to_copper on native KiCad 10.0.5 and baseline 543/670 board findings (PR #8849)
- **#8979**: CI red on main: Routed PCB DRC Check fails on boards 01/02 physical_copper_gap (2 + 14 blocking errors)
- **#9000**: Router: barrier-closure diagnostic (predict severed nets when a routing loop closes)
- **#9001**: Router: regret ordering as a net-order method (commit biggest-loss nets first)
- **#9019**: Grid router (Python + C++): keep own-net pad-vs-trace legs and neck-down taper out of the slit band (#9011, board 06)
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI
- **#9073**: Board 05: clear the 29 physical_copper_gap errors in the reviewed routing recipe
- **#9097**: Board 04 fresh regen: raw route carries 13 advisory warnings (pour_slot x10, staircase, fragments, silk_hole_overlap) - reviewed gate leg 3 red
- **#9120**: CI: native-diagnostics opt-in is a substring match on the PR body, so a PR that merely mentions the marker inherits the native-workload audit gate
- **#9124**: Board 06 drain remainder: same-net via-vs-trace slits and the face-aligned corner-wedge seat the slit-band predicate accepts
- **#9138**: Match-group skew producer measures summed PCB copper: dead-end loops and duplicates mask real skew
- **#9143**: Board 03: fiducial keepouts collide with the native_clearance contract and the #8838 mask policy
- **#9182**: off_path_length_mm charges length-matching staircase copper, so OFF_PATH_TOLERANCE_MM tracks staircase density rather than defect presence
- **#9188**: same_net_band_edges() hardcodes the jlcpcb 4L/1oz stackup inside a shared library module
- **#9196**: Board 06 leg band check: pin the `sibling_legs` contract at the predicate level (and tidy its signature)
- **#9204**: Converge board 06's leg-band predicate on the shared band constants from #9150
- **#9208**: Board 06 USB2_D- drain: C++ via-annulus vs own-net-trace slit-band check
- **#9214**: Record the trim-less detection margin that `_straddle_slack_mb` trades away on large-page hosts
- **#9217**: Board 07: test_board07_real_design still grades byte spreads on raw copper, not the gate's matched length

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9083**: fix(board03): clip silk off drilled holes in the recipe and gate silk_hole_overlap = 0
- **#9177**: fix(board03): land fiducials and board ID past the native_clearance contract
- **#9207**: refactor: require stackup args on same_net_band_edges()
- **#9212**: fix: refuse C++ via seats whose annulus slits an own-net trace
- **#9218**: docs: record trim-less detection margin and fix healthy-residual sign in RSS hook test
- **#9219**: test: grade board07 byte spreads on the gate's matched length
- **#9222**: feat: add --order-method regret net ordering (biggest-loss nets first)
- **#9224**: refactor: use shared band constants in board 06 leg-band predicate

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9123**: ci: anchor native-diagnostics opt-in to its own line and state the audit tolerance
- **#9194**: docs: record measured off-path decomposition at board-07's OFF_PATH_TOLERANCE_MM
- **#9220**: test: pin sibling_legs contract on _same_net_leg_band_ok directly

## Proposed

Issues carrying `loom:curated`.

- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#7856**: [Epic #6441] Phase 5 measurement: coupled-trunk retention >= 95% on boards 03, 06b, 06rf, 07 *(curated)*
- **#8214**: optimize-traces: collision-check each rewrite on finished boards instead of whole-net rollback *(curated)*
- **#8425**: Live-verify covered-track split (#8406) on pinned kicad-cli and #8368 board B *(curated)*
- **#8434**: Changelog Fragment Check re-runs on every label change; Loom role label churn keeps it queued *(curated)*
- **#8486**: route perf: nets proven sealed are re-searched at full budget and re-rescued every iteration (43% of search time, rescue 0/15 for ~1,475 s each pass) *(curated)*
- **#8530**: Fiducials + board-identification silkscreen should be automatic: add-fiducials / add-board-info commands, a check rule, and a default tapeout step *(curated)*
- **#8603**: Board 07: classify and eliminate remaining redundant_copper warnings at source (part of #8399) *(curated)*
- **#8610**: route --complete chain pass commits ~282 shorts against Net 0 copper on dense mains board (rc 4) *(curated)*
- **#8620**: Find the real cause of the 10-12x pure-Python routing slowdown with all extras on macOS *(curated)*
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs *(curated)*
- **#8858**: Verify default-on mask_to_copper on native KiCad 10.0.5 and baseline 543/670 board findings (PR #8849) *(curated)*
- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors) *(curated)*
- **#8943**: Record JLCDFM silkscreen line-width selection criterion and per-item rows (residue of #8844) *(curated)*
- **#8948**: kct's own managed .kicad_dru makes mask_to_copper incomplete: admit physical_hole_clearance, text_thickness and text_height to the native context allowlist *(curated)*
- **#8962**: Same-net slit population on fresh regens: repair-pass + route-side producers (stale fixture attribution from #8957) *(curated)*
- **#8979**: CI red on main: Routed PCB DRC Check fails on boards 01/02 physical_copper_gap (2 + 14 blocking errors) *(curated)*
- **#9000**: Router: barrier-closure diagnostic (predict severed nets when a routing loop closes) *(curated)*
- **#9001**: Router: regret ordering as a net-order method (commit biggest-loss nets first) *(curated)*
- **#9019**: Grid router (Python + C++): keep own-net pad-vs-trace legs and neck-down taper out of the slit band (#9011, board 06) *(curated)*
- **#9031**: tests: nested pytest.approx() at test_expand_route_experiment.py:166 blocks any pytest >= 9.2 bump *(curated)*
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings *(curated)*
- **#9035**: Ecosystem registry drift: upstream license/rename/archive change *(curated)*
- **#9058**: Board 01: re-pin readiness evidence after PR #8848 on a host where mask_to_copper completes *(curated)*
- **#9065**: Wire the silk-vs-drill tools into board 03's recipe and gate silk_hole_overlap = 0 in CI *(curated)*
- **#9073**: Board 05: clear the 29 physical_copper_gap errors in the reviewed routing recipe *(curated)*
- **#9093**: submit-jlcpcb: attended, exact-total-approved JLC parts purchase leg *(curated)*
- **#9097**: Board 04 fresh regen: raw route carries 13 advisory warnings (pour_slot x10, staircase, fragments, silk_hole_overlap) - reviewed gate leg 3 red *(curated)*
- **#9120**: CI: native-diagnostics opt-in is a substring match on the PR body, so a PR that merely mentions the marker inherits the native-workload audit gate *(curated)*
- **#9124**: Board 06 drain remainder: same-net via-vs-trace slits and the face-aligned corner-wedge seat the slit-band predicate accepts *(curated)*
- **#9138**: Match-group skew producer measures summed PCB copper: dead-end loops and duplicates mask real skew *(curated)*
- **#9140**: Coupled-pair pose/stub-tail legs: keep own-net pad legs out of the slit band (#9011, board 06 LVDS) *(curated)*
- **#9143**: Board 03: fiducial keepouts collide with the native_clearance contract and the #8838 mask policy *(curated)*
- **#9182**: off_path_length_mm charges length-matching staircase copper, so OFF_PATH_TOLERANCE_MM tracks staircase density rather than defect presence *(curated)*
- **#9188**: same_net_band_edges() hardcodes the jlcpcb 4L/1oz stackup inside a shared library module *(curated)*
- **#9196**: Board 06 leg band check: pin the `sibling_legs` contract at the predicate level (and tidy its signature) *(curated)*
- **#9204**: Converge board 06's leg-band predicate on the shared band constants from #9150 *(curated)*
- **#9208**: Board 06 USB2_D- drain: C++ via-annulus vs own-net-trace slit-band check *(curated)*
- **#9210**: acute_foldback: cross-net clearance veto keeps copper a later same-group repair removed *(curated)*
- **#9214**: Record the trim-less detection margin that `_straddle_slack_mb` trades away on large-page hosts *(curated)*
- **#9217**: Board 07: test_board07_real_design still grades byte spreads on raw copper, not the gate's matched length *(curated)*

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
| Very important | 1 |
| Important | 1 |
| Ready (`loom:issue`) | 3 |
| In Progress (`loom:building`) | 20 |
| PRs awaiting review | 8 |
| Approved PRs awaiting merge | 4 |
| Curated | 42 |
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
