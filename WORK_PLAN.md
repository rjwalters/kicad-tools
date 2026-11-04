# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-11-03*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs under a hold (`loom:blocked` or `loom:operator`) — only a human merge decision is missing.

- **#8257**: ci: provision jlcparts catalog for board 05, re-pin board 02/03 baselines

## Very Important

`loom:very-important` (one counts per workspace); every stage takes these first.

_None._

## Important

`loom:important`; taken before unlabelled work.

_None._

## Ready

Human-approved issues ready for implementation (`loom:issue`).

_None._

## In Progress

Issues currently being built (`loom:building`).

- **#8255**: Release v0.28.0 blocked: three red main-CI jobs (board 05 strict stock preflight, board 02/03 baselines after #8229/#8230)
- **#8267**: Board 05: D3 (C131244 red 0805 LED) shows stock 0 in jlcparts catalog; --strict-preflight bom_lcsc_stock fails
- **#8278**: Board 05: regeneration/readiness refresh blocked by new ampacity + redundant_copper kct checks
- **#8288**: Relief rescue: complete the sealed centre-pin case or report corridor shortfall (split from #8256)
- **#8295**: Relief freed-band diagnostic: spatially bound the measured-gap scan (follow-up to #8256)
- **#8296**: Pad-centre join in _partial save emits off-angle segments and tilts merged runs (follow-up to #8270)
- **#8300**: optimize-traces: accept --voltage-map / creepage inputs and reject edits that violate pairwise creepage
- **#8301**: kct creepage: report a location witness (closest points + items) for every pair row, including board rows
- **#8303**: census attach-zone cross terms are segmentation-dependent (no collinear fusion, unlike router #8280)
- **#8310**: route --complete: target individual links (endpoints, via waypoints, layer limits) with a probe mode and per-link gating
- **#8311**: pcb transplant: copy named nets' copper from one board into another of the same placement
- **#8312**: fix-silkscreen: clip footprint silk that violates silk-to-pad clearance
- **#8313**: pcb prune: break via-closed loops on nets with declared current paths so reinforce can anchor them
- **#8322**: optimize-traces --drc-aware reports "no regressions" while kicad-cli (project DRU) finds 1→17 clearance errors and creepage 4→33
- **#8323**: pcb prune adds a clearance error under the project's custom DRU rule despite its built-in DRC check
- **#8332**: creepage: #8232 same_footprint 'board' residual row disagrees with router kernel inside attach zones (R25/R75 softstart)
- **#8333**: pcb prune: loop removal deletes all copper on a single-pad net

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#8306**: feat: add closest-point witness to kct creepage rows
- **#8307**: feat: reject optimize-traces edits that violate pairwise creepage
- **#8318**: feat: add kct route-link for targeted single-link routing and probe
- **#8319**: feat: add --clip-to-pads to fix-silkscreen for silk-to-pad clearance
- **#8328**: fix: key pcb prune DRC referee on rule and nets, warn when project rules are missing
- **#8334**: fix: skip prune loop removal on nets with fewer than two terminals
- **#8335**: fix: emit 45-aligned doglegs when joining route ends to pad centres
- **#8338**: test(router): pin relief rescue outcomes on the sealed centre pin
- **#8339**: perf: index blocker segments in relief measured-gap scan
- **#8340**: fix(creepage): fuse collinear trace runs before attach-zone cross terms
- **#8342**: fix(creepage): classify same-footprint residual row by attach zone

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

- **#8257**: ci: provision jlcparts catalog for board 05, re-pin board 02/03 baselines
- **#8326**: fix: cross-check optimize-traces --drc-aware against kicad-cli before/after

## Proposed

Issues carrying `loom:curated`.

- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#5539**: Champion: Merge-Risk Hold Digest *(curated)*
- **#7487**: Re-rule or remove board 03 usb_geometry via-in-pad window exception by 2027-04-19 (#7457 / #7084 Q10) *(curated)*
- **#8214**: optimize-traces: collision-check each rewrite on finished boards instead of whole-net rollback *(curated)*
- **#8255**: Release v0.28.0 blocked: three red main-CI jobs (board 05 strict stock preflight, board 02/03 baselines after #8229/#8230) *(curated)*
- **#8267**: Board 05: D3 (C131244 red 0805 LED) shows stock 0 in jlcparts catalog; --strict-preflight bom_lcsc_stock fails *(curated)*
- **#8278**: Board 05: regeneration/readiness refresh blocked by new ampacity + redundant_copper kct checks *(curated)*
- **#8285**: optimize-traces: connectivity check discards a different set of nets across identical runs (nondeterminism) *(curated)*
- **#8288**: Relief rescue: complete the sealed centre-pin case or report corridor shortfall (split from #8256) *(curated)*
- **#8295**: Relief freed-band diagnostic: spatially bound the measured-gap scan (follow-up to #8256) *(curated)*
- **#8296**: Pad-centre join in _partial save emits off-angle segments and tilts merged runs (follow-up to #8270) *(curated)*
- **#8300**: optimize-traces: accept --voltage-map / creepage inputs and reject edits that violate pairwise creepage *(curated)*
- **#8301**: kct creepage: report a location witness (closest points + items) for every pair row, including board rows *(curated)*
- **#8303**: census attach-zone cross terms are segmentation-dependent (no collinear fusion, unlike router #8280) *(curated)*
- **#8310**: route --complete: target individual links (endpoints, via waypoints, layer limits) with a probe mode and per-link gating *(curated)*
- **#8311**: pcb transplant: copy named nets' copper from one board into another of the same placement *(curated)*
- **#8312**: fix-silkscreen: clip footprint silk that violates silk-to-pad clearance *(curated)*
- **#8313**: pcb prune: break via-closed loops on nets with declared current paths so reinforce can anchor them *(curated)*
- **#8322**: optimize-traces --drc-aware reports "no regressions" while kicad-cli (project DRU) finds 1→17 clearance errors and creepage 4→33 *(curated)*
- **#8323**: pcb prune adds a clearance error under the project's custom DRU rule despite its built-in DRC check *(curated)*
- **#8327**: route --complete: softstart rev-D loses /V_AC_SENSE_RAW R2.1-C31.1 after #8280 pairwise gate (9/11 -> 7/11 HV nets, link budget overrun) *(curated)*
- **#8332**: creepage: #8232 same_footprint 'board' residual row disagrees with router kernel inside attach zones (R25/R75 softstart) *(curated)*
- **#8333**: pcb prune: loop removal deletes all copper on a single-pad net *(curated)*
- **#8337**: Live-verify kct pcb strip --from-report/--dangling against real kicad-cli --refill-zones *(curated)*

## Proposed (Architect / Hermit)

_None._

## Epics

- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC
- **#5952**: Epic: Harness-neutral kicad-tools (Claude Code, Codex CLI, opencode)

## Backlog Balance

| Bucket | Count |
|------|-------|
| Operator merge-risk holds | 1 |
| Very important | 0 |
| Important | 0 |
| Ready (`loom:issue`) | 0 |
| In Progress (`loom:building`) | 17 |
| PRs awaiting review | 11 |
| Approved PRs awaiting merge | 2 |
| Curated | 24 |
| Architect / Hermit proposals | 0 |
| Active epics | 3 |
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
