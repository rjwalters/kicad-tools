# Work Plan

Prioritized roadmap generated from current GitHub label state. Maintained by the Guide triage agent.

*Last updated: 2026-09-29*

---

<!-- guide:plan-body:start -->
## Operator Attention: Merge-Risk-Hold Pileup

Judge-approved PRs stuck under a `loom:operator` merge-risk hold — implementation work is done, only a human merge decision is missing.

_None._

## Operator Priority

Issues the operator starred (`loom:operator-priority`); land these first.

_None._

## Ready

Human-approved issues ready for implementation (`loom:issue`).

- **#5398**: [Epic #4410] Complete stranded Kelvin sense nets through generic routing and batch rescue
- **#5660**: [Epic #5509] Phase 3a: switch halo marking (grid.py + grid.cpp) to the clearance kernel

## In Progress

Issues currently being built (`loom:building`).

- **#5790**: research: extend KRT benchmark to boards 04/05/07 (fanout, length-matching, dense bundles) + fix reproduce command

## PRs Awaiting Review

PRs waiting on Judge (`loom:review-requested`).

- **#5799**: docs(research): benchmark KRT on boards 04/05/07 and fix the harness timeout leak

## Approved (Awaiting Merge)

PRs that passed review and are queued for Champion auto-merge (`loom:pr`).

_None._

## Proposed

Issues carrying `loom:curated`.

- **#5164**: Stage and validate native zone refill for Board 07 via repairs before restoring CI *(curated)*
- **#5240**: CI runtime: reduce measured Test, Board06 E2E and diff-pair regression job medians *(curated)*
- **#5286**: Board07 full recipe exports seven unexpected signal opens and clearance failures after stage-budget integration *(curated)*
- **#5333**: Board07 coupled pre-pass exhausts all seven pair searches despite valid corridor guides *(curated)*
- **#5398**: [Epic #4410] Complete stranded Kelvin sense nets through generic routing and batch rescue *(curated)*
- **#5410**: router: refine dynamic route halo rejection with physical clearance checks *(curated)*
- **#5506**: router(HV): attribute and resolve the /FUSED_LINE J2.1→Q2B.2 no-path-layer-constrained leg on softstart rev-C (#4507 T4) *(curated)*
- **#5508**: Epic: pad-access invariant — no committed route may strand an unrouted pad *(curated)*
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC *(curated)*
- **#5511**: Epic: netlist degrees of freedom — swappable-pin groups and acting placement feedback *(curated)*
- **#5550**: Reconcile 4 outstanding loom-quarantine stashes left by the worktree reaper (issues 5201, 4982, 5216, 5004) *(curated)*
- **#5607**: boards: committed board06 output/ artifact is stale relative to the recipe (needs a KiCad-capable refresh) *(curated)*
- **#5617**: perf(router): profile and reduce the Diff-Pair regression job's re-route step (17.55 min = 96% of the job, unchanged across #5403/#5433/#5488/#5500) *(curated)*
- **#5660**: [Epic #5509] Phase 3a: switch halo marking (grid.py + grid.cpp) to the clearance kernel *(curated)*
- **#5673**: Halo-shape switch (#5660/PR #5668) under-blocks via drill-to-drill spacing — board02/06 DRC regressions *(curated)*
- **#5693**: Upstream the #5690 backticked-trailer fix to rjwalters/loom and retire the resync-ignore pin *(curated)*
- **#5701**: ci: stacked PRs (base = a feature branch) get no CI at all — 'CLEAN' means 'never ran', not 'passed' *(curated)*
- **#5704**: Manufacturing profiles: project min_silk_clearance is mapped from the solder-mask clearance, not the silkscreen floor *(curated)*
- **#5712**: champion-epic.md: 'Epic Passes Evaluation' comments reuse the rejection-only champion:epic-verdict marker, corrupting the unrevised-eval skip/escalate ladder *(curated)*
- **#5790**: research: extend KRT benchmark to boards 04/05/07 (fanout, length-matching, dense bundles) + fix reproduce command *(curated)*

## Proposed (Architect / Hermit)

_None._

## Epics

- **#3438**: router: board 07 stuck at 28/31 — negotiated router cannot complete parallel pad-array bundles (DDR bundle ALONE routes 9/11)
- **#4410**: board-05: generator must produce a manufacturable BLDC board unattended (81% route, 7 ISENSE/PWM open, 2 real shorts)
- **#4431**: route: per-net scalar clearance cannot express HV isolation — need class-pair (net-class × net-class) clearance rules in the router; both workarounds proven inadequate on a mains board
- **#5508**: Epic: pad-access invariant — no committed route may strand an unrouted pad
- **#5509**: Epic: one clearance kernel — search, commit, and final validation must agree with kicad-cli DRC
- **#5511**: Epic: netlist degrees of freedom — swappable-pin groups and acting placement feedback
- **#5784**: Epic: adopt measured KiCadRoutingTools techniques (oracle completion, pose diff-pair search, N+1 rip-up)

## Backlog Balance

| Tier | Count |
|------|-------|
| Operator merge-risk holds | 0 |
| Operator priority | 0 |
| Ready (`loom:issue`) | 2 |
| In Progress (`loom:building`) | 1 |
| PRs awaiting review | 1 |
| Approved PRs awaiting merge | 0 |
| Curated | 20 |
| Architect / Hermit proposals | 0 |
| Active epics | 7 |
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
