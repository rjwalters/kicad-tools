# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-16*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9008**: chore(loom): resync installed Loom to v0.19.1302
- **#9028**: build(deps): bump astral-sh/setup-uv from 10.3.2 to 10.3.3 in the github-actions group across 1 directory

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors)

## Important

`loom:important`; taken before unlabelled work.

- **#8858**: Verify default-on mask_to_copper on native KiCad 10.0.5 and baseline 543/670 board findings (PR #8849)

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#8957**: Router leaves same-net sub-danger copper slits on regenerated synthetic boards (physical_copper_gap, boards 06/07)
- **#8992**: Tie-row grouping bound is independent of the spacing the placer uses: rows at min_clearance_mm >= 0.5 mm place but do not count
- **#8999**: Router: airwire-separation cost term (prefer routes that keep other nets' desire-lines intact)
- **#9000**: Router: barrier-closure diagnostic (predict severed nets when a routing loop closes)
- **#9001**: Router: regret ordering as a net-order method (commit biggest-loss nets first)
- **#9002**: Zones/router: pour-skeleton fit experiment — predict refills cut into islands before refill
- **#9007**: Board 06 escape-via seat predicate carries only the same-net pad band, not the trace band
- **#9009**: mask_to_copper still cannot complete after #8946: export/object KiCad version mismatch under KCT_KICAD_DOCKER=1, and no remediation for an unqualified host KiCad
- **#9015**: Reviewed clearance class arm uses the outer floor only: net classes authored between the split inner/outer floors are unprotected on inner layers
- **#9025**: Add a repin_staged regression test for the waivers-sidecar inputs binding
- **#9030**: test_stitch_same_net_pad_gap_8957.py module docstring still names GapBand.error_edge_mm as the stitcher's edge
- **#9031**: tests: nested pytest.approx() at test_expand_route_experiment.py:166 blocks any pytest >= 9.2 bump
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings
- **#9041**: changelog.d/8974.fixed.md quotes the pre-merge 28 finding count; merged-tree delta is 23 to 0

## In Progress

Issues currently being built (`loom:building`).

- **#8840**: Sharp trace corner and missing plated through-hole rules; fiducial parity check
- **#8997**: Gerber reader: a macro named C/R/O/P silently shadows the standard aperture template
- **#8998**: Router: joint per-net candidate selection before commit (negotiated driver)
- **#9003**: Router: detour-ratio report and targeted post-settle detour repair pass
- **#9034**: Pad spacing: physical_copper_gap errors outnumber the fab DFM's danger items about 3 to 1
- **#9038**: mask_to_copper reports about 200 times JLCDFM's count on a consumer board, with messages that cannot be triaged

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#9014**: feat: add --report-detour-ratio probe with alone-on-empty detour report
- **#9040**: fix: reject Gerber aperture macros named with reserved template names C/R/O/P
- **#9043**: test: pin pad_spacing fab-band severities on a committed five-case fixture
- **#9044**: fix: triageable mask_to_copper findings; incomplete runs cap severity at warning

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#8623**: build(deps-dev): bump @astrojs/check from 0.9.11 to 0.9.12 in /site
- **#9008**: chore(loom): resync installed Loom to v0.19.1302
- **#9028**: build(deps): bump astral-sh/setup-uv from 10.3.2 to 10.3.3 in the github-actions group across 1 directory

## Proposed

Issues carrying `loom:curated`.

- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#8214**: optimize-traces: collision-check each rewrite on finished boards instead of whole-net rollback *(curated)*
- **#8425**: Live-verify covered-track split (#8406) on pinned kicad-cli and #8368 board B *(curated)*
- **#8434**: Changelog Fragment Check re-runs on every label change; Loom role label churn keeps it queued *(curated)*
- **#8486**: route perf: nets proven sealed are re-searched at full budget and re-rescued every iteration (43% of search time, rescue 0/15 for ~1,475 s each pass) *(curated)*
- **#8530**: Fiducials + board-identification silkscreen should be automatic: add-fiducials / add-board-info commands, a check rule, and a default tapeout step *(curated)*
- **#8603**: Board 07: classify and eliminate remaining redundant_copper warnings at source (part of #8399) *(curated)*
- **#8610**: route --complete chain pass commits ~282 shorts against Net 0 copper on dense mains board (rc 4) *(curated)*
- **#8620**: Find the real cause of the 10-12x pure-Python routing slowdown with all extras on macOS *(curated)*
- **#8629**: submit-jlcpcb --website: SELECTORS are placeholders that match nothing on the live quote page; CDP attach fails on a Chrome with no open tabs *(curated)*
- **#8840**: Sharp trace corner and missing plated through-hole rules; fiducial parity check *(curated)*
- **#8858**: Verify default-on mask_to_copper on native KiCad 10.0.5 and baseline 543/670 board findings (PR #8849) *(curated)*
- **#8900**: CI red on main: board 03 gates fail on default-on mask_to_copper (334 errors) *(curated)*
- **#8943**: Record JLCDFM silkscreen line-width selection criterion and per-item rows (residue of #8844) *(curated)*
- **#8948**: kct's own managed .kicad_dru makes mask_to_copper incomplete: admit physical_hole_clearance, text_thickness and text_height to the native context allowlist *(curated)*
- **#8957**: Router leaves same-net sub-danger copper slits on regenerated synthetic boards (physical_copper_gap, boards 06/07) *(curated)*
- **#8962**: Same-net slit population on fresh regens: repair-pass + route-side producers (stale fixture attribution from #8957) *(curated)*
- **#8979**: CI red on main: Routed PCB DRC Check fails on boards 01/02 physical_copper_gap (2 + 14 blocking errors) *(curated)*
- **#8992**: Tie-row grouping bound is independent of the spacing the placer uses: rows at min_clearance_mm >= 0.5 mm place but do not count *(curated)*
- **#8994**: C++ pathfinder via placement: keep via annuli out of the same-net pad slit band (board 06 producer, #8962 producer 2b) *(curated)*
- **#8997**: Gerber reader: a macro named C/R/O/P silently shadows the standard aperture template *(curated)*
- **#8998**: Router: joint per-net candidate selection before commit (negotiated driver) *(curated)*
- **#8999**: Router: airwire-separation cost term (prefer routes that keep other nets' desire-lines intact) *(curated)*
- **#9000**: Router: barrier-closure diagnostic (predict severed nets when a routing loop closes) *(curated)*
- **#9001**: Router: regret ordering as a net-order method (commit biggest-loss nets first) *(curated)*
- **#9002**: Zones/router: pour-skeleton fit experiment — predict refills cut into islands before refill *(curated)*
- **#9003**: Router: detour-ratio report and targeted post-settle detour repair pass *(curated)*
- **#9007**: Board 06 escape-via seat predicate carries only the same-net pad band, not the trace band *(curated)*
- **#9009**: mask_to_copper still cannot complete after #8946: export/object KiCad version mismatch under KCT_KICAD_DOCKER=1, and no remediation for an unqualified host KiCad *(curated)*
- **#9015**: Reviewed clearance class arm uses the outer floor only: net classes authored between the split inner/outer floors are unprotected on inner layers *(curated)*
- **#9017**: Router: resolve pad_spacing detection edge and plumb it into kct route (#9011 plumbing) *(curated)*
- **#9025**: Add a repin_staged regression test for the waivers-sidecar inputs binding *(curated)*
- **#9030**: test_stitch_same_net_pad_gap_8957.py module docstring still names GapBand.error_edge_mm as the stitcher's edge *(curated)*
- **#9031**: tests: nested pytest.approx() at test_expand_route_experiment.py:166 blocks any pytest >= 9.2 bump *(curated)*
- **#9033**: Silkscreen-to-hole: kct reports far fewer findings than the fab DFM, and only as warnings *(curated)*
- **#9034**: Pad spacing: physical_copper_gap errors outnumber the fab DFM's danger items about 3 to 1 *(curated)*
- **#9035**: Ecosystem registry drift: upstream license/rename/archive change *(curated)*
- **#9038**: mask_to_copper reports about 200 times JLCDFM's count on a consumer board, with messages that cannot be triaged *(curated)*
- **#9041**: changelog.d/8974.fixed.md quotes the pre-merge 28 finding count; merged-tree delta is 23 to 0 *(curated)*

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
| Important | 1 |
| Ready (`loom:issue`) | 14 |
| In Progress (`loom:building`) | 6 |
| PRs awaiting review | 4 |
| Approved PRs awaiting merge | 3 |
| Curated | 40 |
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
